/**
 * Alert Manager - Dashboard page script (Task 25)
 *
 * Drives templates/dashboard.html: the timeframe <select> (persisted to
 * localStorage['dashboard_window']), the 5-card summary grid, the critical
 * alerts table (+ "Load more" pagination), the row-detail <dialog> modal,
 * the "Updated Xs ago" indicator, and 60s auto-refresh (skipped while the
 * tab is hidden). Calls Task 22's `GET /api/dashboard/summary` and
 * `GET /api/dashboard/critical` via common.js's `fetchWithTimeout`.
 *
 * --------------------------------------------------------------------
 * Loading common.js: browser vs. Node
 * --------------------------------------------------------------------
 * In the browser, layout.html loads `common.js` via a plain <script> tag
 * in the document body BEFORE this file (`extra_scripts` is appended
 * after that tag -- see layout.html), so `window.AlertManagerCommon` is
 * already populated by the time this IIFE runs; there is no `module`
 * global in that environment, so the `require("./common.js")` branch
 * below never executes there. Under this repo's own test harness
 * (tests/js/test_dashboard.mjs, plain Node, no bundler/DOM), the reverse
 * is true: there is no `window.AlertManagerCommon` to read, but
 * common.js exports itself via `module.exports` (see that file's own
 * closing IIFE) precisely so a CommonJS `require()` can pull it in
 * directly. Both branches resolve to the same object shape either way.
 *
 * --------------------------------------------------------------------
 * TEXT-INSERTION SAFETY (security-critical -- read before editing)
 * --------------------------------------------------------------------
 * Every alert-derived value this file renders (title, message, source,
 * client, category, ingestion_source, metadata keys/values, the raw-JSON
 * dump) comes from stored alert payloads that could originate from any
 * authenticated node/device -- untrusted as far as this page is
 * concerned. This file deliberately does not use `innerHTML` ANYWHERE:
 * every DOM node is built with `document.createElement` and every piece
 * of untrusted text is assigned via `.textContent` (including the raw
 * JSON block -- `JSON.stringify(...)` assigned to `.textContent` is safe
 * regardless of what characters end up inside those quotes, because
 * `textContent` never interprets its input as markup). `common.js`'s
 * `escapeHtml` exists for call sites that build markup via string
 * interpolation; this file has none, so it isn't imported here. A
 * grep-based static check in tests/js/test_dashboard.mjs asserts the
 * literal string "innerHTML" does not appear anywhere in this file, as a
 * regression guard on that invariant.
 *
 * --------------------------------------------------------------------
 * Testability
 * --------------------------------------------------------------------
 * Every function above the "DOM rendering / wiring" section is pure (no
 * `document`/`window` access) and exported via `module.exports` under
 * Node, exactly like common.js. Everything below that point manipulates
 * the DOM or wires event listeners and is only ever invoked through
 * `init()`, which itself only runs when `document` exists (see the
 * bottom of this file) -- requiring this module under plain Node is
 * side-effect-free.
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
    const timeAgo = AlertManagerCommon.timeAgo;
    const formatLocalizedTime = AlertManagerCommon.formatLocalizedTime;
    const getSelectedTimezone = AlertManagerCommon.getSelectedTimezone;

    // ------------------------------------------------------------------
    // Constants -- mirror gateway/dashboard/routes.py's own enum/defaults
    // (`_WINDOW_SECONDS`, `DEFAULT_WINDOW`, `DEFAULT_LIMIT`). Keep these in
    // sync with that module if its enum/defaults ever change.
    // ------------------------------------------------------------------
    const WINDOW_STORAGE_KEY = "dashboard_window";
    const VALID_WINDOWS = ["1h", "6h", "12h", "24h", "7d", "14d", "30d", "all"];
    const DEFAULT_WINDOW = "24h";
    const DEFAULT_LIMIT = 50;
    const MESSAGE_TRUNCATE_LENGTH = 120;
    const LEVELS_ORDER = ["critical", "warning", "notice", "info", "debug"];
    const LEVEL_LABELS = {
        critical: "Critical",
        warning: "Warning",
        notice: "Notice",
        info: "Info",
        debug: "Debug",
    };
    const AUTO_REFRESH_MS = 60000;
    const UPDATED_TICK_MS = 5000;
    const FETCH_TIMEOUT_MS = 10000;

    // ------------------------------------------------------------------
    // Pure logic: window persistence
    // ------------------------------------------------------------------

    /** @returns {boolean} whether `value` is one of the server's 8 exact window-enum values. */
    function isValidWindow(value) {
        return VALID_WINDOWS.indexOf(value) !== -1;
    }

    /**
     * Reads `localStorage['dashboard_window']`, falling back to
     * DEFAULT_WINDOW when unset, invalid, or localStorage throws (e.g.
     * privacy-restricted browser contexts -- same degradation pattern as
     * common.js's own timezone getter/setter).
     * @param {Storage} [storage] injectable for tests; defaults to window.localStorage
     */
    function readPersistedWindow(storage) {
        const store = storage || (typeof window !== "undefined" ? window.localStorage : null);
        if (!store) {
            return DEFAULT_WINDOW;
        }
        let raw;
        try {
            raw = store.getItem(WINDOW_STORAGE_KEY);
        } catch (e) {
            return DEFAULT_WINDOW;
        }
        return isValidWindow(raw) ? raw : DEFAULT_WINDOW;
    }

    /** Persists a valid window value; a no-op (never throws) for an invalid value or an unavailable storage. */
    function persistWindow(value, storage) {
        const store = storage || (typeof window !== "undefined" ? window.localStorage : null);
        if (!store || !isValidWindow(value)) {
            return;
        }
        try {
            store.setItem(WINDOW_STORAGE_KEY, value);
        } catch (e) {
            // degrade silently
        }
    }

    // ------------------------------------------------------------------
    // Pure logic: pagination offset math
    // ------------------------------------------------------------------

    /** "Load more" always advances by the fixed page `limit`, per this task's brief -- not by however many rows the last page actually returned. */
    function nextOffset(offset, limit) {
        return offset + limit;
    }

    /** Whether another page exists beyond the rows already loaded (offset already advanced past the last fetched page). */
    function hasMore(offsetAfterPage, total) {
        return offsetAfterPage < total;
    }

    // ------------------------------------------------------------------
    // Pure logic: message truncation (client-side only -- the API sends
    // the FULL message; see gateway/dashboard/routes.py's
    // `_serialize_critical_row` docstring)
    // ------------------------------------------------------------------

    /** Truncates to `maxLength` chars (default 120) with a "..." suffix; a shorter/equal-length message is returned unchanged. Coerces null/undefined to "". */
    function truncateMessage(message, maxLength) {
        const limit = typeof maxLength === "number" ? maxLength : MESSAGE_TRUNCATE_LENGTH;
        const str = message === null || message === undefined ? "" : String(message);
        if (str.length <= limit) {
            return str;
        }
        return str.slice(0, limit) + "...";
    }

    // ------------------------------------------------------------------
    // Pure logic: API URL construction
    // ------------------------------------------------------------------

    function buildSummaryUrl(windowValue) {
        return "/api/dashboard/summary?window=" + encodeURIComponent(windowValue);
    }

    function buildCriticalUrl(windowValue, offset, limit) {
        return (
            "/api/dashboard/critical?window=" + encodeURIComponent(windowValue) +
            "&offset=" + encodeURIComponent(offset) +
            "&limit=" + encodeURIComponent(limit)
        );
    }

    // ------------------------------------------------------------------
    // Pure logic: small display helpers
    // ------------------------------------------------------------------

    /** Em dash for a null/empty/missing client, per the brief. */
    function clientDisplayValue(client) {
        return client ? client : "—";
    }

    // ------------------------------------------------------------------
    // API error type + fetch helpers
    // ------------------------------------------------------------------

    function DashboardApiError(status, body) {
        this.name = "DashboardApiError";
        this.status = status;
        this.body = body;
        this.message = (body && (body.message || body.error)) || "HTTP " + status;
    }
    DashboardApiError.prototype = Object.create(Error.prototype);
    DashboardApiError.prototype.constructor = DashboardApiError;

    /** GETs `url`, parses JSON, and rejects with a `DashboardApiError` (carrying `.status`/`.body`) on a non-2xx response -- this is what a 503 from Task 22's `_db_unavailable_response` shape surfaces as. */
    function fetchJson(url) {
        return fetchWithTimeout(url, { credentials: "same-origin" }, FETCH_TIMEOUT_MS).then(function (resp) {
            return resp
                .json()
                .catch(function () {
                    return null;
                })
                .then(function (body) {
                    if (!resp.ok) {
                        throw new DashboardApiError(resp.status, body);
                    }
                    return body;
                });
        });
    }

    function fetchSummary(windowValue) {
        return fetchJson(buildSummaryUrl(windowValue));
    }

    function fetchCritical(windowValue, offset, limit) {
        return fetchJson(buildCriticalUrl(windowValue, offset, limit));
    }

    // ==================================================================
    // DOM rendering / wiring -- browser only (see file header). Nothing
    // below this line runs unless init() is called.
    // ==================================================================

    const state = {
        windowValue: DEFAULT_WINDOW,
        offset: 0,
        limit: DEFAULT_LIMIT,
        total: 0,
        lastUpdated: null, // epoch seconds of the last successful fetch
        refreshTimer: null,
        tickTimer: null,
    };

    let els = null;

    function renderCards(counts) {
        els.cardsContainer.textContent = "";
        LEVELS_ORDER.forEach(function (level) {
            const count = (counts && counts[level]) || 0;

            const card = document.createElement("div");
            card.className = "dashboard-card";
            card.id = "dashboard-card-" + level;
            card.setAttribute("data-level", level);

            const countEl = document.createElement("div");
            countEl.className = "dashboard-card-count";
            countEl.id = "dashboard-card-" + level + "-count";
            countEl.textContent = String(count);

            const labelEl = document.createElement("div");
            labelEl.className = "dashboard-card-label";
            labelEl.textContent = LEVEL_LABELS[level] || level;

            card.appendChild(countEl);
            card.appendChild(labelEl);

            if (level === "critical") {
                // Pulse ONLY while count > 0 -- toggled here, never via a
                // bare CSS selector, so a critical card at 0 never pulses.
                if (count > 0) {
                    card.classList.add("pulse");
                }
                card.addEventListener("click", function () {
                    els.criticalSection.scrollIntoView({ behavior: "smooth", block: "start" });
                });
            }

            els.cardsContainer.appendChild(card);
        });
    }

    function createTimeCell(epochSeconds) {
        const td = document.createElement("td");
        td.className = "col-time";

        const rel = document.createElement("span");
        rel.className = "time-relative";
        rel.textContent = timeAgo(epochSeconds);

        const abs = document.createElement("span");
        abs.className = "time-absolute";
        abs.textContent = formatLocalizedTime(epochSeconds, getSelectedTimezone());

        td.appendChild(rel);
        td.appendChild(abs);
        return td;
    }

    function createClientCell(client) {
        const td = document.createElement("td");
        td.className = "col-client";

        const pill = document.createElement("span");
        const hasClient = !!client;
        pill.className = "client-pill" + (hasClient ? "" : " client-pill-empty");
        pill.textContent = clientDisplayValue(client);

        td.appendChild(pill);
        return td;
    }

    function createSourceCell(source) {
        const td = document.createElement("td");
        td.className = "col-source mono";
        td.textContent = source === null || source === undefined ? "" : String(source);
        return td;
    }

    function createAlertCell(title, message) {
        const td = document.createElement("td");
        td.className = "col-alert";

        const titleEl = document.createElement("span");
        titleEl.className = "alert-cell-title";
        titleEl.textContent = title === null || title === undefined ? "" : String(title);

        const msgEl = document.createElement("span");
        msgEl.className = "alert-cell-message";
        msgEl.textContent = truncateMessage(message);

        td.appendChild(titleEl);
        td.appendChild(msgEl);
        return td;
    }

    function createRow(alert) {
        const tr = document.createElement("tr");
        tr.dataset.alertId = String(alert.id);

        tr.appendChild(createTimeCell(alert.timestamp));
        tr.appendChild(createClientCell(alert.client));
        tr.appendChild(createSourceCell(alert.source));
        tr.appendChild(createAlertCell(alert.title, alert.message));

        tr.addEventListener("click", function () {
            openModal(alert);
        });

        return tr;
    }

    function renderCriticalRows(alerts, append) {
        if (!append) {
            els.tbody.textContent = "";
        }
        alerts.forEach(function (alert) {
            els.tbody.appendChild(createRow(alert));
        });
    }

    function updateEmptyAndLoadMore() {
        const isEmpty = state.total === 0;
        els.emptyState.hidden = !isEmpty;
        els.tableWrap.hidden = isEmpty;
        els.loadMoreBtn.hidden = isEmpty || !hasMore(state.offset, state.total);
    }

    function setDefinitionField(id, value) {
        const el = document.getElementById(id);
        el.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    }

    function renderMetadataTable(metadata) {
        els.modalMetadataTbody.textContent = "";
        const keys = metadata && typeof metadata === "object" ? Object.keys(metadata) : [];

        if (keys.length === 0) {
            const tr = document.createElement("tr");
            const td = document.createElement("td");
            td.colSpan = 2;
            td.textContent = "(none)";
            tr.appendChild(td);
            els.modalMetadataTbody.appendChild(tr);
            return;
        }

        keys.forEach(function (key) {
            const tr = document.createElement("tr");

            const tdKey = document.createElement("td");
            tdKey.textContent = key;

            const tdVal = document.createElement("td");
            const value = metadata[key];
            tdVal.textContent = value !== null && typeof value === "object" ? JSON.stringify(value) : String(value);

            tr.appendChild(tdKey);
            tr.appendChild(tdVal);
            els.modalMetadataTbody.appendChild(tr);
        });
    }

    function openModal(alert) {
        // The critical-alerts endpoint only ever returns level='critical'
        // rows (gateway/dashboard/routes.py's `_critical_rows_query`
        // filters `level = 'critical'` and the row shape has no `level`
        // field at all) -- populated explicitly anyway, rather than left
        // as static template markup, both per the brief's literal
        // "populate ... level badge" instruction and so this keeps working
        // unmodified if this table's scope ever widens.
        els.modalLevelBadge.textContent = "Critical";
        els.modalLevelBadge.className = "badge badge-critical";

        setDefinitionField("dashboard-modal-field-id", alert.id);
        setDefinitionField("dashboard-modal-field-timestamp", alert.timestamp);
        setDefinitionField("dashboard-modal-field-category", alert.category);
        setDefinitionField("dashboard-modal-field-source", alert.source);
        setDefinitionField("dashboard-modal-field-client", alert.client);
        setDefinitionField("dashboard-modal-field-ingestion-source", alert.ingestion_source);

        els.modalMessage.textContent = alert.message === null || alert.message === undefined ? "" : String(alert.message);

        renderMetadataTable(alert.metadata);

        // Safe despite being a <pre>: JSON.stringify's output is assigned
        // via textContent, never innerHTML -- see file header.
        els.modalRawJson.textContent = JSON.stringify(alert, null, 2);

        els.modal.showModal();
    }

    function showErrorBanner(message) {
        els.errorBanner.textContent = message;
        els.errorBanner.hidden = false;
    }

    function hideErrorBanner() {
        els.errorBanner.hidden = true;
        els.errorBanner.textContent = "";
    }

    function handleFetchError(err) {
        if (err && err.name === "DashboardApiError" && err.status === 503) {
            const msg =
                (err.body && err.body.message) ||
                "Dashboard data is temporarily unavailable; please try again shortly.";
            showErrorBanner(msg);
            return;
        }
        if (err && err.name === "AbortError") {
            showErrorBanner("Dashboard request timed out. Retrying shortly.");
            return;
        }
        showErrorBanner("Unable to load dashboard data. Please try refreshing.");
    }

    function updateUpdatedIndicator() {
        if (state.lastUpdated === null) {
            els.updatedIndicator.textContent = "";
            return;
        }
        els.updatedIndicator.textContent = "Updated " + timeAgo(state.lastUpdated);
    }

    /** Full (re)load: resets pagination to the first page and replaces the table -- used on initial load, timeframe change, and each 60s auto-refresh tick. */
    function loadDashboard(windowValue) {
        state.windowValue = windowValue;
        state.offset = 0;

        return Promise.all([fetchSummary(windowValue), fetchCritical(windowValue, 0, state.limit)])
            .then(function (results) {
                const summary = results[0];
                const critical = results[1];

                renderCards(summary.counts);
                renderCriticalRows(critical.alerts, false);
                state.total = critical.total;
                state.offset = nextOffset(0, state.limit);
                updateEmptyAndLoadMore();

                state.lastUpdated =
                    typeof summary.generated_at === "number" ? summary.generated_at : Math.floor(Date.now() / 1000);
                updateUpdatedIndicator();
                hideErrorBanner();
            })
            .catch(handleFetchError);
    }

    /** "Load more": appends the next page's rows rather than replacing the table. */
    function loadMore() {
        return fetchCritical(state.windowValue, state.offset, state.limit)
            .then(function (critical) {
                renderCriticalRows(critical.alerts, true);
                state.total = critical.total;
                state.offset = nextOffset(state.offset, state.limit);
                updateEmptyAndLoadMore();
                hideErrorBanner();
            })
            .catch(handleFetchError);
    }

    function onWindowChange() {
        let value = els.windowSelect.value;
        if (!isValidWindow(value)) {
            value = DEFAULT_WINDOW;
            els.windowSelect.value = value;
        }
        persistWindow(value);
        loadDashboard(value);
    }

    function init() {
        els = {
            windowSelect: document.getElementById("dashboard-window-select"),
            updatedIndicator: document.getElementById("dashboard-updated"),
            errorBanner: document.getElementById("dashboard-error-banner"),
            cardsContainer: document.getElementById("dashboard-cards"),
            criticalSection: document.getElementById("dashboard-critical-section"),
            emptyState: document.getElementById("dashboard-empty-state"),
            tableWrap: document.getElementById("dashboard-table-wrap"),
            tbody: document.getElementById("dashboard-critical-tbody"),
            loadMoreBtn: document.getElementById("dashboard-load-more"),
            modal: document.getElementById("dashboard-alert-modal"),
            modalClose: document.getElementById("dashboard-modal-close"),
            modalLevelBadge: document.getElementById("dashboard-modal-level-badge"),
            modalMessage: document.getElementById("dashboard-modal-message"),
            modalMetadataTbody: document.getElementById("dashboard-modal-metadata-tbody"),
            modalRawJson: document.getElementById("dashboard-modal-raw-json"),
        };

        if (!els.windowSelect) {
            // Defensive no-op: this script is only ever included on
            // dashboard.html, but degrade gracefully rather than throwing
            // if it somehow loads elsewhere.
            return;
        }

        state.limit = DEFAULT_LIMIT;

        const persisted = readPersistedWindow();
        els.windowSelect.value = persisted;

        els.windowSelect.addEventListener("change", onWindowChange);
        els.loadMoreBtn.addEventListener("click", function () {
            loadMore();
        });
        els.modalClose.addEventListener("click", function () {
            els.modal.close();
        });

        loadDashboard(persisted);

        state.tickTimer = window.setInterval(updateUpdatedIndicator, UPDATED_TICK_MS);
        // Page Visibility API: the fetch itself is skipped (not just its
        // rendering) while the tab is hidden, per the brief.
        state.refreshTimer = window.setInterval(function () {
            if (document.hidden) {
                return;
            }
            loadDashboard(state.windowValue);
        }, AUTO_REFRESH_MS);
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    }

    // ------------------------------------------------------------------
    // Export (mirrors common.js's own dual browser/Node export seam)
    // ------------------------------------------------------------------

    const AlertManagerDashboard = {
        VALID_WINDOWS: VALID_WINDOWS,
        DEFAULT_WINDOW: DEFAULT_WINDOW,
        DEFAULT_LIMIT: DEFAULT_LIMIT,
        MESSAGE_TRUNCATE_LENGTH: MESSAGE_TRUNCATE_LENGTH,
        LEVELS_ORDER: LEVELS_ORDER,
        isValidWindow: isValidWindow,
        readPersistedWindow: readPersistedWindow,
        persistWindow: persistWindow,
        nextOffset: nextOffset,
        hasMore: hasMore,
        truncateMessage: truncateMessage,
        buildSummaryUrl: buildSummaryUrl,
        buildCriticalUrl: buildCriticalUrl,
        clientDisplayValue: clientDisplayValue,
        DashboardApiError: DashboardApiError,
        fetchSummary: fetchSummary,
        fetchCritical: fetchCritical,
    };

    window.AlertManagerDashboard = AlertManagerDashboard;

    if (typeof module !== "undefined" && module.exports) {
        module.exports = AlertManagerDashboard;
    }
})(typeof window !== "undefined" ? window : globalThis);
