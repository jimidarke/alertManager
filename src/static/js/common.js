/**
 * Alert Manager - Shared frontend utilities
 *
 * Loaded by layout.html on every session-authenticated page. Framework-free
 * (matches this project's existing src/static/js/log-viewer.js), and
 * exposed on `window.AlertManagerCommon` so later per-page scripts
 * (dashboard.js - Task 25, the log-viewer.js rewrite - Task 26, admin's
 * inline JS - Task 27) can call into it via a plain `<script>` include, no
 * bundler/module system involved.
 *
 * Contents:
 *   - fetchWithTimeout(url, options, timeoutMs): fetch() + AbortController
 *   - getCsrfToken(): reads the <meta name="csrf-token"> layout.html embeds
 *     (docs/OVERHAUL_PLAN.md 4.4: "layout embeds token in a meta tag")
 *   - Timezone helpers matching docs/TIMEZONE.md's established pattern
 *     (Intl.DateTimeFormat with a user-selected IANA zone, default
 *     America/Edmonton, persisted in localStorage under the same key
 *     src/static/js/log-viewer.js already uses)
 *   - timeAgo(epochSeconds): relative-time string ("3m ago")
 *   - escapeHtml(str): defensive escaping for untrusted text that ends up
 *     in an innerHTML-built string (prefer textContent directly when
 *     possible; this exists for the cases -- e.g. building a table row's
 *     HTML in one template-literal pass -- where that isn't practical)
 */

(function (window) {
    "use strict";

    // ------------------------------------------------------------------
    // fetchWithTimeout
    // ------------------------------------------------------------------

    /**
     * Wraps `fetch(url, options)` with an AbortController-based timeout.
     * Rejects with a DOMException named "AbortError" if `timeoutMs` elapses
     * before the request settles (the same error shape a caller-initiated
     * `AbortController.abort()` produces, so existing `catch` blocks that
     * check `error.name === 'AbortError'` work unmodified).
     *
     * If `options.signal` is already set, this function composes with it
     * rather than clobbering it: the request aborts on EITHER the caller's
     * own signal firing OR the timeout elapsing, whichever comes first.
     *
     * @param {string} url
     * @param {RequestInit} [options]
     * @param {number} [timeoutMs=10000]
     * @returns {Promise<Response>}
     */
    function fetchWithTimeout(url, options, timeoutMs) {
        options = options || {};
        timeoutMs = typeof timeoutMs === "number" ? timeoutMs : 10000;

        const controller = new AbortController();
        const callerSignal = options.signal;

        if (callerSignal) {
            if (callerSignal.aborted) {
                controller.abort();
            } else {
                callerSignal.addEventListener("abort", function () {
                    controller.abort();
                });
            }
        }

        const timer = setTimeout(function () {
            controller.abort();
        }, timeoutMs);

        const fetchOptions = Object.assign({}, options, { signal: controller.signal });

        return fetch(url, fetchOptions).finally(function () {
            clearTimeout(timer);
        });
    }

    // ------------------------------------------------------------------
    // CSRF token
    // ------------------------------------------------------------------

    /**
     * Reads the CSRF token layout.html embeds in
     * `<meta name="csrf-token" content="...">`. Returns `""` if the page
     * has no such tag (e.g. login.html, which is not extending layout.html
     * -- see that template's own comment for why -- and carries the token
     * on its form directly instead).
     *
     * @returns {string}
     */
    function getCsrfToken() {
        const meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute("content") || "" : "";
    }

    // ------------------------------------------------------------------
    // Timezone helpers -- docs/TIMEZONE.md's established pattern
    // ------------------------------------------------------------------

    const TIMEZONE_STORAGE_KEY = "alertManagerTimezone";
    const DEFAULT_TIMEZONE = "America/Edmonton";

    // Same North American timezone list docs/TIMEZONE.md documents for
    // src/static/js/log-viewer.js's own `northAmericanTimezones` dropdown --
    // kept here so any page (not just the log viewer) can offer the same
    // selector without duplicating the list a second time.
    const NORTH_AMERICAN_TIMEZONES = [
        { value: "America/St_Johns", label: "Newfoundland (NST/NDT)" },
        { value: "America/Halifax", label: "Atlantic (AST/ADT)" },
        { value: "America/Toronto", label: "Eastern (EST/EDT)" },
        { value: "America/Winnipeg", label: "Central (CST/CDT)" },
        { value: "America/Edmonton", label: "Mountain (MST/MDT)" },
        { value: "America/Vancouver", label: "Pacific (PST/PDT)" },
        { value: "America/New_York", label: "US Eastern (EST/EDT)" },
        { value: "America/Chicago", label: "US Central (CST/CDT)" },
        { value: "America/Denver", label: "US Mountain (MST/MDT)" },
        { value: "America/Phoenix", label: "US Arizona (MST)" },
        { value: "America/Los_Angeles", label: "US Pacific (PST/PDT)" },
        { value: "America/Anchorage", label: "Alaska (AKST/AKDT)" },
        { value: "Pacific/Honolulu", label: "Hawaii (HST)" },
        { value: "America/Mexico_City", label: "Mexico Central (CST/CDT)" },
        { value: "America/Cancun", label: "Mexico Eastern (EST)" },
        { value: "America/Mazatlan", label: "Mexico Mountain (MST/MDT)" },
        { value: "America/Tijuana", label: "Mexico Pacific (PST/PDT)" },
    ];

    /** Reads the persisted timezone preference, falling back to DEFAULT_TIMEZONE. */
    function getSelectedTimezone() {
        try {
            return window.localStorage.getItem(TIMEZONE_STORAGE_KEY) || DEFAULT_TIMEZONE;
        } catch (e) {
            // localStorage can throw in some privacy-restricted contexts
            // (e.g. Safari private browsing quirks) -- degrade to the
            // default rather than breaking the page.
            return DEFAULT_TIMEZONE;
        }
    }

    /** Persists a timezone preference (an IANA zone name, e.g. "America/Denver"). */
    function setSelectedTimezone(tz) {
        try {
            window.localStorage.setItem(TIMEZONE_STORAGE_KEY, tz);
        } catch (e) {
            // Same non-fatal degradation as getSelectedTimezone().
        }
    }

    /**
     * Formats a Unix epoch-seconds value as a localized time string in the
     * given (or currently-selected) IANA timezone -- the same
     * Intl.DateTimeFormat-based approach docs/TIMEZONE.md documents for
     * src/static/js/log-viewer.js's `formatLocalizedTime`.
     *
     * @param {number} epochSeconds
     * @param {string} [timezone] defaults to getSelectedTimezone()
     * @returns {string}
     */
    function formatLocalizedTime(epochSeconds, timezone) {
        const date = new Date(epochSeconds * 1000);
        const formatter = new Intl.DateTimeFormat("en-US", {
            timeZone: timezone || getSelectedTimezone(),
            month: "short",
            day: "numeric",
            hour: "numeric",
            minute: "2-digit",
            hour12: true,
        });
        return formatter.format(date);
    }

    // ------------------------------------------------------------------
    // timeAgo
    // ------------------------------------------------------------------

    /**
     * Formats a Unix epoch-seconds value as a short relative-time string
     * ("just now", "3m ago", "2h ago", "5d ago"). Falls back to an absolute
     * localized date (via formatLocalizedTime) once the value is more than
     * 30 days old, where "N days ago" stops being a useful readout.
     *
     * A future timestamp (clock skew, or a value that hasn't happened yet)
     * is treated as "just now" rather than printing a negative duration.
     *
     * @param {number} epochSeconds
     * @param {number} [nowSeconds] defaults to the current time -- injectable for tests
     * @returns {string}
     */
    function timeAgo(epochSeconds, nowSeconds) {
        const now = typeof nowSeconds === "number" ? nowSeconds : Date.now() / 1000;
        const diff = Math.floor(now - epochSeconds);

        if (diff < 0 || diff < 5) {
            return "just now";
        }
        if (diff < 60) {
            return diff + "s ago";
        }
        if (diff < 3600) {
            return Math.floor(diff / 60) + "m ago";
        }
        if (diff < 86400) {
            return Math.floor(diff / 3600) + "h ago";
        }
        if (diff < 2592000) {
            // < 30 days
            return Math.floor(diff / 86400) + "d ago";
        }
        return formatLocalizedTime(epochSeconds);
    }

    // ------------------------------------------------------------------
    // escapeHtml
    // ------------------------------------------------------------------

    const HTML_ESCAPE_MAP = {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    };

    /**
     * Defensive HTML-escaping for untrusted text (alert titles/messages/
     * metadata from the DB or log files) that must be interpolated into an
     * HTML string (e.g. a template literal assigned to `innerHTML`).
     * `textContent`/`createElement` remain the preferred approach wherever
     * they're practical -- this exists for the call sites (per
     * docs/OVERHAUL_PLAN.md 4.6's dashboard modal, 4.7's log-viewer
     * rendering) that build a chunk of markup in one pass.
     *
     * @param {*} value coerced to a string first (so a non-string, e.g.
     *   `null`/a number, never throws -- it's escaped as its string form)
     * @returns {string}
     */
    function escapeHtml(value) {
        return String(value).replace(/[&<>"']/g, function (ch) {
            return HTML_ESCAPE_MAP[ch];
        });
    }

    // ------------------------------------------------------------------
    // Export
    // ------------------------------------------------------------------

    const AlertManagerCommon = {
        fetchWithTimeout: fetchWithTimeout,
        getCsrfToken: getCsrfToken,
        NORTH_AMERICAN_TIMEZONES: NORTH_AMERICAN_TIMEZONES,
        DEFAULT_TIMEZONE: DEFAULT_TIMEZONE,
        getSelectedTimezone: getSelectedTimezone,
        setSelectedTimezone: setSelectedTimezone,
        formatLocalizedTime: formatLocalizedTime,
        timeAgo: timeAgo,
        escapeHtml: escapeHtml,
    };

    window.AlertManagerCommon = AlertManagerCommon;

    // Node/CommonJS export seam, used only by this repo's own test harness
    // (tests/js/test_common.js runs under plain Node, which has no
    // `window`) -- has zero effect in a browser, where `module` is
    // undefined and this block never executes.
    if (typeof module !== "undefined" && module.exports) {
        module.exports = AlertManagerCommon;
    }
})(typeof window !== "undefined" ? window : globalThis);
