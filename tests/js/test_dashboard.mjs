// Tests for src/static/js/dashboard.js (Task 25).
//
// Same rationale/harness as tests/js/test_common.mjs (Task 24): Node's
// built-in `node:test`/`node:assert`, no framework, no package.json --
// see that file's own header comment for the full "why" (this repo has no
// JS test runner today, and this is a framework-free static file loaded
// via a plain <script> tag).
//
// dashboard.js's own header comment documents its testability split: every
// function ABOVE its "DOM rendering / wiring" section is pure (no
// `document`/`window` access) and exported via `module.exports`, exactly
// like common.js. Everything below that point only ever runs through
// `init()`, which itself only fires when `document` exists -- so
// `require()`-ing this module under plain Node (no DOM at all) is
// side-effect-free, and this file only exercises the pure/exported half.
//
// What this file deliberately does NOT (and per the task brief, cannot)
// test without a real browser/DOM: actual card/table/modal rendering, the
// native <dialog> open/close behavior, `scrollIntoView`, `localStorage`
// integration end-to-end in a real browser, CSS (grid/pulse
// animation/<768px full-screen dialog) -- those are covered by the visual
// structure of dashboard.html/dashboard.css and manual verification,
// documented as a known gap in this task's report rather than silently
// skipped.
//
// Run with:  node tests/js/test_dashboard.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const dashboardJsPath = path.join(__dirname, "..", "..", "src", "static", "js", "dashboard.js");

const require = createRequire(import.meta.url);
const AlertManagerDashboard = require(dashboardJsPath);

const {
    VALID_WINDOWS,
    DEFAULT_WINDOW,
    DEFAULT_LEVELS,
    MESSAGE_TRUNCATE_LENGTH,
    LEVELS_ORDER,
    isValidWindow,
    readPersistedWindow,
    persistWindow,
    normalizeLevels,
    readPersistedLevels,
    persistLevels,
    toggleLevel,
    levelsLabel,
    clientKey,
    sourceKey,
    isGroupOpen,
    recordCollapseChoice,
    readCollapseState,
    persistCollapseState,
    truncateMessage,
    buildSummaryUrl,
    buildGroupsUrl,
    clientDisplayValue,
    DashboardApiError,
    fetchSummary,
    fetchGroups,
} = AlertManagerDashboard;

// ---------------------------------------------------------------------
// A tiny in-memory Storage stand-in (localStorage's actual shape), so
// persistence logic is testable without a real `window.localStorage`.
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

// ---------------------------------------------------------------------
// Window enum -- must match gateway/dashboard/routes.py's `_WINDOW_SECONDS`
// exactly (this is the client-side half of the brief's "validate
// client-side against this exact list too, matching the server's enum").
// ---------------------------------------------------------------------

const SERVER_WINDOW_ENUM = ["1h", "6h", "12h", "24h", "7d", "14d", "30d", "all"];

test("VALID_WINDOWS matches the server's exact 8-value window enum, in order", () => {
    assert.deepEqual(VALID_WINDOWS, SERVER_WINDOW_ENUM);
});

test("DEFAULT_WINDOW is 24h, matching the server's DEFAULT_WINDOW", () => {
    assert.equal(DEFAULT_WINDOW, "24h");
});

test("DEFAULT_LEVELS is critical only, matching the server's DEFAULT_LEVELS", () => {
    assert.deepEqual(DEFAULT_LEVELS, ["critical"]);
});

test("MESSAGE_TRUNCATE_LENGTH is 120 per the brief", () => {
    assert.equal(MESSAGE_TRUNCATE_LENGTH, 120);
});

test("LEVELS_ORDER is critical->warning->notice->info->debug, matching CLAUDE.md's canonical order", () => {
    assert.deepEqual(LEVELS_ORDER, ["critical", "warning", "notice", "info", "debug"]);
});

test("isValidWindow accepts every enum value", () => {
    for (const w of SERVER_WINDOW_ENUM) {
        assert.equal(isValidWindow(w), true, `expected ${w} to be valid`);
    }
});

test("isValidWindow rejects values the server would 400 on", () => {
    for (const bad of ["2h", "24H", "", "1 day", "week", "365d", null, undefined, "ALL"]) {
        assert.equal(isValidWindow(bad), false, `expected ${JSON.stringify(bad)} to be invalid`);
    }
});

// ---------------------------------------------------------------------
// Window persistence: valid / invalid / missing localStorage value
// ---------------------------------------------------------------------

test("readPersistedWindow returns the stored value when it is valid", () => {
    const storage = makeFakeStorage({ dashboard_window: "7d" });
    assert.equal(readPersistedWindow(storage), "7d");
});

test("readPersistedWindow falls back to DEFAULT_WINDOW when the stored value is invalid", () => {
    const storage = makeFakeStorage({ dashboard_window: "bogus" });
    assert.equal(readPersistedWindow(storage), DEFAULT_WINDOW);
});

test("readPersistedWindow falls back to DEFAULT_WINDOW when nothing is stored", () => {
    const storage = makeFakeStorage({});
    assert.equal(readPersistedWindow(storage), DEFAULT_WINDOW);
});

test("readPersistedWindow falls back to DEFAULT_WINDOW when no storage is available at all", () => {
    assert.equal(readPersistedWindow(null), DEFAULT_WINDOW);
});

test("readPersistedWindow degrades to DEFAULT_WINDOW instead of throwing when storage access throws", () => {
    const storage = makeThrowingStorage();
    assert.equal(readPersistedWindow(storage), DEFAULT_WINDOW);
});

test("persistWindow writes a valid value under the exact 'dashboard_window' key", () => {
    const storage = makeFakeStorage({});
    persistWindow("12h", storage);
    assert.equal(storage.getItem("dashboard_window"), "12h");
});

test("persistWindow is a no-op (does not throw, does not write) for an invalid value", () => {
    const storage = makeFakeStorage({});
    persistWindow("bogus", storage);
    assert.equal(storage.getItem("dashboard_window"), null);
});

test("persistWindow degrades silently instead of throwing when storage access throws", () => {
    const storage = makeThrowingStorage();
    assert.doesNotThrow(() => persistWindow("6h", storage));
});

test("round-trip: persistWindow then readPersistedWindow returns the same value", () => {
    const storage = makeFakeStorage({});
    persistWindow("30d", storage);
    assert.equal(readPersistedWindow(storage), "30d");
});

// ---------------------------------------------------------------------
// Level-filter selection (cards are multi-select toggles)
// ---------------------------------------------------------------------

test("normalizeLevels drops unknown values, dedupes, and returns canonical order", () => {
    assert.deepEqual(normalizeLevels(["debug", "bogus", "critical", "debug"]), ["critical", "debug"]);
    assert.deepEqual(normalizeLevels(null), []);
});

test("readPersistedLevels defaults to critical only when nothing is stored", () => {
    assert.deepEqual(readPersistedLevels(makeFakeStorage()), ["critical"]);
});

test("readPersistedLevels returns the stored selection in canonical order", () => {
    const storage = makeFakeStorage({ dashboard_levels: JSON.stringify(["info", "warning"]) });
    assert.deepEqual(readPersistedLevels(storage), ["warning", "info"]);
});

test("readPersistedLevels honors an explicitly stored empty selection", () => {
    assert.deepEqual(readPersistedLevels(makeFakeStorage({ dashboard_levels: "[]" })), []);
});

test("readPersistedLevels falls back to the default on garbage or a non-array", () => {
    assert.deepEqual(readPersistedLevels(makeFakeStorage({ dashboard_levels: "{not json" })), ["critical"]);
    assert.deepEqual(readPersistedLevels(makeFakeStorage({ dashboard_levels: '"critical"' })), ["critical"]);
});

test("readPersistedLevels degrades to the default instead of throwing when storage throws", () => {
    assert.deepEqual(readPersistedLevels(makeThrowingStorage()), ["critical"]);
});

test("persistLevels writes normalized JSON under 'dashboard_levels' and round-trips", () => {
    const storage = makeFakeStorage();
    persistLevels(["debug", "critical", "nope"], storage);
    assert.equal(storage._data.get("dashboard_levels"), '["critical","debug"]');
    assert.deepEqual(readPersistedLevels(storage), ["critical", "debug"]);
});

test("persistLevels degrades silently when storage throws", () => {
    assert.doesNotThrow(() => persistLevels(["critical"], makeThrowingStorage()));
});

test("toggleLevel adds a missing level in canonical position and removes a present one", () => {
    assert.deepEqual(toggleLevel(["critical", "info"], "warning"), ["critical", "warning", "info"]);
    assert.deepEqual(toggleLevel(["critical", "warning"], "critical"), ["warning"]);
    assert.deepEqual(toggleLevel(["critical"], "critical"), []);
});

test("levelsLabel renders a human list in canonical order", () => {
    assert.equal(levelsLabel(["warning", "critical"]), "Critical, Warning");
    assert.equal(levelsLabel([]), "");
});

// ---------------------------------------------------------------------
// Group collapse state
// ---------------------------------------------------------------------

test("clientKey/sourceKey are distinct namespaces and unambiguous", () => {
    assert.equal(clientKey("acme-co"), "c:acme-co");
    assert.notEqual(sourceKey("a", "b:c"), sourceKey("a:b", "c"));
    assert.notEqual(clientKey("x"), sourceKey("x", ""));
});

test("isGroupOpen defaults to open only when the group has matching rows", () => {
    assert.equal(isGroupOpen("c:a", 3, {}), true);
    assert.equal(isGroupOpen("c:a", 0, {}), false);
});

test("isGroupOpen prefers an explicit user choice over the default", () => {
    assert.equal(isGroupOpen("c:a", 3, { "c:a": false }), false);
    assert.equal(isGroupOpen("c:a", 0, { "c:a": true }), true);
});

test("recordCollapseChoice stores only choices that differ from the default", () => {
    assert.deepEqual(recordCollapseChoice({}, "c:a", false, 3), { "c:a": false });
    assert.deepEqual(recordCollapseChoice({}, "c:a", true, 0), { "c:a": true });
    // Matching the default clears any explicit choice (no-op for the
    // programmatic `toggle` event fired on initial render).
    assert.deepEqual(recordCollapseChoice({ "c:a": false }, "c:a", true, 3), {});
    assert.deepEqual(recordCollapseChoice({}, "c:a", true, 3), {});
});

test("recordCollapseChoice does not mutate its input", () => {
    const before = { "c:a": false };
    recordCollapseChoice(before, "c:b", false, 2);
    assert.deepEqual(before, { "c:a": false });
});

test("collapse state round-trips through storage and degrades on garbage/throws", () => {
    const storage = makeFakeStorage();
    persistCollapseState({ "c:a": false, [sourceKey("a", "web")]: true }, storage);
    assert.deepEqual(readCollapseState(storage), { "c:a": false, [sourceKey("a", "web")]: true });
    assert.deepEqual(readCollapseState(makeFakeStorage({ dashboard_collapse: "[1,2]" })), {});
    assert.deepEqual(readCollapseState(makeFakeStorage({ dashboard_collapse: "nope" })), {});
    assert.deepEqual(readCollapseState(makeThrowingStorage()), {});
    assert.doesNotThrow(() => persistCollapseState({}, makeThrowingStorage()));
});

// ---------------------------------------------------------------------
// 120-char message truncation
// ---------------------------------------------------------------------

test("truncateMessage leaves a message shorter than 120 chars unchanged", () => {
    const msg = "x".repeat(50);
    assert.equal(truncateMessage(msg), msg);
});

test("truncateMessage leaves a message of exactly 120 chars unchanged (no ellipsis)", () => {
    const msg = "x".repeat(120);
    const result = truncateMessage(msg);
    assert.equal(result, msg);
    assert.equal(result.length, 120);
});

test("truncateMessage truncates a message longer than 120 chars to 120 + '...'", () => {
    const msg = "x".repeat(150);
    const result = truncateMessage(msg);
    assert.equal(result, "x".repeat(120) + "...");
    assert.equal(result.length, 123);
});

test("truncateMessage truncates a message of 121 chars (one over the boundary)", () => {
    const msg = "x".repeat(121);
    const result = truncateMessage(msg);
    assert.equal(result, "x".repeat(120) + "...");
});

test("truncateMessage respects a custom maxLength", () => {
    assert.equal(truncateMessage("hello world", 5), "hello...");
});

test("truncateMessage coerces null/undefined to an empty string instead of throwing", () => {
    assert.equal(truncateMessage(null), "");
    assert.equal(truncateMessage(undefined), "");
});

// ---------------------------------------------------------------------
// clientDisplayValue
// ---------------------------------------------------------------------

test("clientDisplayValue labels a null/empty/missing client as '(no client)'", () => {
    assert.equal(clientDisplayValue(null), "(no client)");
    assert.equal(clientDisplayValue(undefined), "(no client)");
    assert.equal(clientDisplayValue(""), "(no client)");
});

test("clientDisplayValue passes through a real client name unchanged", () => {
    assert.equal(clientDisplayValue("revysmc"), "revysmc");
});

// ---------------------------------------------------------------------
// URL / query-param construction (mocked-fetch-adjacent: pure builders)
// ---------------------------------------------------------------------

test("buildSummaryUrl constructs the exact summary endpoint with window param", () => {
    assert.equal(buildSummaryUrl("24h"), "/api/dashboard/summary?window=24h");
    assert.equal(buildSummaryUrl("7d"), "/api/dashboard/summary?window=7d");
});

test("buildGroupsUrl sends window plus canonical, comma-joined levels", () => {
    assert.equal(buildGroupsUrl("24h", ["critical"]), "/api/dashboard/groups?window=24h&levels=critical");
    assert.equal(
        buildGroupsUrl("7d", ["info", "critical"]),
        "/api/dashboard/groups?window=7d&levels=critical%2Cinfo"
    );
});

test("buildGroupsUrl always sends levels, even empty (empty != server default)", () => {
    assert.equal(buildGroupsUrl("1h", []), "/api/dashboard/groups?window=1h&levels=");
});

// ---------------------------------------------------------------------
// Mocked-fetch tests: verifying correct URL/query-param construction end
// to end through fetchSummary/fetchGroups (which internally call
// common.js's fetchWithTimeout -> global fetch).
// ---------------------------------------------------------------------

function withMockedFetch(handler, run) {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = handler;
    return Promise.resolve()
        .then(run)
        .finally(() => {
            globalThis.fetch = originalFetch;
        });
}

test("fetchSummary requests the exact summary URL for the given window", async () => {
    let capturedUrl = null;
    await withMockedFetch(
        async (url) => {
            capturedUrl = url;
            return new Response(JSON.stringify({ counts: {}, total: 0 }), { status: 200 });
        },
        async () => {
            await fetchSummary("6h");
        }
    );
    assert.equal(capturedUrl, "/api/dashboard/summary?window=6h");
});

test("fetchGroups requests the exact groups URL for the window and levels", async () => {
    let capturedUrl = null;
    await withMockedFetch(
        async (url) => {
            capturedUrl = url;
            return new Response(JSON.stringify({ clients: [] }), { status: 200 });
        },
        async () => {
            await fetchGroups("14d", ["warning", "critical"]);
        }
    );
    assert.equal(capturedUrl, "/api/dashboard/groups?window=14d&levels=critical%2Cwarning");
});

test("fetchSummary resolves with the parsed JSON body on a 200", async () => {
    let result;
    await withMockedFetch(
        async () =>
            new Response(JSON.stringify({ counts: { critical: 3 }, total: 3 }), { status: 200 }),
        async () => {
            result = await fetchSummary("24h");
        }
    );
    assert.deepEqual(result, { counts: { critical: 3 }, total: 3 });
});

test("fetchSummary rejects with a DashboardApiError carrying status+body on a 503 (DB-down shape from Task 22)", async () => {
    const dbDownBody = {
        status: "error",
        error: "database unavailable",
        message: "Dashboard data is temporarily unavailable; please try again shortly.",
        detail: "connection refused",
        timestamp: "2026-07-20T00:00:00Z",
    };
    let caught = null;
    await withMockedFetch(
        async () => new Response(JSON.stringify(dbDownBody), { status: 503 }),
        async () => {
            try {
                await fetchSummary("24h");
            } catch (e) {
                caught = e;
            }
        }
    );
    assert.ok(caught instanceof DashboardApiError);
    assert.equal(caught.status, 503);
    assert.equal(caught.body.error, "database unavailable");
    assert.equal(caught.message, dbDownBody.message);
});

test("fetchGroups rejects with a DashboardApiError on a 400 (bad window/levels)", async () => {
    let caught = null;
    await withMockedFetch(
        async () =>
            new Response(JSON.stringify({ status: "error", message: "invalid levels" }), { status: 400 }),
        async () => {
            try {
                await fetchGroups("24h", ["critical"]);
            } catch (e) {
                caught = e;
            }
        }
    );
    assert.ok(caught instanceof DashboardApiError);
    assert.equal(caught.status, 400);
    assert.equal(caught.message, "invalid levels");
});

// ---------------------------------------------------------------------
// Static XSS-safety net: dashboard.js must never build markup via
// innerHTML + interpolated/untrusted text. Every insertion point in this
// file uses textContent/createElement instead (see its own header
// comment, which legitimately mentions the word "innerHTML" in prose --
// so this checks for actual PROPERTY-ACCESS usage, e.g. `el.innerHTML =`
// or `x.innerHTML +=`, via regex, rather than a bare substring search
// that would false-positive on the documentation itself).
// ---------------------------------------------------------------------

const DANGEROUS_DOM_SINK_RE = /\.\s*(innerHTML|outerHTML)\s*(=|\+=)|\.\s*insertAdjacentHTML\s*\(/;

test("dashboard.js source contains zero actual innerHTML/outerHTML/insertAdjacentHTML sink usages", () => {
    const source = fs.readFileSync(dashboardJsPath, "utf8");
    const match = DANGEROUS_DOM_SINK_RE.exec(source);
    assert.equal(match, null, `found a raw-HTML DOM sink usage: ${match && match[0]}`);
});

test("(meta) the dangerous-sink regex itself actually detects a real innerHTML assignment", () => {
    // Guards the guard: proves DANGEROUS_DOM_SINK_RE isn't accidentally
    // unmatchable (e.g. a typo'd pattern that would make the test above
    // vacuously pass no matter what dashboard.js contains).
    assert.ok(DANGEROUS_DOM_SINK_RE.test('el.innerHTML = "<b>" + title + "</b>";'));
    assert.ok(DANGEROUS_DOM_SINK_RE.test("el.outerHTML += chunk;"));
    assert.ok(DANGEROUS_DOM_SINK_RE.test('el.insertAdjacentHTML("beforeend", html);'));
    assert.ok(!DANGEROUS_DOM_SINK_RE.test("// mentions innerHTML in a comment only"));
});
