/**
 * Alert Manager - Log viewer page script (Task 26, from-scratch rewrite)
 *
 * Drives templates/logs.html: the weekly-file dropdown, pause/resume,
 * level filter checkboxes, search box (debounced, `<mark>`-highlighted),
 * timezone selector (via common.js), the ops-logs toggle, and the
 * chunk/tail-aware polling+backfill loop against Task 21's
 * `/api/logs/list`, `/api/logs/tail`, `/api/logs/chunk`.
 *
 * --------------------------------------------------------------------
 * Architecture: LogStore / Renderer / Search / Poller
 * --------------------------------------------------------------------
 * `LogStore` (below) holds the FULL parsed line history in memory as a
 * chronological array of `{raw, ts, level}` objects -- level/timestamp are
 * parsed once at ingest (`parseLine`), never re-parsed per filter pass.
 * `LogStore.matchIndexes(levelFilters, query)` scans the complete in-memory
 * store and returns matching indexes for the current filter+search state --
 * this replaces the old DOM-line-cap (`MAX_DOM_LINES`) approach entirely;
 * see "Deleted" below.
 *
 * The "Renderer" section renders the newest 500 matches first and appends
 * 500 more when a bottom sentinel `<div>` scrolls into view
 * (`IntersectionObserver`) -- explicitly NOT true virtual scrolling: this
 * page prepends new content at the TOP (newest-first) and lines wrap to
 * variable heights, both of which fight fixed-row virtualization.
 *
 * The "Search" section debounces the search box 250ms, matches
 * case-insensitively against each line's `raw` text, ANDs that with the
 * active level filters (see `passesFilters`), and highlights matches with
 * `<mark>` -- see the "TEXT-INSERTION SAFETY" section below for exactly
 * how that's done safely.
 *
 * The "Poller/Backfill" section implements the initial-load sequence the
 * brief specifies: (1) `?stat=1` for `file_size`; (2) immediately start
 * 10s polling from `position=file_size` so brand-new lines appear from the
 * moment the page loads; (3) concurrently run a backward-chunk backfill
 * loop (`/api/logs/chunk`) from `file_size` down to `0`, streaming history
 * in behind the live tail. A single `state.generation` counter invalidates
 * any in-flight backfill/poll continuation when the user switches files
 * mid-load (each async step re-checks it before touching shared state).
 *
 * --------------------------------------------------------------------
 * Deleted: MAX_DOM_LINES, the DOM-pruning loop, the 10k-line warning
 * --------------------------------------------------------------------
 * The old log-viewer.js capped the DOM at `MAX_DOM_LINES` (5000), pruning
 * the oldest rendered lines every poll, and showed a warning banner past
 * `LINE_COUNT_WARNING_THRESHOLD` (10000) total lines processed. Both are
 * gone: `LogStore` holds the full history in memory (bounded only by the
 * chunk backfill's own 50MB widen ceiling -- gateway/logs/files.py's
 * `MAX_WIDEN_BYTES` -- surfaced client-side as a "too large" backfill-stop
 * message, not a truncation), and the DOM only ever holds
 * `state.renderCount` rows (grows by 500 as the user scrolls), not the
 * full store -- so there is nothing left to prune and nothing left to warn
 * about.
 *
 * --------------------------------------------------------------------
 * TEXT-INSERTION SAFETY (security-critical -- read before editing)
 * --------------------------------------------------------------------
 * Log line content (`entry.raw`) comes from files any authenticated
 * node/device can write alert text into (via FilePathway) -- untrusted, at
 * the same trust level as the alert payload fields dashboard.js already
 * treats as untrusted. Two rendering paths exist for a line's content:
 *
 *   1. No active search query: `renderLineContent` assigns
 *      `el.textContent = raw` -- zero risk, `textContent` never interprets
 *      its input as markup, exactly like every text field in dashboard.js.
 *
 *   2. An active search query: the matched substring(s) need a `<mark>`
 *      wrapper, which means building an HTML string. `escapeAndHighlight`
 *      does this the ONLY safe way: it walks `raw` (the ORIGINAL,
 *      unescaped text) with `String.prototype.indexOf` to find each
 *      case-insensitive match, SPLITS the string at those match
 *      boundaries FIRST, and only THEN runs `common.js`'s `escapeHtml` on
 *      each individual segment (the text before a match, the matched text
 *      itself, the text after) before concatenating them with the two
 *      literal, hard-coded strings `"<mark>"` / `"</mark>"` in between.
 *
 *      Why this order matters, concretely: HTML-escaping replaces each of
 *      `& < > " '` independently, with no dependency on neighboring
 *      characters -- so escaping several substrings separately and
 *      concatenating the results is byte-for-byte identical to escaping
 *      the whole original string in one pass. Splitting BEFORE escaping
 *      therefore never needs to map a byte offset found in the RAW string
 *      onto a shifted offset in an ALREADY-escaped string (which is where
 *      the classic version of this bug lives: `&`/`<`/`>` expand into
 *      multi-character entities, so raw-string offsets do NOT line up with
 *      escaped-string offsets once any escaping has happened before the
 *      match is spliced in). This implementation never attempts that
 *      mapping at all -- every character that ends up in the output has
 *      individually passed through `escapeHtml`, except the two constant,
 *      hard-coded `<mark>`/`</mark>` tag strings this file itself writes
 *      (never derived from `raw`). The result is assigned via
 *      `el.innerHTML` -- the ONE deliberate, carefully-scoped `innerHTML`
 *      use in this entire file (see `renderLineContent`).
 *
 *      Matching itself uses plain (non-regex) `String.prototype.indexOf`
 *      on lower-cased copies of both strings, not a `RegExp` built from
 *      the user's query -- so there's no regex-metacharacter-injection or
 *      ReDoS concern from an attacker-chosen (or attacker-INFLUENCED, via
 *      a crafted alert `message`) search string either.
 *
 *      See tests/js/test_log_viewer.mjs's "escapeAndHighlight" suite for
 *      an exhaustive battery of adversarial payloads (`<script>`, `&`,
 *      quotes, a literal `<mark>` already present in the raw text, etc.)
 *      verified against the general safety invariant "every `<`/`>` in
 *      the output is either escaped, or part of a `<mark>`/`</mark>` tag
 *      this file itself inserted" -- not just a handful of spot checks.
 *
 * Every OTHER piece of dynamically-inserted text in this file (file names
 * in the dropdown, status text, the match counter, the backfill-progress
 * readout, localized timestamps) goes through `textContent` or
 * `createElement`, never `innerHTML`.
 *
 * --------------------------------------------------------------------
 * Loading common.js: browser vs. Node / Testability
 * --------------------------------------------------------------------
 * Same dual-environment seam as dashboard.js (see that file's own header
 * comment for the full rationale): under Node (this repo's test harness,
 * tests/js/test_log_viewer.mjs), `require("./common.js")` is used;
 * layout.html loads common.js as a plain global `<script>` in the browser,
 * where `window.AlertManagerCommon` is already populated by the time this
 * file runs.
 *
 * Every function above the "DOM rendering / wiring" section is pure (no
 * `document`/`window` access beyond an injectable `storage` parameter for
 * the two preference read/write helpers) and exported via
 * `module.exports`. Everything below that point only ever runs through
 * `init()`, which itself only fires when `document` exists -- so
 * `require()`-ing this module under plain Node is side-effect-free.
 *
 * What this file's tests deliberately do NOT (and per the task brief,
 * cannot) cover without a real browser/DOM: actual `IntersectionObserver`
 * firing on real scroll, real `fetch`/`AbortController` timeout behavior,
 * CSS layout/wrapping, and the exact pixel-level autoscroll heuristic --
 * `pollOnce`/`fetchStat`/`fetchChunkStep`/`runBackfill` are instead tested
 * against an injected mock `fetchFn` that returns canned
 * `{status, json()}`-shaped objects, and `renderLineContent`/
 * `buildLineElement`-adjacent DOM code is exercised (where tested at all)
 * against plain `{textContent: "", innerHTML: ""}` stand-in objects rather
 * than a real DOM node -- documented as a known gap in this task's report
 * rather than silently skipped.
 */

(function (window) {
    "use strict";

    let AlertManagerCommon;
    if (typeof module !== "undefined" && module.exports) {
        AlertManagerCommon = require("./common.js");
    } else {
        AlertManagerCommon = window.AlertManagerCommon;
    }

    const fetchWithTimeout = AlertManagerCommon.fetchWithTimeout;
    const escapeHtml = AlertManagerCommon.escapeHtml;
    const formatLocalizedTime = AlertManagerCommon.formatLocalizedTime;
    const getSelectedTimezone = AlertManagerCommon.getSelectedTimezone;
    const setSelectedTimezone = AlertManagerCommon.setSelectedTimezone;
    const NORTH_AMERICAN_TIMEZONES = AlertManagerCommon.NORTH_AMERICAN_TIMEZONES;

    // ------------------------------------------------------------------
    // Constants
    // ------------------------------------------------------------------
    const LEVELS = ["critical", "warning", "notice", "info", "debug"];
    const DEFAULT_LEVEL_FILTERS = { critical: true, warning: true, notice: true, info: false, debug: false };
    const DEFAULT_POLL_INTERVAL_MS = 10000; // matches http_gateway.py's old Python-side default
    const SEARCH_DEBOUNCE_MS = 250;
    const RENDER_BATCH_SIZE = 500;
    const FETCH_TIMEOUT_MS = 30000; // matches the old log-viewer.js's own fetchWithTimeout default
    const LEVEL_FILTER_STORAGE_KEY = "logViewer_levelFilters";
    const OPS_TOGGLE_STORAGE_KEY = "logViewer_showOpsLogs";

    const TIMESTAMP_RE = /^(\d{10})/;
    const LEVEL_RE = /\[(critical|warning|notice|info|debug)\]/i;

    // ==================================================================
    // Pure: line parsing
    // ==================================================================

    /**
     * Parses one raw log line into `{raw, ts, level}`. `ts` is the leading
     * 10-digit Unix-epoch-seconds prefix (CLAUDE.md's file pathway format,
     * `{timestamp} [{level}] ...`) or `null` if absent; `level` is the
     * bracketed level word (case-insensitive, normalized to lowercase) or
     * `null` if absent/unrecognized. Parsed once at ingest (`LogStore.
     * appendLive`/`prependOlder`), never re-parsed per filter pass.
     */
    function parseLine(raw) {
        const text = raw === null || raw === undefined ? "" : String(raw);
        const tsMatch = TIMESTAMP_RE.exec(text);
        const levelMatch = LEVEL_RE.exec(text);
        return {
            raw: text,
            ts: tsMatch ? parseInt(tsMatch[1], 10) : null,
            level: levelMatch ? levelMatch[1].toLowerCase() : null,
        };
    }

    /**
     * The single AND-not-OR predicate `LogStore.matchIndexes` and the
     * live-poll fast path (`onLiveLines`) both use, so the two can never
     * drift out of sync with each other.
     *
     * A line with NO recognized level (`entry.level === null` -- e.g. a
     * malformed or non-alert line) always passes the level-filter half of
     * this check: there's no level bucket to exclude it FROM. Level
     * filtering only ever hides lines whose level is known and whose
     * filter checkbox is off. (This is a deliberate divergence from the
     * pre-overhaul log-viewer.js, which -- due to a latent bug --
     * re-applied its filters over already-rendered DOM lines on every
     * checkbox change and defaulted an unmatched line to HIDDEN in that
     * pass, even though the same line was left VISIBLE at insert time.
     * That inconsistency isn't reproduced here; see this task's report.)
     *
     * Search matching is a plain case-insensitive substring test against
     * `raw` (never a `RegExp` built from `query` -- see this file's
     * header comment on `escapeAndHighlight` for why that matters for
     * `<mark>` highlighting too).
     */
    function passesFilters(entry, levelFilters, query) {
        if (entry.level && !(levelFilters && levelFilters[entry.level] === true)) {
            return false;
        }
        if (query) {
            const q = String(query).toLowerCase();
            if (entry.raw.toLowerCase().indexOf(q) === -1) {
                return false;
            }
        }
        return true;
    }

    // ==================================================================
    // Pure: LogStore
    // ==================================================================

    /**
     * Chronological (oldest-first) in-memory array of parsed log entries.
     * `appendLive` pushes new lines at the end (from `/api/logs/tail`
     * polling); `prependOlder` unshifts an older chunk's lines at the
     * front (from `/api/logs/chunk` backward backfill) -- both accept raw
     * line arrays and parse them internally via `parseLine`.
     */
    function LogStore() {
        this.entries = [];
    }

    LogStore.prototype.appendLive = function (rawLines) {
        const parsed = (rawLines || []).map(parseLine);
        for (let i = 0; i < parsed.length; i++) {
            this.entries.push(parsed[i]);
        }
        return parsed;
    };

    LogStore.prototype.prependOlder = function (rawLines) {
        const parsed = (rawLines || []).map(parseLine);
        this.entries = parsed.concat(this.entries);
        return parsed;
    };

    /**
     * Scans the FULL store and returns the ascending (oldest-first)
     * indexes of every entry passing `passesFilters(entry, levelFilters,
     * query)` -- the core perf fix replacing the old DOM-line-cap
     * approach: filtering/search always runs over the in-memory data, not
     * the rendered DOM.
     */
    LogStore.prototype.matchIndexes = function (levelFilters, query) {
        const result = [];
        for (let i = 0; i < this.entries.length; i++) {
            if (passesFilters(this.entries[i], levelFilters, query)) {
                result.push(i);
            }
        }
        return result;
    };

    Object.defineProperty(LogStore.prototype, "length", {
        get: function () {
            return this.entries.length;
        },
    });

    // ==================================================================
    // Pure: escape-then-highlight (security-critical -- see file header)
    // ==================================================================

    /**
     * HTML-escapes `raw` and wraps every case-insensitive occurrence of
     * `query` in `<mark>...</mark>`, safely -- see this file's header
     * comment ("TEXT-INSERTION SAFETY") for the full reasoning. Returns
     * plain `escapeHtml(raw)` (no `<mark>` tags at all) when `query` is
     * empty or has no match.
     *
     * @param {*} raw untrusted log line content, coerced to a string
     * @param {string} query the current (already-typed) search query
     * @returns {string} HTML-safe markup, fit for `innerHTML`
     */
    function escapeAndHighlight(raw, query) {
        const text = raw === null || raw === undefined ? "" : String(raw);
        const q = query === null || query === undefined ? "" : String(query);

        if (q.length === 0) {
            return escapeHtml(text);
        }

        const lowerText = text.toLowerCase();
        const lowerQuery = q.toLowerCase();

        let result = "";
        let pos = 0;
        let idx = lowerText.indexOf(lowerQuery, pos);

        if (idx === -1) {
            return escapeHtml(text);
        }

        while (idx !== -1) {
            // Escape the segment BEFORE this match, then the matched
            // segment itself (original casing preserved), each
            // independently -- never the other way around (escape whole
            // string, then try to splice tags in at raw-string offsets).
            result += escapeHtml(text.slice(pos, idx));
            result += "<mark>" + escapeHtml(text.slice(idx, idx + lowerQuery.length)) + "</mark>";
            pos = idx + lowerQuery.length;
            idx = lowerText.indexOf(lowerQuery, pos);
        }
        result += escapeHtml(text.slice(pos));
        return result;
    }

    /**
     * Renders one line's content into `el`: plain `textContent` when
     * there's no active search query (the common case, zero risk), or
     * `escapeAndHighlight`'s output via `innerHTML` when there is -- the
     * ONE call site in this file that ever assigns `innerHTML`. Works
     * against any object exposing `textContent`/`innerHTML` properties
     * (a real DOM element, or a plain stand-in object for tests).
     */
    function renderLineContent(el, raw, query) {
        if (!query) {
            el.textContent = raw === null || raw === undefined ? "" : String(raw);
        } else {
            el.innerHTML = escapeAndHighlight(raw, query);
        }
    }

    // ==================================================================
    // Pure: debounce
    // ==================================================================

    function debounce(fn, wait) {
        let timer = null;
        function debounced() {
            const args = arguments;
            const ctx = this;
            if (timer) {
                clearTimeout(timer);
            }
            timer = setTimeout(function () {
                timer = null;
                fn.apply(ctx, args);
            }, wait);
        }
        debounced.cancel = function () {
            if (timer) {
                clearTimeout(timer);
                timer = null;
            }
        };
        return debounced;
    }

    // ==================================================================
    // Pure: URL builders (Task 21's exact API contract)
    // ==================================================================

    function buildListUrl(includeOps) {
        return "/api/logs/list?include_ops=" + (includeOps ? "true" : "false");
    }

    function buildStatUrl(file) {
        return "/api/logs/tail?file=" + encodeURIComponent(file) + "&stat=1";
    }

    function buildTailUrl(file, position) {
        return "/api/logs/tail?file=" + encodeURIComponent(file) + "&position=" + encodeURIComponent(position);
    }

    function buildChunkUrl(file, end) {
        return "/api/logs/chunk?file=" + encodeURIComponent(file) + "&end=" + encodeURIComponent(end);
    }

    // ==================================================================
    // Pure-ish: API step functions (Poller / Backfill) -- take an
    // injected `fetchFn(url) -> Promise<{status, json()}>` so these are
    // fully testable with a mock, no real network/DOM involved.
    // ==================================================================

    /** `?stat=1` -> `{type:'ok', fileSize}` | `{type:'access_denied'}` | `{type:'error', ...}`. */
    function fetchStat(fetchFn, file) {
        return fetchFn(buildStatUrl(file)).then(
            function (resp) {
                if (resp.status === 403) {
                    return { type: "access_denied" };
                }
                return resp
                    .json()
                    .catch(function () {
                        return null;
                    })
                    .then(function (body) {
                        const ok = resp.status >= 200 && resp.status < 300;
                        if (!ok || !body || typeof body.file_size !== "number") {
                            return { type: "error", httpStatus: resp.status, body: body };
                        }
                        return { type: "ok", fileSize: body.file_size };
                    });
            },
            function (err) {
                return { type: "error", error: err };
            }
        );
    }

    /** One forward tail read -> `{type:'ok', lines, position, fileSize, eof}` | `{type:'access_denied'}` | `{type:'error', ...}`. */
    function pollOnce(fetchFn, file, position) {
        return fetchFn(buildTailUrl(file, position)).then(
            function (resp) {
                if (resp.status === 403) {
                    return { type: "access_denied" };
                }
                return resp
                    .json()
                    .catch(function () {
                        return null;
                    })
                    .then(function (body) {
                        const ok = resp.status >= 200 && resp.status < 300;
                        if (!ok || !body || body.status === "error") {
                            return { type: "error", httpStatus: resp.status, body: body };
                        }
                        return {
                            type: "ok",
                            lines: body.lines || [],
                            position: typeof body.position === "number" ? body.position : position,
                            fileSize: body.file_size,
                            eof: !!body.eof,
                        };
                    });
            },
            function (err) {
                return { type: "error", error: err };
            }
        );
    }

    /** One backward chunk read -> `{type:'ok', start, lines, eof}` | `{type:'access_denied'}` | `{type:'too_large', body}` | `{type:'error', ...}`. */
    function fetchChunkStep(fetchFn, file, end) {
        return fetchFn(buildChunkUrl(file, end)).then(
            function (resp) {
                if (resp.status === 403) {
                    return { type: "access_denied" };
                }
                return resp
                    .json()
                    .catch(function () {
                        return null;
                    })
                    .then(function (body) {
                        if (resp.status === 413) {
                            return { type: "too_large", body: body };
                        }
                        const ok = resp.status >= 200 && resp.status < 300;
                        if (!ok || !body || body.status === "error") {
                            return { type: "error", httpStatus: resp.status, body: body };
                        }
                        return { type: "ok", start: body.start, lines: body.lines || [], eof: !!body.eof };
                    });
            },
            function (err) {
                return { type: "error", error: err };
            }
        );
    }

    /** `fileSize` bytes loaded so far as a 0-100 integer percentage; 100 for an empty/zero-size file. */
    function computeProgressPercent(fileSize, start) {
        if (!fileSize || fileSize <= 0) {
            return 100;
        }
        const loaded = fileSize - start;
        const pct = Math.floor((loaded / fileSize) * 100);
        return Math.max(0, Math.min(100, pct));
    }

    function formatBackfillProgress(percent, status) {
        if (status === "done") {
            return "History fully loaded";
        }
        if (status === "too_large") {
            return "History load stopped at " + percent + "% (a log entry was too large to read further back)";
        }
        if (status === "access_denied") {
            return "History load stopped (access denied)";
        }
        if (status === "error") {
            return "History load stopped at " + percent + "% (error loading history)";
        }
        return "Loading history: " + percent + "%";
    }

    function computeCounterText(matchCount, totalCount) {
        return matchCount + " matches / " + totalCount + " lines";
    }

    /**
     * Backward-chunk backfill loop: walks `end` from `fileSize` down to
     * `0` via repeated `fetchChunkStep` calls, prepending each chunk's
     * lines into `deps.store` (oldest chunk last), until `eof`, a `413`
     * ("too large" -- stop paging this file's older region, per
     * gateway/logs/routes.py's module docstring), a `403` (access
     * revoked mid-backfill), or an unexpected error. `deps.
     * isCurrentGeneration()` is checked both before and after each
     * network round-trip so a stale backfill (the user switched files
     * mid-load) abandons itself without mutating a discarded store or
     * firing further callbacks.
     *
     * @param {Object} deps
     * @param {Function} deps.fetchFn
     * @param {string} deps.file
     * @param {number} deps.fileSize
     * @param {LogStore} deps.store
     * @param {Function} deps.isCurrentGeneration () => boolean
     * @param {Function} deps.onProgress (percent) => void, called after each chunk
     * @param {Function} deps.onDone () => void, called once on eof
     * @param {Function} deps.onTooLarge (body) => void
     * @param {Function} deps.onAccessDenied () => void
     * @param {Function} deps.onError (result) => void
     */
    async function runBackfill(deps) {
        const fileSize = deps.fileSize;
        if (!fileSize || fileSize <= 0) {
            deps.onProgress(100);
            deps.onDone();
            return;
        }

        let end = fileSize;
        while (end > 0) {
            if (!deps.isCurrentGeneration()) {
                return;
            }
            const result = await fetchChunkStep(deps.fetchFn, deps.file, end);
            if (!deps.isCurrentGeneration()) {
                return;
            }

            if (result.type === "access_denied") {
                deps.onAccessDenied();
                return;
            }
            if (result.type === "too_large") {
                deps.onTooLarge(result.body);
                return;
            }
            if (result.type === "error") {
                deps.onError(result);
                return;
            }

            if (result.lines && result.lines.length > 0) {
                deps.store.prependOlder(result.lines);
            }
            deps.onProgress(computeProgressPercent(fileSize, result.start));

            // `result.start >= end` is a defensive stop against a
            // non-progressing response (shouldn't happen per Task 21's
            // server contract, which guarantees `start < end` unless
            // `eof`) -- without it, a server bug here could infinite-loop
            // this client.
            if (result.eof || typeof result.start !== "number" || result.start >= end) {
                deps.onDone();
                return;
            }
            end = result.start;
        }
        deps.onDone();
    }

    // ==================================================================
    // Pure: preference persistence (injectable storage, same pattern as
    // dashboard.js's readPersistedWindow/persistWindow)
    // ==================================================================

    function mergeFilterPreferences(saved, defaults) {
        const merged = Object.assign({}, defaults);
        if (saved && typeof saved === "object") {
            LEVELS.forEach(function (level) {
                if (typeof saved[level] === "boolean") {
                    merged[level] = saved[level];
                }
            });
        }
        return merged;
    }

    function readPersistedFilters(storage) {
        const store = storage || (typeof window !== "undefined" ? window.localStorage : null);
        if (!store) {
            return Object.assign({}, DEFAULT_LEVEL_FILTERS);
        }
        let raw;
        try {
            raw = store.getItem(LEVEL_FILTER_STORAGE_KEY);
        } catch (e) {
            return Object.assign({}, DEFAULT_LEVEL_FILTERS);
        }
        if (!raw) {
            return Object.assign({}, DEFAULT_LEVEL_FILTERS);
        }
        let parsed;
        try {
            parsed = JSON.parse(raw);
        } catch (e) {
            return Object.assign({}, DEFAULT_LEVEL_FILTERS);
        }
        return mergeFilterPreferences(parsed, DEFAULT_LEVEL_FILTERS);
    }

    function persistFilters(filters, storage) {
        const store = storage || (typeof window !== "undefined" ? window.localStorage : null);
        if (!store) {
            return;
        }
        try {
            store.setItem(LEVEL_FILTER_STORAGE_KEY, JSON.stringify(filters));
        } catch (e) {
            // degrade silently, same as common.js's timezone setters
        }
    }

    function readPersistedOpsToggle(storage) {
        const store = storage || (typeof window !== "undefined" ? window.sessionStorage : null);
        if (!store) {
            return false;
        }
        let raw;
        try {
            raw = store.getItem(OPS_TOGGLE_STORAGE_KEY);
        } catch (e) {
            return false;
        }
        return raw === "true";
    }

    function persistOpsToggle(value, storage) {
        const store = storage || (typeof window !== "undefined" ? window.sessionStorage : null);
        if (!store) {
            return;
        }
        try {
            store.setItem(OPS_TOGGLE_STORAGE_KEY, value ? "true" : "false");
        } catch (e) {
            // degrade silently
        }
    }

    // ==================================================================
    // DOM rendering / wiring -- browser only (see file header). Nothing
    // below this line runs unless init() is called.
    // ==================================================================

    const state = {
        currentFile: "",
        paused: false,
        generation: 0,
        store: new LogStore(),
        levelFilters: Object.assign({}, DEFAULT_LEVEL_FILTERS),
        searchQuery: "",
        showOpsLogs: false,
        autoScroll: true,
        pollPosition: 0,
        fileSize: 0,
        renderCount: RENDER_BATCH_SIZE, // target cap: how many newest matches SHOULD be in the DOM
        renderedCount: 0, // how many actually are, right now
        lastMatches: [], // newest-first store indexes, refreshed by recomputeMatches()
        backfillStatus: "idle", // 'idle' | 'loading' | 'done' | 'too_large' | 'error'
        backfillPercent: 0,
        pollTimer: null,
    };

    let els = null;

    function prodFetch(url) {
        return fetchWithTimeout(url, { credentials: "same-origin" }, FETCH_TIMEOUT_MS);
    }

    // ------------------------------------------------------------------
    // Renderer
    // ------------------------------------------------------------------

    function buildLineElement(entry, query) {
        const div = document.createElement("div");
        div.className = "log-line" + (entry.level ? " " + entry.level : "");

        if (typeof entry.ts === "number") {
            const timeSpan = document.createElement("span");
            timeSpan.className = "localized-time";
            timeSpan.textContent = formatLocalizedTime(entry.ts, getSelectedTimezone());
            div.appendChild(timeSpan);
        }

        const contentSpan = document.createElement("span");
        contentSpan.className = "log-line-content";
        renderLineContent(contentSpan, entry.raw, query);
        div.appendChild(contentSpan);

        return div;
    }

    /** Removes every rendered row, keeping the two fixed anchors (`#log-no-logs`, `#log-sentinel`). */
    function clearRows() {
        const container = els.container;
        const keep = [els.noLogs, els.sentinel];
        Array.prototype.slice.call(container.children).forEach(function (child) {
            if (keep.indexOf(child) === -1) {
                container.removeChild(child);
            }
        });
    }

    /** Appends rows for the given (newest-first) store indexes just above the bottom sentinel. */
    function appendRowsAtBottom(indexes) {
        const frag = document.createDocumentFragment();
        indexes.forEach(function (i) {
            frag.appendChild(buildLineElement(state.store.entries[i], state.searchQuery));
        });
        els.container.insertBefore(frag, els.sentinel);
    }

    /**
     * Prepends entry OBJECTS (not store indexes -- these are brand-new
     * live lines, handed over directly from `LogStore.appendLive`'s
     * return value) at the very top, above everything currently rendered.
     * `entries` must be given oldest-of-this-batch-first: inserting each
     * one right after the fixed `#log-no-logs` anchor pushes the
     * previous insertion down, so the LAST one inserted (the newest of
     * the batch) ends up literally topmost.
     */
    function prependEntriesAtTop(entries) {
        entries.forEach(function (entry) {
            const row = buildLineElement(entry, state.searchQuery);
            els.container.insertBefore(row, els.noLogs.nextSibling);
        });
    }

    function updateCounter(matchCount, totalCount) {
        els.matchCount.textContent = computeCounterText(matchCount, totalCount);
    }

    function updateEmptyState(isEmpty) {
        els.noLogs.hidden = !isEmpty;
    }

    function updateBackfillProgressUI(status, percent) {
        els.backfillProgress.textContent = formatBackfillProgress(percent, status);
    }

    /** Refreshes `state.lastMatches` (newest-first store indexes) and the "N matches / M lines" counter from the current store+filters+query. Does NOT touch the DOM rows -- callers that need rows rendered call `topUpRenderedRows()` afterward. */
    function recomputeMatches() {
        const matches = state.store.matchIndexes(state.levelFilters, state.searchQuery);
        state.lastMatches = matches.slice().reverse();
        updateCounter(state.lastMatches.length, state.store.length);
        updateEmptyState(state.lastMatches.length === 0);
    }

    /** Appends whatever's newly available between `renderedCount` and the current `renderCount` cap (or however much of `lastMatches` exists, if less). Called after every event that can make more matches available: initial render, backfill chunks, and the "load more" sentinel. */
    function topUpRenderedRows() {
        const target = Math.min(state.renderCount, state.lastMatches.length);
        if (target <= state.renderedCount) {
            return;
        }
        appendRowsAtBottom(state.lastMatches.slice(state.renderedCount, target));
        state.renderedCount = target;
    }

    /** Full re-render from LogStore -- used for every filter/search/timezone change, per the brief ("no incremental DOM patching" for THESE events specifically). */
    function renderFull() {
        state.renderCount = RENDER_BATCH_SIZE;
        state.renderedCount = 0;
        clearRows();
        recomputeMatches();
        topUpRenderedRows();
        if (state.autoScroll) {
            els.container.scrollTop = 0;
        }
    }

    /** Sentinel-triggered "load more": bumps the render cap by one batch and tops up. A no-op once every currently-known match is rendered AND backfill has finished (nothing more will ever arrive). */
    function loadMoreRows() {
        if (state.renderedCount >= state.lastMatches.length && state.backfillStatus !== "loading") {
            return;
        }
        state.renderCount += RENDER_BATCH_SIZE;
        recomputeMatches();
        topUpRenderedRows();
    }

    // ------------------------------------------------------------------
    // Poller
    // ------------------------------------------------------------------

    /** New lines from a poll tick: append to the store, fast-path-prepend any that match the CURRENT filter/search directly at the top (no full re-render), then refresh the counter. */
    function onLiveLines(newRawLines) {
        const newEntries = state.store.appendLive(newRawLines);
        const matchingNew = newEntries.filter(function (e) {
            return passesFilters(e, state.levelFilters, state.searchQuery);
        });
        if (matchingNew.length > 0) {
            prependEntriesAtTop(matchingNew);
            state.renderCount += matchingNew.length;
            state.renderedCount += matchingNew.length;
            if (state.autoScroll) {
                els.container.scrollTop = 0;
            }
        }
        recomputeMatches(); // refreshes the counter, and (redundantly-safely) the empty state
    }

    function updateLastUpdateUI() {
        els.lastUpdate.textContent = new Date().toLocaleTimeString();
    }

    function setStatus(cls, text) {
        els.status.className = "status " + cls;
        els.status.textContent = text;
    }

    function updatePauseButtonUI() {
        els.pauseBtn.textContent = state.paused ? "Resume" : "Pause";
        els.pauseBtn.classList.toggle("paused", state.paused);
    }

    /** 403 on either the tail poll or a backfill chunk: pause (same as the old log-viewer.js's auto-pause), surface the denial, and let any in-flight backfill notice via its own `isCurrentGeneration`/onAccessDenied wiring. */
    function handleAccessDenied() {
        state.paused = true;
        updatePauseButtonUI();
        setStatus("error", "Access Denied");
        els.accessDeniedBanner.hidden = false;
    }

    function pollTick() {
        if (state.paused || !state.currentFile) {
            return;
        }
        const file = state.currentFile;
        const myGeneration = state.generation;
        pollOnce(prodFetch, file, state.pollPosition)
            .then(function (result) {
                if (state.generation !== myGeneration) {
                    return; // stale -- file switched mid-flight
                }
                if (result.type === "access_denied") {
                    handleAccessDenied();
                    return;
                }
                if (result.type === "error") {
                    setStatus("disconnected", "Disconnected");
                    return;
                }
                state.pollPosition = result.position;
                if (result.lines && result.lines.length > 0) {
                    onLiveLines(result.lines);
                    updateLastUpdateUI();
                }
                setStatus("connected", "Connected");
            })
            .catch(function () {
                setStatus("disconnected", "Disconnected");
            });
    }

    // ------------------------------------------------------------------
    // File switching / initial-load sequence
    // ------------------------------------------------------------------

    /**
     * The initial-load sequence, per the brief, run both on first page
     * load and every time the file dropdown changes: (1) stat -> file
     * size; (2) an immediate poll tick (so anything written between stat
     * and now shows up right away) plus the standing 10s interval taking
     * over from there; (3) a concurrent backward-chunk backfill from
     * `file_size` to `0`.
     */
    function startViewingFile(file) {
        state.generation += 1;
        const myGeneration = state.generation;

        state.currentFile = file;
        state.store = new LogStore();
        state.pollPosition = 0;
        state.fileSize = 0;
        state.renderCount = RENDER_BATCH_SIZE;
        state.renderedCount = 0;
        state.lastMatches = [];
        state.backfillStatus = "loading";
        state.backfillPercent = 0;

        clearRows();
        updateEmptyState(true);
        updateCounter(0, 0);
        updateBackfillProgressUI("loading", 0);
        els.accessDeniedBanner.hidden = true;
        setStatus("connected", "Connected");

        fetchStat(prodFetch, file).then(function (result) {
            if (state.generation !== myGeneration) {
                return;
            }
            if (result.type === "access_denied") {
                handleAccessDenied();
                return;
            }
            if (result.type === "error") {
                setStatus("disconnected", "Error");
                updateBackfillProgressUI("error", 0);
                return;
            }

            state.fileSize = result.fileSize;
            state.pollPosition = result.fileSize;

            pollTick();

            runBackfill({
                fetchFn: prodFetch,
                file: file,
                fileSize: result.fileSize,
                store: state.store,
                isCurrentGeneration: function () {
                    return state.generation === myGeneration;
                },
                onProgress: function (pct) {
                    state.backfillPercent = pct;
                    updateBackfillProgressUI("loading", pct);
                    recomputeMatches();
                    topUpRenderedRows();
                },
                onDone: function () {
                    state.backfillStatus = "done";
                    updateBackfillProgressUI("done", 100);
                },
                onTooLarge: function () {
                    state.backfillStatus = "too_large";
                    updateBackfillProgressUI("too_large", state.backfillPercent);
                },
                onAccessDenied: handleAccessDenied,
                onError: function () {
                    state.backfillStatus = "error";
                    updateBackfillProgressUI("error", state.backfillPercent);
                },
            });
        });
    }

    // ------------------------------------------------------------------
    // File list / dropdown / ops toggle
    // ------------------------------------------------------------------

    function populateFileSelect(files) {
        els.fileSelect.textContent = "";
        files.forEach(function (f) {
            const opt = document.createElement("option");
            opt.value = f;
            opt.textContent = f;
            els.fileSelect.appendChild(opt);
        });
    }

    /** Ported-unchanged behavior: always selects the first (most recent) file in the list, matching the old `loadLogFiles()`. */
    function loadLogFiles() {
        return fetchWithTimeout(buildListUrl(state.showOpsLogs), { credentials: "same-origin" }, FETCH_TIMEOUT_MS)
            .then(function (resp) {
                return resp.json();
            })
            .then(function (data) {
                const files = data.files || [];
                populateFileSelect(files);
                if (files.length > 0) {
                    els.fileSelect.value = files[0];
                    startViewingFile(files[0]);
                } else {
                    const opt = document.createElement("option");
                    opt.value = "";
                    opt.textContent = "No log files found";
                    els.fileSelect.appendChild(opt);
                    setStatus("disconnected", "No files");
                }
            })
            .catch(function () {
                setStatus("disconnected", "Error");
            });
    }

    // ------------------------------------------------------------------
    // init()
    // ------------------------------------------------------------------

    function setupTimezoneSelect() {
        els.timezoneSelect.textContent = "";
        const current = getSelectedTimezone();
        NORTH_AMERICAN_TIMEZONES.forEach(function (tz) {
            const opt = document.createElement("option");
            opt.value = tz.value;
            opt.textContent = tz.label;
            if (tz.value === current) {
                opt.selected = true;
            }
            els.timezoneSelect.appendChild(opt);
        });
        els.timezoneSelect.addEventListener("change", function () {
            setSelectedTimezone(els.timezoneSelect.value);
            renderFull();
        });
    }

    function setupFilters() {
        state.levelFilters = readPersistedFilters();
        LEVELS.forEach(function (level) {
            const cb = els.filterCheckboxes[level];
            if (!cb) {
                return;
            }
            cb.checked = state.levelFilters[level];
            cb.addEventListener("change", function () {
                state.levelFilters[level] = cb.checked;
                persistFilters(state.levelFilters);
                renderFull();
            });
        });

        if (els.filterAllBtn) {
            els.filterAllBtn.addEventListener("click", function () {
                LEVELS.forEach(function (level) {
                    state.levelFilters[level] = true;
                    if (els.filterCheckboxes[level]) {
                        els.filterCheckboxes[level].checked = true;
                    }
                });
                persistFilters(state.levelFilters);
                renderFull();
            });
        }
        if (els.filterNoneBtn) {
            els.filterNoneBtn.addEventListener("click", function () {
                LEVELS.forEach(function (level) {
                    state.levelFilters[level] = false;
                    if (els.filterCheckboxes[level]) {
                        els.filterCheckboxes[level].checked = false;
                    }
                });
                persistFilters(state.levelFilters);
                renderFull();
            });
        }
    }

    function setupSearch() {
        const debouncedSearch = debounce(function () {
            state.searchQuery = els.searchInput.value || "";
            renderFull();
        }, SEARCH_DEBOUNCE_MS);
        els.searchInput.addEventListener("input", debouncedSearch);
    }

    function setupOpsToggle() {
        if (!els.opsToggle) {
            return; // non-admin: server-side gated out of the template entirely
        }
        state.showOpsLogs = readPersistedOpsToggle();
        els.opsToggle.checked = state.showOpsLogs;
        els.opsToggle.addEventListener("change", function () {
            state.showOpsLogs = els.opsToggle.checked;
            persistOpsToggle(state.showOpsLogs);
            loadLogFiles();
        });
    }

    function setupControls() {
        els.fileSelect.addEventListener("change", function () {
            startViewingFile(els.fileSelect.value);
        });
        els.pauseBtn.addEventListener("click", function () {
            state.paused = !state.paused;
            updatePauseButtonUI();
        });
        if (els.reloadFileBtn) {
            els.reloadFileBtn.addEventListener("click", function () {
                if (state.currentFile) {
                    startViewingFile(state.currentFile);
                }
            });
        }
        if (els.refreshFilesBtn) {
            els.refreshFilesBtn.addEventListener("click", function () {
                loadLogFiles();
            });
        }
        els.container.addEventListener("scroll", function () {
            const atTop = els.container.scrollTop <= 50;
            state.autoScroll = atTop;
            els.autoscrollStatus.textContent = state.autoScroll ? "On" : "Off";
        });
    }

    function setupSentinel() {
        if (typeof IntersectionObserver === "undefined" || !els.sentinel) {
            return;
        }
        const observer = new IntersectionObserver(
            function (entries) {
                entries.forEach(function (entry) {
                    if (entry.isIntersecting) {
                        loadMoreRows();
                    }
                });
            },
            { root: els.container, threshold: 0 }
        );
        observer.observe(els.sentinel);
    }

    function setupUnixClock() {
        function tick() {
            els.currentUnixTime.textContent = String(Math.floor(Date.now() / 1000));
        }
        tick();
        window.setInterval(tick, 1000);
    }

    function init() {
        els = {
            fileSelect: document.getElementById("log-file-select"),
            pauseBtn: document.getElementById("log-pause-btn"),
            reloadFileBtn: document.getElementById("log-reload-file-btn"),
            refreshFilesBtn: document.getElementById("log-refresh-files-btn"),
            status: document.getElementById("log-status"),
            currentUnixTime: document.getElementById("log-current-unix-time"),
            matchCount: document.getElementById("log-match-count"),
            lastUpdate: document.getElementById("log-last-update"),
            autoscrollStatus: document.getElementById("log-autoscroll-status"),
            backfillProgress: document.getElementById("log-backfill-progress"),
            searchInput: document.getElementById("log-search-input"),
            timezoneSelect: document.getElementById("log-timezone-select"),
            opsToggle: document.getElementById("log-ops-toggle"),
            filterAllBtn: document.getElementById("log-filter-all-btn"),
            filterNoneBtn: document.getElementById("log-filter-none-btn"),
            container: document.getElementById("log-container"),
            noLogs: document.getElementById("log-no-logs"),
            sentinel: document.getElementById("log-sentinel"),
            accessDeniedBanner: document.getElementById("log-access-denied-banner"),
            filterCheckboxes: {
                critical: document.getElementById("log-filter-critical"),
                warning: document.getElementById("log-filter-warning"),
                notice: document.getElementById("log-filter-notice"),
                info: document.getElementById("log-filter-info"),
                debug: document.getElementById("log-filter-debug"),
            },
        };

        if (!els.fileSelect || !els.container || !els.sentinel || !els.noLogs) {
            return; // defensive no-op, matches dashboard.js's pattern
        }

        setupTimezoneSelect();
        setupFilters();
        setupSearch();
        setupOpsToggle();
        setupControls();
        setupSentinel();
        setupUnixClock();
        updatePauseButtonUI();

        const pollIntervalMs = typeof SERVER_POLL_INTERVAL_MS !== "undefined" ? SERVER_POLL_INTERVAL_MS : DEFAULT_POLL_INTERVAL_MS;
        state.pollTimer = window.setInterval(pollTick, pollIntervalMs);

        loadLogFiles();
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    }

    // ------------------------------------------------------------------
    // Export (mirrors common.js/dashboard.js's own dual browser/Node seam)
    // ------------------------------------------------------------------

    const AlertManagerLogViewer = {
        LEVELS: LEVELS,
        DEFAULT_LEVEL_FILTERS: DEFAULT_LEVEL_FILTERS,
        DEFAULT_POLL_INTERVAL_MS: DEFAULT_POLL_INTERVAL_MS,
        SEARCH_DEBOUNCE_MS: SEARCH_DEBOUNCE_MS,
        RENDER_BATCH_SIZE: RENDER_BATCH_SIZE,
        FETCH_TIMEOUT_MS: FETCH_TIMEOUT_MS,
        parseLine: parseLine,
        passesFilters: passesFilters,
        LogStore: LogStore,
        escapeAndHighlight: escapeAndHighlight,
        renderLineContent: renderLineContent,
        debounce: debounce,
        buildListUrl: buildListUrl,
        buildStatUrl: buildStatUrl,
        buildTailUrl: buildTailUrl,
        buildChunkUrl: buildChunkUrl,
        fetchStat: fetchStat,
        pollOnce: pollOnce,
        fetchChunkStep: fetchChunkStep,
        runBackfill: runBackfill,
        computeProgressPercent: computeProgressPercent,
        formatBackfillProgress: formatBackfillProgress,
        computeCounterText: computeCounterText,
        mergeFilterPreferences: mergeFilterPreferences,
        readPersistedFilters: readPersistedFilters,
        persistFilters: persistFilters,
        readPersistedOpsToggle: readPersistedOpsToggle,
        persistOpsToggle: persistOpsToggle,
    };

    window.AlertManagerLogViewer = AlertManagerLogViewer;

    if (typeof module !== "undefined" && module.exports) {
        module.exports = AlertManagerLogViewer;
    }
})(typeof window !== "undefined" ? window : globalThis);
