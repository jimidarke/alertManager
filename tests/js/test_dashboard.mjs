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
    DEFAULT_LIMIT,
    MESSAGE_TRUNCATE_LENGTH,
    LEVELS_ORDER,
    isValidWindow,
    readPersistedWindow,
    persistWindow,
    nextOffset,
    hasMore,
    truncateMessage,
    buildSummaryUrl,
    buildCriticalUrl,
    clientDisplayValue,
    DashboardApiError,
    fetchSummary,
    fetchCritical,
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

test("DEFAULT_LIMIT is 50, matching the server's DEFAULT_LIMIT", () => {
    assert.equal(DEFAULT_LIMIT, 50);
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
// Pagination offset math
// ---------------------------------------------------------------------

test("nextOffset advances by the fixed page limit, not by rows actually returned", () => {
    assert.equal(nextOffset(0, 50), 50);
    assert.equal(nextOffset(50, 50), 100);
    assert.equal(nextOffset(100, 50), 150);
});

test("nextOffset works with a non-default limit too", () => {
    assert.equal(nextOffset(0, 25), 25);
    assert.equal(nextOffset(25, 25), 50);
});

test("hasMore is true while the offset already covered is less than total", () => {
    assert.equal(hasMore(50, 137), true);
    assert.equal(hasMore(100, 137), true);
});

test("hasMore is false once the offset reaches or exceeds total", () => {
    assert.equal(hasMore(137, 137), false);
    assert.equal(hasMore(150, 137), false);
});

test("hasMore is false for an empty result set (offset 0, total 0)", () => {
    assert.equal(hasMore(0, 0), false);
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

test("clientDisplayValue returns an em dash for null/empty/missing client", () => {
    assert.equal(clientDisplayValue(null), "—");
    assert.equal(clientDisplayValue(undefined), "—");
    assert.equal(clientDisplayValue(""), "—");
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

test("buildCriticalUrl constructs the exact critical endpoint with window/offset/limit params", () => {
    assert.equal(
        buildCriticalUrl("24h", 0, 50),
        "/api/dashboard/critical?window=24h&offset=0&limit=50"
    );
    assert.equal(
        buildCriticalUrl("7d", 50, 50),
        "/api/dashboard/critical?window=7d&offset=50&limit=50"
    );
    assert.equal(
        buildCriticalUrl("1h", 100, 25),
        "/api/dashboard/critical?window=1h&offset=100&limit=25"
    );
});

// ---------------------------------------------------------------------
// Mocked-fetch tests: verifying correct URL/query-param construction end
// to end through fetchSummary/fetchCritical (which internally call
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

test("fetchCritical requests the exact critical URL including offset/limit for pagination", async () => {
    let capturedUrl = null;
    await withMockedFetch(
        async (url) => {
            capturedUrl = url;
            return new Response(JSON.stringify({ alerts: [], total: 0 }), { status: 200 });
        },
        async () => {
            await fetchCritical("14d", 50, 50);
        }
    );
    assert.equal(capturedUrl, "/api/dashboard/critical?window=14d&offset=50&limit=50");
});

test("fetchCritical's second page (Load more) uses offset=limit, not offset=rows-returned", async () => {
    let capturedUrl = null;
    await withMockedFetch(
        async (url) => {
            capturedUrl = url;
            return new Response(JSON.stringify({ alerts: [], total: 0 }), { status: 200 });
        },
        async () => {
            const offset = nextOffset(0, DEFAULT_LIMIT);
            await fetchCritical("24h", offset, DEFAULT_LIMIT);
        }
    );
    assert.equal(capturedUrl, "/api/dashboard/critical?window=24h&offset=50&limit=50");
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

test("fetchCritical rejects with a DashboardApiError on a 400 (bad window/limit/offset)", async () => {
    let caught = null;
    await withMockedFetch(
        async () =>
            new Response(JSON.stringify({ status: "error", message: "limit must be >= 1" }), { status: 400 }),
        async () => {
            try {
                await fetchCritical("24h", 0, 0);
            } catch (e) {
                caught = e;
            }
        }
    );
    assert.ok(caught instanceof DashboardApiError);
    assert.equal(caught.status, 400);
    assert.equal(caught.message, "limit must be >= 1");
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
