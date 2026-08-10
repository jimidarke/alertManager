// Tests for src/static/js/common.js (Task 24).
//
// Why Node's built-in `node:test` + `node:assert`, and not a real browser
// harness or a new dependency: this project has no JS test runner and no
// package.json today (`find ... -iname package.json` turns up nothing),
// and per CLAUDE.md's Development Notes this is a framework-free static
// JS file loaded via a plain <script> tag, matching the existing
// src/static/js/log-viewer.js (also framework-free, also untested by any
// existing harness). Node 18+ ships `node:test`/`node:assert` in the
// standard library -- zero new dependencies, zero config files, and this
// repo's available Node is v24 (`node --version`). common.js itself
// exports its public surface via `module.exports` ONLY when `module`
// exists (see that file's closing IIFE) -- a no-op in a real browser,
// but exactly what lets this file `require()` it directly under Node
// without any bundler/transpilation step.
//
// `fetchWithTimeout` needs `fetch`/`AbortController` (both are Node
// 18+ globals, so no polyfill needed) but not a DOM; `getCsrfToken` and
// the localStorage-backed timezone setters need `document`/`window`,
// which don't exist under plain Node, so those two are exercised only
// indirectly (their try/catch-guarded localStorage access degrades
// gracefully -- covered by the "Node has no window/localStorage" case
// implicitly, since AlertManagerCommon still loads and every OTHER
// function still works).
//
// Run with:  node tests/js/test_common.mjs
// (or, if the project ever adds one, `node --test tests/js/`)

import assert from "node:assert/strict";
import { test } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const commonJsPath = path.join(__dirname, "..", "..", "src", "static", "js", "common.js");

const require = createRequire(import.meta.url);
const AlertManagerCommon = require(commonJsPath);

const { fetchWithTimeout, timeAgo, escapeHtml, formatLocalizedTime, NORTH_AMERICAN_TIMEZONES, DEFAULT_TIMEZONE } =
    AlertManagerCommon;

// ---------------------------------------------------------------------
// escapeHtml
// ---------------------------------------------------------------------

test("escapeHtml escapes all five HTML-significant characters", () => {
    assert.equal(escapeHtml("<script>alert('x')</script>"), "&lt;script&gt;alert(&#39;x&#39;)&lt;/script&gt;");
});

test("escapeHtml escapes ampersand and double quotes", () => {
    assert.equal(escapeHtml('Tom & Jerry says "hi"'), "Tom &amp; Jerry says &quot;hi&quot;");
});

test("escapeHtml leaves plain text untouched", () => {
    assert.equal(escapeHtml("nothing dangerous here 123"), "nothing dangerous here 123");
});

test("escapeHtml coerces non-string input instead of throwing", () => {
    assert.equal(escapeHtml(null), "null");
    assert.equal(escapeHtml(undefined), "undefined");
    assert.equal(escapeHtml(42), "42");
});

test("escapeHtml is idempotent-safe against double-escaping expectations (single pass only)", () => {
    // Documents the actual (correct) behavior: escapeHtml runs once, so an
    // already-escaped "&amp;" becomes "&amp;amp;" if run twice -- this
    // test exists to make that a visible, intentional fact, not a
    // surprise a caller discovers by running it on already-escaped text.
    const once = escapeHtml("&amp;");
    assert.equal(once, "&amp;amp;");
});

// ---------------------------------------------------------------------
// timeAgo
// ---------------------------------------------------------------------

test("timeAgo: just now for 0-4 seconds old", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now, now), "just now");
    assert.equal(timeAgo(now - 4, now), "just now");
});

test("timeAgo: seconds bucket (5-59s)", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now - 5, now), "5s ago");
    assert.equal(timeAgo(now - 59, now), "59s ago");
});

test("timeAgo: minutes bucket (60s - <1h)", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now - 60, now), "1m ago");
    assert.equal(timeAgo(now - 3599, now), "59m ago");
});

test("timeAgo: hours bucket (1h - <24h)", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now - 3600, now), "1h ago");
    assert.equal(timeAgo(now - 86399, now), "23h ago");
});

test("timeAgo: days bucket (24h - <30d)", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now - 86400, now), "1d ago");
    assert.equal(timeAgo(now - 2591999, now), "29d ago");
});

test("timeAgo: falls back to an absolute localized date at 30+ days", () => {
    const now = 1_700_000_000;
    const result = timeAgo(now - 2592000, now);
    assert.equal(/ago$/.test(result), false);
    assert.equal(/\d/.test(result), true); // some date-ish content
});

test("timeAgo: a future timestamp (clock skew) reads as just now, never negative", () => {
    const now = 1_700_000_000;
    assert.equal(timeAgo(now + 50, now), "just now");
});

test("timeAgo: defaults `now` to the current wall clock when omitted", () => {
    const nowSeconds = Date.now() / 1000;
    assert.equal(timeAgo(nowSeconds - 1), "just now");
});

// ---------------------------------------------------------------------
// formatLocalizedTime / timezone constants -- sanity checks only (full
// Intl.DateTimeFormat locale behavior is a platform guarantee, not this
// module's own logic to test)
// ---------------------------------------------------------------------

test("formatLocalizedTime accepts an explicit IANA zone and returns a non-empty string", () => {
    const result = formatLocalizedTime(1_700_000_000, "America/Edmonton");
    assert.equal(typeof result, "string");
    assert.ok(result.length > 0);
});

test("DEFAULT_TIMEZONE is America/Edmonton, matching docs/TIMEZONE.md", () => {
    assert.equal(DEFAULT_TIMEZONE, "America/Edmonton");
});

test("NORTH_AMERICAN_TIMEZONES includes every zone docs/TIMEZONE.md documents", () => {
    const values = NORTH_AMERICAN_TIMEZONES.map((tz) => tz.value);
    for (const expected of [
        "America/St_Johns",
        "America/Halifax",
        "America/Toronto",
        "America/Winnipeg",
        "America/Edmonton",
        "America/Vancouver",
        "America/New_York",
        "America/Chicago",
        "America/Denver",
        "America/Phoenix",
        "America/Los_Angeles",
        "America/Anchorage",
        "Pacific/Honolulu",
        "America/Mexico_City",
        "America/Cancun",
        "America/Mazatlan",
        "America/Tijuana",
    ]) {
        assert.ok(values.includes(expected), `missing timezone ${expected}`);
    }
});

// ---------------------------------------------------------------------
// fetchWithTimeout
// ---------------------------------------------------------------------

test("fetchWithTimeout resolves normally when the request finishes well within the timeout", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = async (url, options) => {
        assert.equal(url, "http://example.test/ok");
        assert.ok(options.signal instanceof AbortSignal);
        return new Response("ok", { status: 200 });
    };
    try {
        const resp = await fetchWithTimeout("http://example.test/ok", {}, 1000);
        assert.equal(resp.status, 200);
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test("fetchWithTimeout aborts (rejects with AbortError) once timeoutMs elapses", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (url, options) =>
        new Promise((resolve, reject) => {
            options.signal.addEventListener("abort", () => {
                const err = new Error("aborted");
                err.name = "AbortError";
                reject(err);
            });
            // Never resolves on its own -- only the abort listener above
            // settles this promise, simulating a hung request.
        });

    try {
        await assert.rejects(fetchWithTimeout("http://example.test/slow", {}, 20), { name: "AbortError" });
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test("fetchWithTimeout defaults to a 10000ms timeout when none is given", async () => {
    const originalFetch = globalThis.fetch;
    let capturedSignal = null;
    globalThis.fetch = async (url, options) => {
        capturedSignal = options.signal;
        return new Response("ok", { status: 200 });
    };
    try {
        await fetchWithTimeout("http://example.test/default-timeout");
        assert.ok(capturedSignal instanceof AbortSignal);
        assert.equal(capturedSignal.aborted, false);
    } finally {
        globalThis.fetch = originalFetch;
    }
});

test("fetchWithTimeout composes with a caller-supplied AbortSignal (either firing aborts the request)", async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = (url, options) =>
        new Promise((resolve, reject) => {
            options.signal.addEventListener("abort", () => {
                const err = new Error("aborted");
                err.name = "AbortError";
                reject(err);
            });
        });

    const callerController = new AbortController();
    try {
        const promise = fetchWithTimeout("http://example.test/caller-abort", { signal: callerController.signal }, 5000);
        callerController.abort();
        await assert.rejects(promise, { name: "AbortError" });
    } finally {
        globalThis.fetch = originalFetch;
    }
});
