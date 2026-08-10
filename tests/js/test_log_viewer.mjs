// Tests for src/static/js/log-viewer.js (Task 26, from-scratch rewrite).
//
// Same rationale/harness as tests/js/test_common.mjs and
// tests/js/test_dashboard.mjs (Tasks 24/25): Node's built-in
// `node:test`/`node:assert`, no framework, no package.json.
//
// log-viewer.js's own header comment documents its testability split:
// every function ABOVE its "DOM rendering / wiring" section is pure (no
// `document`/`window` access, beyond an injectable `storage` parameter for
// the two preference helpers) and exported via `module.exports`.
// Everything below that point only runs through `init()`, which only
// fires when `document` exists -- so `require()`-ing this module under
// plain Node exercises only the pure/exported half, side-effect-free.
//
// HIGHEST PRIORITY in this file: `escapeAndHighlight`, the one place in
// the whole log-viewer rewrite that builds an HTML string from untrusted
// log-line content and assigns it via `innerHTML`. It's tested against a
// battery of real XSS payloads, both with specific expected-output
// assertions AND a general safety invariant (see "escapeAndHighlight" /
// "safety invariant" suites below): stripping out ONLY the literal
// `<mark>`/`</mark>` substrings this file itself inserts, the remainder
// must contain no `<`/`>` characters at all -- i.e. every angle bracket
// that survives into the output is either escaped, or part of a tag this
// file wrote itself, never raw attacker-controlled markup.
//
// What this file deliberately does NOT (and per the task brief, cannot)
// test without a real browser/DOM: actual `IntersectionObserver` firing on
// real scroll, `fetch`/`AbortController` real-network timeout behavior,
// CSS layout/wrapping/scroll-position autoscroll detection, and the
// `init()`/event-wiring code path itself. `pollOnce`/`fetchStat`/
// `fetchChunkStep`/`runBackfill` are tested against an injected mock
// `fetchFn` returning canned `{status, json()}`-shaped objects (no real
// network); `renderLineContent` is tested against a plain
// `{textContent: "", innerHTML: ""}` stand-in object (no real DOM node).
//
// Run with:  node tests/js/test_log_viewer.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const logViewerJsPath = path.join(__dirname, "..", "..", "src", "static", "js", "log-viewer.js");

const require = createRequire(import.meta.url);
const AlertManagerLogViewer = require(logViewerJsPath);

const {
    LEVELS,
    DEFAULT_LEVEL_FILTERS,
    DEFAULT_POLL_INTERVAL_MS,
    SEARCH_DEBOUNCE_MS,
    RENDER_BATCH_SIZE,
    parseLine,
    passesFilters,
    LogStore,
    escapeAndHighlight,
    renderLineContent,
    debounce,
    buildListUrl,
    buildStatUrl,
    buildTailUrl,
    buildChunkUrl,
    fetchStat,
    pollOnce,
    fetchChunkStep,
    runBackfill,
    computeProgressPercent,
    formatBackfillProgress,
    computeCounterText,
    mergeFilterPreferences,
    readPersistedFilters,
    persistFilters,
    readPersistedOpsToggle,
    persistOpsToggle,
} = AlertManagerLogViewer;

// ---------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------

function makeFakeStorage(initial) {
    const data = new Map(Object.entries(initial || {}));
    return {
        getItem(key) {
            return data.has(key) ? data.get(key) : null;
        },
        setItem(key, value) {
            data.set(key, String(value));
        },
        _data: data,
    };
}

function makeThrowingStorage() {
    return {
        getItem() {
            throw new Error("storage disabled");
        },
        setItem() {
            throw new Error("storage disabled");
        },
    };
}

/** A canned fetch response stand-in: `{status, json}`. */
function fakeResponse(status, body) {
    return {
        status: status,
        json: function () {
            return Promise.resolve(body);
        },
    };
}

/** Builds a mock fetchFn from a queue of canned responses (by call order), asserting it isn't called more times than provided. */
function mockFetchSequence(responses) {
    let i = 0;
    const calls = [];
    const fn = function (url) {
        calls.push(url);
        if (i >= responses.length) {
            throw new Error("mockFetchSequence: no more canned responses, but fetchFn called again with " + url);
        }
        const r = responses[i];
        i += 1;
        return Promise.resolve(r);
    };
    fn.calls = calls;
    return fn;
}

// =======================================================================
// Constants sanity
// =======================================================================

test("LEVELS matches CLAUDE.md's canonical critical->debug order", () => {
    assert.deepEqual(LEVELS, ["critical", "warning", "notice", "info", "debug"]);
});

test("DEFAULT_LEVEL_FILTERS defaults: critical/warning/notice on, info/debug off", () => {
    assert.deepEqual(DEFAULT_LEVEL_FILTERS, { critical: true, warning: true, notice: true, info: false, debug: false });
});

test("DEFAULT_POLL_INTERVAL_MS is 10000, matching the server's default LOG_POLL_INTERVAL_MS", () => {
    assert.equal(DEFAULT_POLL_INTERVAL_MS, 10000);
});

test("SEARCH_DEBOUNCE_MS is 250 per the brief", () => {
    assert.equal(SEARCH_DEBOUNCE_MS, 250);
});

test("RENDER_BATCH_SIZE is 500 per the brief", () => {
    assert.equal(RENDER_BATCH_SIZE, 500);
});

// =======================================================================
// parseLine
// =======================================================================

test("parseLine extracts a leading 10-digit Unix timestamp and bracketed level", () => {
    const entry = parseLine("1737400000 [critical] [system] test-source: Title - message");
    assert.equal(entry.ts, 1737400000);
    assert.equal(entry.level, "critical");
    assert.equal(entry.raw, "1737400000 [critical] [system] test-source: Title - message");
});

test("parseLine lowercases an uppercase level bracket", () => {
    const entry = parseLine("1737400000 [WARNING] [system] x: y - z");
    assert.equal(entry.level, "warning");
});

test("parseLine handles a line with no timestamp/level (ts=null, level=null, raw preserved)", () => {
    const entry = parseLine("some unstructured system-message text");
    assert.equal(entry.ts, null);
    assert.equal(entry.level, null);
    assert.equal(entry.raw, "some unstructured system-message text");
});

test("parseLine coerces null/undefined to an empty-string raw", () => {
    assert.equal(parseLine(null).raw, "");
    assert.equal(parseLine(undefined).raw, "");
});

test("parseLine does not match a level word without brackets", () => {
    const entry = parseLine("1737400000 critical happened");
    assert.equal(entry.level, null);
});

// =======================================================================
// passesFilters (the single predicate LogStore.matchIndexes AND the
// live-append fast path both use)
// =======================================================================

test("passesFilters: a recognized level passes only when its filter is true", () => {
    const critical = parseLine("1 [critical] x");
    assert.equal(passesFilters(critical, { critical: true }, ""), true);
    assert.equal(passesFilters(critical, { critical: false }, ""), false);
});

test("passesFilters: a line with NO recognized level always passes the level check, even with all filters off", () => {
    const unmatched = parseLine("no level here");
    assert.equal(passesFilters(unmatched, { critical: false, warning: false, notice: false, info: false, debug: false }, ""), true);
});

test("passesFilters: empty/missing query never excludes", () => {
    const entry = parseLine("1 [info] hello");
    assert.equal(passesFilters(entry, { info: true }, ""), true);
    assert.equal(passesFilters(entry, { info: true }, null), true);
    assert.equal(passesFilters(entry, { info: true }, undefined), true);
});

test("passesFilters: case-insensitive substring search against raw", () => {
    const entry = parseLine("1 [info] Barcode Not Found for SKU 123");
    assert.equal(passesFilters(entry, { info: true }, "barcode"), true);
    assert.equal(passesFilters(entry, { info: true }, "BARCODE"), true);
    assert.equal(passesFilters(entry, { info: true }, "nope"), false);
});

test("passesFilters is ANDed, not ORed: both level filter AND search query must pass", () => {
    const critical = parseLine("1 [critical] disk failure");
    const warning = parseLine("1 [warning] disk failure");
    const filters = { critical: true, warning: false };
    // Matches search, but level filter is off -> excluded.
    assert.equal(passesFilters(warning, filters, "disk"), false);
    // Matches level filter, but search doesn't match -> excluded.
    assert.equal(passesFilters(critical, filters, "nonexistent"), false);
    // Matches both -> included.
    assert.equal(passesFilters(critical, filters, "disk"), true);
});

// =======================================================================
// LogStore
// =======================================================================

test("LogStore.appendLive pushes parsed entries at the end, preserving order", () => {
    const store = new LogStore();
    store.appendLive(["1 [info] a", "2 [info] b"]);
    store.appendLive(["3 [info] c"]);
    assert.equal(store.length, 3);
    assert.deepEqual(store.entries.map((e) => e.raw), ["1 [info] a", "2 [info] b", "3 [info] c"]);
});

test("LogStore.prependOlder unshifts parsed entries at the front, preserving THEIR order", () => {
    const store = new LogStore();
    store.appendLive(["30 [info] live"]);
    store.prependOlder(["20 [info] older-chunk-line1", "25 [info] older-chunk-line2"]);
    assert.deepEqual(store.entries.map((e) => e.raw), ["20 [info] older-chunk-line1", "25 [info] older-chunk-line2", "30 [info] live"]);
});

test("LogStore: three successive prependOlder calls (simulating real backfill order, each chunk older than the last) end up fully chronological", () => {
    const store = new LogStore();
    store.appendLive(["40 [info] live"]);
    store.prependOlder(["30 [info] chunk1"]); // first backfill call: just-older-than-live
    store.prependOlder(["20 [info] chunk2"]); // second call: even older
    store.prependOlder(["10 [info] chunk3"]); // third call: oldest
    assert.deepEqual(store.entries.map((e) => e.raw), ["10 [info] chunk3", "20 [info] chunk2", "30 [info] chunk1", "40 [info] live"]);
});

test("LogStore.matchIndexes returns ascending indexes, AND-combining level filters with search", () => {
    const store = new LogStore();
    store.appendLive([
        "1 [critical] disk failure", // 0: matches both
        "2 [warning] disk failure", // 1: level filtered out
        "3 [critical] all clear", // 2: search filtered out
        "4 [info] disk failure", // 3: level filtered out (info off by default)
    ]);
    const filters = { critical: true, warning: false, notice: false, info: false, debug: false };
    assert.deepEqual(store.matchIndexes(filters, "disk"), [0]);
});

test("LogStore.matchIndexes with an empty query returns every index passing the level filter alone", () => {
    const store = new LogStore();
    store.appendLive(["1 [critical] a", "2 [warning] b", "3 [info] c"]);
    const filters = { critical: true, warning: true, notice: true, info: false, debug: false };
    assert.deepEqual(store.matchIndexes(filters, ""), [0, 1]);
});

test("LogStore.length reflects total entries regardless of filters", () => {
    const store = new LogStore();
    store.appendLive(["1 [debug] a", "2 [debug] b"]);
    assert.equal(store.length, 2);
    assert.deepEqual(store.matchIndexes({ debug: false }, ""), []);
});

// =======================================================================
// escapeAndHighlight -- the security-critical function. Exhaustive.
// =======================================================================

test("escapeAndHighlight: no query returns plain escapeHtml output, no <mark> tags", () => {
    const out = escapeAndHighlight("<script>alert(1)</script>", "");
    assert.equal(out.indexOf("<mark>"), -1);
    assert.equal(out.indexOf("<script>"), -1);
    assert.equal(out, "&lt;script&gt;alert(1)&lt;/script&gt;");
});

test("escapeAndHighlight: no match returns plain escapeHtml output, no <mark> tags", () => {
    const out = escapeAndHighlight("hello world", "xyz");
    assert.equal(out, "hello world");
    assert.equal(out.indexOf("<mark>"), -1);
});

test("escapeAndHighlight: simple match is wrapped in <mark>, rest is untouched", () => {
    assert.equal(escapeAndHighlight("hello world", "world"), "hello <mark>world</mark>");
});

test("escapeAndHighlight: case-insensitive match, ORIGINAL casing preserved inside <mark>", () => {
    assert.equal(escapeAndHighlight("ErRoR occurred ERROR again", "error"), "<mark>ErRoR</mark> occurred <mark>ERROR</mark> again");
});

test("escapeAndHighlight: multiple non-overlapping matches, each wrapped independently", () => {
    assert.equal(escapeAndHighlight("aaaa", "aa"), "<mark>aa</mark><mark>aa</mark>");
});

test("escapeAndHighlight: ampersand match highlighted as its escaped entity, not corrupted", () => {
    const out = escapeAndHighlight("A & B & C", "&");
    assert.equal(out, "A <mark>&amp;</mark> B <mark>&amp;</mark> C");
});

test("escapeAndHighlight: quotes inside a match are escaped inside the <mark>", () => {
    const out = escapeAndHighlight('He said "hello" and left', '"hello"');
    assert.equal(out, "He said <mark>&quot;hello&quot;</mark> and left");
});

test("escapeAndHighlight: <script> payload with a matching query -- no live <script> tag survives, only escaped text + our own literal <mark>", () => {
    const out = escapeAndHighlight("<script>alert(1)</script>", "script");
    assert.equal(out, "&lt;<mark>script</mark>&gt;alert(1)&lt;/<mark>script</mark>&gt;");
    assert.equal(out.indexOf("<script>"), -1);
    assert.equal(out.indexOf("</script>"), -1);
});

test("escapeAndHighlight: img onerror payload -- fully escaped even when the query matches inside it", () => {
    const out = escapeAndHighlight('<img src=x onerror=alert(1)>', "onerror");
    assert.equal(out.indexOf("<img"), -1);
    assert.equal(out.indexOf("onerror=alert"), -1); // the live, unescaped attribute text must not survive
    assert.ok(out.indexOf("<mark>onerror</mark>") !== -1);
});

test("escapeAndHighlight: a literal <mark></mark> already present in raw text is escaped, not treated as our own tag", () => {
    const out = escapeAndHighlight("real <mark>fake</mark> content", "fake");
    // Our own <mark> wraps "fake"; the raw text's OWN literal <mark>/</mark>
    // around it must come out as escaped entities, not real tags.
    assert.equal(out, "real &lt;mark&gt;<mark>fake</mark>&lt;/mark&gt; content");
});

test("escapeAndHighlight: attribute-breakout attempt (\" onmouseover=...) is fully escaped", () => {
    const out = escapeAndHighlight('" onmouseover="alert(1)', "onmouseover");
    assert.equal(out.indexOf('"'), -1); // no raw quote characters survive at all
    assert.ok(out.indexOf("<mark>onmouseover</mark>") !== -1);
});

test("escapeAndHighlight: query containing regex metacharacters is treated as a literal substring (no ReDoS/regex-injection surface)", () => {
    const out = escapeAndHighlight("price: $5.00 (cheap).*+?", "$5.00 (");
    assert.equal(out, "price: <mark>$5.00 (</mark>cheap).*+?");
});

test("escapeAndHighlight: unicode text highlights correctly", () => {
    assert.equal(escapeAndHighlight("héllo wörld", "wörld"), "héllo <mark>wörld</mark>");
});

test("escapeAndHighlight: null/undefined raw coerces to empty string, never throws", () => {
    assert.equal(escapeAndHighlight(null, "x"), "");
    assert.equal(escapeAndHighlight(undefined, "x"), "");
});

test("escapeAndHighlight: query longer than the text never matches, never throws", () => {
    assert.equal(escapeAndHighlight("hi", "this is a much longer query than the text"), "hi");
});

// -----------------------------------------------------------------------
// General safety invariant, run over a battery of adversarial payloads:
// after stripping ONLY the literal "<mark>"/"</mark>" substrings this
// file itself inserts, NO "<" or ">" character may survive anywhere in
// the output. This is the structural proof that escaping always happens
// BEFORE any tag is spliced in, for every payload/query combination, not
// just the handful of cases spot-checked above.
// -----------------------------------------------------------------------

function stripOurMarks(html) {
    return html.split("<mark>").join("").split("</mark>").join("");
}

const XSS_PAYLOADS = [
    "<script>alert(document.cookie)</script>",
    "<img src=x onerror=alert(1)>",
    "<svg onload=alert(1)>",
    "<a href=\"javascript:alert(1)\">click</a>",
    "'><script>alert(String.fromCharCode(88,83,83))</script>",
    "\"><img src=x onerror=alert(1)>",
    "<iframe src=\"javascript:alert(1)\"></iframe>",
    "<body onload=alert(1)>",
    "<div onclick=\"alert('xss')\">click me</div>",
    "&lt;script&gt;already escaped once&lt;/script&gt;",
    "<mark class=\"fake\">already has a mark tag</mark>",
    "plain line with no markup at all",
    "1737400000 [critical] [system] test-source: Title - <script>evil()</script>",
    "<<<<< nested angle brackets >>>>>",
    "unterminated < tag",
    "unterminated > tag",
    "\" onmouseover=\"alert(1)",
    "' onmouseover='alert(1)",
];

const QUERIES = ["script", "SCRIPT", "onerror", "<", ">", "\"", "'", "&", "mark", "xss", "nonexistent-query", ""];

test("escapeAndHighlight safety invariant: no unescaped '<' or '>' survives outside our own literal <mark> tags, across a battery of XSS payloads x queries", () => {
    let combinations = 0;
    XSS_PAYLOADS.forEach((payload) => {
        QUERIES.forEach((query) => {
            const out = escapeAndHighlight(payload, query);
            const stripped = stripOurMarks(out);
            assert.equal(
                stripped.indexOf("<"),
                -1,
                `payload=${JSON.stringify(payload)} query=${JSON.stringify(query)} -> output=${JSON.stringify(out)} leaked an unescaped '<'`
            );
            assert.equal(
                stripped.indexOf(">"),
                -1,
                `payload=${JSON.stringify(payload)} query=${JSON.stringify(query)} -> output=${JSON.stringify(out)} leaked an unescaped '>'`
            );
            combinations += 1;
        });
    });
    // Sanity: the loop actually ran a meaningful number of combinations.
    assert.equal(combinations, XSS_PAYLOADS.length * QUERIES.length);
});

// =======================================================================
// renderLineContent -- testable against a plain stand-in object (no DOM)
// =======================================================================

test("renderLineContent: no query -> textContent set, innerHTML left untouched", () => {
    const el = { textContent: "", innerHTML: "SHOULD_NOT_CHANGE" };
    renderLineContent(el, "<script>alert(1)</script>", "");
    assert.equal(el.textContent, "<script>alert(1)</script>");
    assert.equal(el.innerHTML, "SHOULD_NOT_CHANGE");
});

test("renderLineContent: active query -> innerHTML set to escaped+highlighted markup, textContent untouched", () => {
    const el = { textContent: "SHOULD_NOT_CHANGE", innerHTML: "" };
    renderLineContent(el, "<script>alert(1)</script>", "script");
    assert.equal(el.textContent, "SHOULD_NOT_CHANGE");
    assert.equal(el.innerHTML, "&lt;<mark>script</mark>&gt;alert(1)&lt;/<mark>script</mark>&gt;");
});

// -----------------------------------------------------------------------
// Static regression guard: exactly one `.innerHTML =` assignment exists
// in the whole file (renderLineContent's) -- catches any future call site
// that starts building raw HTML strings elsewhere in this file.
// -----------------------------------------------------------------------

test("static check: log-viewer.js contains exactly one '.innerHTML =' assignment expression", () => {
    const src = fs.readFileSync(logViewerJsPath, "utf8");
    const assignments = src.match(/\.innerHTML\s*=(?!=)/g) || [];
    assert.equal(assignments.length, 1, "expected exactly one innerHTML assignment (renderLineContent's) -- found: " + assignments.length);
});

// =======================================================================
// debounce
// =======================================================================

test("debounce: rapid calls collapse into a single invocation with the LAST call's arguments", async () => {
    let calls = [];
    const fn = debounce((v) => calls.push(v), 20);
    fn("a");
    fn("b");
    fn("c");
    assert.equal(calls.length, 0); // nothing fires synchronously
    await new Promise((resolve) => setTimeout(resolve, 60));
    assert.deepEqual(calls, ["c"]);
});

test("debounce: two well-separated call bursts each fire once", async () => {
    let calls = 0;
    const fn = debounce(() => {
        calls += 1;
    }, 15);
    fn();
    await new Promise((resolve) => setTimeout(resolve, 40));
    fn();
    await new Promise((resolve) => setTimeout(resolve, 40));
    assert.equal(calls, 2);
});

// =======================================================================
// URL builders (Task 21's exact API contract)
// =======================================================================

test("buildListUrl", () => {
    assert.equal(buildListUrl(false), "/api/logs/list?include_ops=false");
    assert.equal(buildListUrl(true), "/api/logs/list?include_ops=true");
});

test("buildStatUrl encodes the filename and appends stat=1", () => {
    assert.equal(buildStatUrl("file/revysmc/2026-07-20.log"), "/api/logs/tail?file=file%2Frevysmc%2F2026-07-20.log&stat=1");
});

test("buildTailUrl encodes the filename and includes position", () => {
    assert.equal(buildTailUrl("ops/alert-router.log", 1024), "/api/logs/tail?file=ops%2Falert-router.log&position=1024");
});

test("buildChunkUrl encodes the filename and includes end", () => {
    assert.equal(buildChunkUrl("file/x.log", 5000), "/api/logs/chunk?file=file%2Fx.log&end=5000");
});

// =======================================================================
// fetchStat / pollOnce / fetchChunkStep (mocked fetchFn -- no real network)
// =======================================================================

test("fetchStat: success returns {type:'ok', fileSize}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(200, { file_size: 12345 })]);
    const result = await fetchStat(fetchFn, "file/x.log");
    assert.deepEqual(result, { type: "ok", fileSize: 12345 });
});

test("fetchStat: 403 returns {type:'access_denied'}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(403, { status: "error", message: "denied" })]);
    const result = await fetchStat(fetchFn, "file/x.log");
    assert.deepEqual(result, { type: "access_denied" });
});

test("fetchStat: malformed body returns {type:'error', ...}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(200, { unexpected: true })]);
    const result = await fetchStat(fetchFn, "file/x.log");
    assert.equal(result.type, "error");
});

test("pollOnce: success with lines returns {type:'ok', lines, position, fileSize, eof}", async () => {
    const fetchFn = mockFetchSequence([
        fakeResponse(200, { status: "success", lines: ["1 [info] a", "2 [info] b"], position: 42, file_size: 100, eof: true }),
    ]);
    const result = await pollOnce(fetchFn, "file/x.log", 0);
    assert.deepEqual(result, { type: "ok", lines: ["1 [info] a", "2 [info] b"], position: 42, fileSize: 100, eof: true });
});

test("pollOnce: 403 returns {type:'access_denied'}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(403, { status: "error" })]);
    const result = await pollOnce(fetchFn, "file/x.log", 0);
    assert.deepEqual(result, { type: "access_denied" });
});

test("pollOnce: server error body (status:'error') returns {type:'error'}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(500, { status: "error", message: "boom" })]);
    const result = await pollOnce(fetchFn, "file/x.log", 0);
    assert.equal(result.type, "error");
});

test("pollOnce: a rejected fetchFn resolves (not rejects) with {type:'error'}", async () => {
    const fetchFn = () => Promise.reject(new Error("network down"));
    const result = await pollOnce(fetchFn, "file/x.log", 0);
    assert.equal(result.type, "error");
});

test("fetchChunkStep: success returns {type:'ok', start, lines, eof}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(200, { status: "success", start: 500, lines: ["10 [info] old"], eof: false })]);
    const result = await fetchChunkStep(fetchFn, "file/x.log", 1000);
    assert.deepEqual(result, { type: "ok", start: 500, lines: ["10 [info] old"], eof: false });
});

test("fetchChunkStep: 413 returns {type:'too_large', body}", async () => {
    const body = { status: "error", message: "Log entry too large to read in one chunk", error_detail: "..." };
    const fetchFn = mockFetchSequence([fakeResponse(413, body)]);
    const result = await fetchChunkStep(fetchFn, "file/x.log", 1000);
    assert.deepEqual(result, { type: "too_large", body: body });
});

test("fetchChunkStep: 403 returns {type:'access_denied'}", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(403, { status: "error" })]);
    const result = await fetchChunkStep(fetchFn, "file/x.log", 1000);
    assert.deepEqual(result, { type: "access_denied" });
});

// =======================================================================
// computeProgressPercent / formatBackfillProgress / computeCounterText
// =======================================================================

test("computeProgressPercent: nothing loaded yet -> 0%", () => {
    assert.equal(computeProgressPercent(1000, 1000), 0);
});

test("computeProgressPercent: fully loaded (start=0) -> 100%", () => {
    assert.equal(computeProgressPercent(1000, 0), 100);
});

test("computeProgressPercent: halfway -> 50%", () => {
    assert.equal(computeProgressPercent(1000, 500), 50);
});

test("computeProgressPercent: zero-size file -> 100% (nothing to load)", () => {
    assert.equal(computeProgressPercent(0, 0), 100);
});

test("formatBackfillProgress: loading/done/too_large/error/access_denied statuses", () => {
    assert.equal(formatBackfillProgress(42, "loading"), "Loading history: 42%");
    assert.equal(formatBackfillProgress(100, "done"), "History fully loaded");
    assert.ok(formatBackfillProgress(17, "too_large").indexOf("17%") !== -1);
    assert.ok(formatBackfillProgress(17, "too_large").toLowerCase().indexOf("too large") !== -1);
    assert.ok(formatBackfillProgress(0, "error").indexOf("error") !== -1);
    assert.ok(formatBackfillProgress(0, "access_denied").toLowerCase().indexOf("access denied") !== -1);
});

test("computeCounterText formats 'N matches / M lines'", () => {
    assert.equal(computeCounterText(312, 48203), "312 matches / 48203 lines");
    assert.equal(computeCounterText(0, 0), "0 matches / 0 lines");
});

// =======================================================================
// runBackfill orchestration (mocked fetchFn -- proves the backward-chunk
// loop's progress math, 413/403 stopping behavior, and generation
// invalidation, all without a real network or DOM)
// =======================================================================

test("runBackfill: zero-size file short-circuits straight to 100% + done, no fetch calls", async () => {
    const fetchFn = mockFetchSequence([]);
    const store = new LogStore();
    const progressCalls = [];
    let done = false;
    await runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 0,
        store,
        isCurrentGeneration: () => true,
        onProgress: (p) => progressCalls.push(p),
        onDone: () => {
            done = true;
        },
        onTooLarge: () => assert.fail("should not be called"),
        onAccessDenied: () => assert.fail("should not be called"),
        onError: () => assert.fail("should not be called"),
    });
    assert.deepEqual(progressCalls, [100]);
    assert.equal(done, true);
    assert.equal(store.length, 0);
});

test("runBackfill: three successful chunks walking to eof -- store ends up fully chronological, progress increases to 100", async () => {
    const fetchFn = mockFetchSequence([
        fakeResponse(200, { status: "success", start: 700, lines: ["700 [info] c"], eof: false }),
        fakeResponse(200, { status: "success", start: 300, lines: ["300 [info] b"], eof: false }),
        fakeResponse(200, { status: "success", start: 0, lines: ["0 [info] a"], eof: true }),
    ]);
    const store = new LogStore();
    const progressCalls = [];
    let done = false;
    await runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 1000,
        store,
        isCurrentGeneration: () => true,
        onProgress: (p) => progressCalls.push(p),
        onDone: () => {
            done = true;
        },
        onTooLarge: () => assert.fail("should not be called"),
        onAccessDenied: () => assert.fail("should not be called"),
        onError: () => assert.fail("should not be called"),
    });
    assert.equal(done, true);
    assert.deepEqual(store.entries.map((e) => e.raw), ["0 [info] a", "300 [info] b", "700 [info] c"]);
    assert.deepEqual(progressCalls, [30, 70, 100]);
    assert.equal(fetchFn.calls.length, 3);
});

test("runBackfill: a 413 mid-loop stops paging further back, keeps earlier-loaded chunks, calls onTooLarge exactly once, never retries", async () => {
    const tooLargeBody = { status: "error", message: "Log entry too large to read in one chunk" };
    const fetchFn = mockFetchSequence([
        fakeResponse(200, { status: "success", start: 700, lines: ["700 [info] c"], eof: false }),
        fakeResponse(413, tooLargeBody),
    ]);
    const store = new LogStore();
    let tooLargeCalls = 0;
    let doneCalled = false;
    await runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 1000,
        store,
        isCurrentGeneration: () => true,
        onProgress: () => {},
        onDone: () => {
            doneCalled = true;
        },
        onTooLarge: (body) => {
            tooLargeCalls += 1;
            assert.deepEqual(body, tooLargeBody);
        },
        onAccessDenied: () => assert.fail("should not be called"),
        onError: () => assert.fail("should not be called"),
    });
    assert.equal(tooLargeCalls, 1);
    assert.equal(doneCalled, false);
    assert.deepEqual(store.entries.map((e) => e.raw), ["700 [info] c"]);
    assert.equal(fetchFn.calls.length, 2); // stopped, never issued a 3rd request
});

test("runBackfill: a 403 stops the loop immediately and calls onAccessDenied, not onError", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(403, { status: "error" })]);
    const store = new LogStore();
    let accessDeniedCalls = 0;
    await runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 1000,
        store,
        isCurrentGeneration: () => true,
        onProgress: () => {},
        onDone: () => assert.fail("should not be called"),
        onTooLarge: () => assert.fail("should not be called"),
        onAccessDenied: () => {
            accessDeniedCalls += 1;
        },
        onError: () => assert.fail("should not be called"),
    });
    assert.equal(accessDeniedCalls, 1);
    assert.equal(store.length, 0);
});

test("runBackfill: generation invalidation after the first chunk stops the loop -- no further fetch calls, no callbacks fire", async () => {
    let generationValid = true;
    const fetchFn = mockFetchSequence([
        fakeResponse(200, { status: "success", start: 700, lines: ["700 [info] c"], eof: false }),
        fakeResponse(200, { status: "success", start: 300, lines: ["THIS SHOULD NEVER BE FETCHED"], eof: false }),
    ]);
    const store = new LogStore();
    let onDoneCalled = false;
    const backfillPromise = runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 1000,
        store,
        isCurrentGeneration: () => generationValid,
        onProgress: () => {
            // Simulate the file being switched (generation bump) right after
            // the first chunk's progress callback fires.
            generationValid = false;
        },
        onDone: () => {
            onDoneCalled = true;
        },
        onTooLarge: () => assert.fail("should not be called"),
        onAccessDenied: () => assert.fail("should not be called"),
        onError: () => assert.fail("should not be called"),
    });
    await backfillPromise;
    assert.equal(onDoneCalled, false);
    assert.equal(fetchFn.calls.length, 1); // the second, stale chunk was never requested
    assert.deepEqual(store.entries.map((e) => e.raw), ["700 [info] c"]); // first chunk's mutation is NOT rolled back -- a discarded store, harmless
});

test("runBackfill: a non-progressing response (start >= end, no eof) stops defensively instead of infinite-looping", async () => {
    const fetchFn = mockFetchSequence([fakeResponse(200, { status: "success", start: 1000, lines: [], eof: false })]);
    const store = new LogStore();
    let done = false;
    await runBackfill({
        fetchFn,
        file: "x.log",
        fileSize: 1000,
        store,
        isCurrentGeneration: () => true,
        onProgress: () => {},
        onDone: () => {
            done = true;
        },
        onTooLarge: () => assert.fail("should not be called"),
        onAccessDenied: () => assert.fail("should not be called"),
        onError: () => assert.fail("should not be called"),
    });
    assert.equal(done, true);
    assert.equal(fetchFn.calls.length, 1);
});

// =======================================================================
// Preference persistence (injectable storage)
// =======================================================================

test("mergeFilterPreferences: only merges known boolean keys, ignores garbage, keeps defaults for missing keys", () => {
    const merged = mergeFilterPreferences({ critical: false, info: true, bogus: "x", warning: "not-a-bool" }, DEFAULT_LEVEL_FILTERS);
    assert.deepEqual(merged, { critical: false, warning: true, notice: true, info: true, debug: false });
});

test("readPersistedFilters: no storage entry -> defaults", () => {
    const storage = makeFakeStorage();
    assert.deepEqual(readPersistedFilters(storage), DEFAULT_LEVEL_FILTERS);
});

test("readPersistedFilters: valid saved JSON merges over defaults", () => {
    const storage = makeFakeStorage({ logViewer_levelFilters: JSON.stringify({ debug: true }) });
    assert.deepEqual(readPersistedFilters(storage), { critical: true, warning: true, notice: true, info: false, debug: true });
});

test("readPersistedFilters: corrupt JSON degrades to defaults, does not throw", () => {
    const storage = makeFakeStorage({ logViewer_levelFilters: "{not json" });
    assert.deepEqual(readPersistedFilters(storage), DEFAULT_LEVEL_FILTERS);
});

test("readPersistedFilters: a throwing storage degrades to defaults, does not throw", () => {
    assert.deepEqual(readPersistedFilters(makeThrowingStorage()), DEFAULT_LEVEL_FILTERS);
});

test("persistFilters round-trips through a fake storage", () => {
    const storage = makeFakeStorage();
    persistFilters({ critical: false, warning: true, notice: true, info: true, debug: false }, storage);
    assert.deepEqual(readPersistedFilters(storage), { critical: false, warning: true, notice: true, info: true, debug: false });
});

test("persistFilters: a throwing storage degrades silently, does not throw", () => {
    assert.doesNotThrow(() => persistFilters(DEFAULT_LEVEL_FILTERS, makeThrowingStorage()));
});

test("readPersistedOpsToggle: defaults to false when unset", () => {
    assert.equal(readPersistedOpsToggle(makeFakeStorage()), false);
});

test("readPersistedOpsToggle/persistOpsToggle round-trip", () => {
    const storage = makeFakeStorage();
    persistOpsToggle(true, storage);
    assert.equal(readPersistedOpsToggle(storage), true);
    persistOpsToggle(false, storage);
    assert.equal(readPersistedOpsToggle(storage), false);
});

test("readPersistedOpsToggle: a throwing storage degrades to false, does not throw", () => {
    assert.equal(readPersistedOpsToggle(makeThrowingStorage()), false);
});
