/**
 * Alert Manager - Dashboard page script (Task 25)
 *
 * Drives templates/dashboard.html: the timeframe <select> (persisted to
 * localStorage['dashboard_window']), the 5 level cards (multi-select
 * filter toggles, persisted to localStorage['dashboard_levels'], default
 * Critical only), the client -> source grouped view (nested <details>,
 * latest <=30 alerts per source, collapse choices persisted to
 * localStorage['dashboard_collapse']), the row-detail <dialog> modal, the
 * "Updated Xs ago" indicator, and 60s auto-refresh (skipped while the tab
 * is hidden). Calls `GET /api/dashboard/summary` and
 * `GET /api/dashboard/groups` via common.js's `fetchWithTimeout`.
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
    // (`_WINDOW_SECONDS`, `DEFAULT_WINDOW`, `_LEVELS`, `DEFAULT_LEVELS`).
    // Keep these in sync with that module if its enum/defaults ever change.
    // ------------------------------------------------------------------
    const WINDOW_STORAGE_KEY = "dashboard_window";
    const LEVELS_STORAGE_KEY = "dashboard_levels";
    const COLLAPSE_STORAGE_KEY = "dashboard_collapse";
    const VALID_WINDOWS = ["1h", "6h", "12h", "24h", "7d", "14d", "30d", "all"];
    const DEFAULT_WINDOW = "24h";
    const DEFAULT_LEVELS = ["critical"];
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
    // Pure logic: level-filter selection (the cards are toggles)
    // ------------------------------------------------------------------

    function getStore(storage) {
        return storage || (typeof window !== "undefined" ? window.localStorage : null);
    }

    /** Filters to known levels, dedupes, and returns them in canonical LEVELS_ORDER. */
    function normalizeLevels(levels) {
        const list = Array.isArray(levels) ? levels : [];
        return LEVELS_ORDER.filter(function (level) {
            return list.indexOf(level) !== -1;
        });
    }

    /**
     * Reads `localStorage['dashboard_levels']` (a JSON array). Falls back to
     * DEFAULT_LEVELS when unset, unparseable, not an array, or storage
     * throws. An explicitly stored EMPTY array is honored (the user turned
     * every level off) -- that is a valid selection, not a fallback case.
     */
    function readPersistedLevels(storage) {
        const store = getStore(storage);
        if (!store) {
            return DEFAULT_LEVELS.slice();
        }
        try {
            const parsed = JSON.parse(store.getItem(LEVELS_STORAGE_KEY));
            return Array.isArray(parsed) ? normalizeLevels(parsed) : DEFAULT_LEVELS.slice();
        } catch (e) {
            return DEFAULT_LEVELS.slice();
        }
    }

    function persistLevels(levels, storage) {
        const store = getStore(storage);
        if (!store) {
            return;
        }
        try {
            store.setItem(LEVELS_STORAGE_KEY, JSON.stringify(normalizeLevels(levels)));
        } catch (e) {
            // degrade silently
        }
    }

    /** Adds `level` if absent, removes it if present; result is always in canonical order. */
    function toggleLevel(levels, level) {
        const current = normalizeLevels(levels);
        if (current.indexOf(level) !== -1) {
            return current.filter(function (l) {
                return l !== level;
            });
        }
        return normalizeLevels(current.concat([level]));
    }

    /** "Critical, Warning" -- human list of the selected levels. */
    function levelsLabel(levels) {
        return normalizeLevels(levels)
            .map(function (level) {
                return LEVEL_LABELS[level];
            })
            .join(", ");
    }

    // ------------------------------------------------------------------
    // Pure logic: group collapse state
    //
    // `collapse` is a {key: boolean} map of EXPLICIT user choices only.
    // A group with no explicit choice falls back to its default: open when
    // it has rows matching the selected levels, collapsed otherwise.
    // ------------------------------------------------------------------

    function clientKey(client) {
        return "c:" + client;
    }

    function sourceKey(client, source) {
        return "s:" + client + "\u0000" + source;
    }

    function isGroupOpen(key, matched, collapse) {
        if (collapse && Object.prototype.hasOwnProperty.call(collapse, key)) {
            return collapse[key] === true;
        }
        return matched > 0;
    }

    /**
     * Returns a new collapse map after the group `key` ended up `open`. A
     * state equal to the default is stored as "no explicit choice" (key
     * removed), so the group keeps following its default afterwards; a
     * state differing from the default is stored explicitly.
     */
    function recordCollapseChoice(collapse, key, open, matched) {
        const next = Object.assign({}, collapse);
        if (open === matched > 0) {
            delete next[key];
        } else {
            next[key] = open;
        }
        return next;
    }

    function readCollapseState(storage) {
        const store = getStore(storage);
        if (!store) {
            return {};
        }
        try {
            const parsed = JSON.parse(store.getItem(COLLAPSE_STORAGE_KEY));
            return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
        } catch (e) {
            return {};
        }
    }

    function persistCollapseState(collapse, storage) {
        const store = getStore(storage);
        if (!store) {
            return;
        }
        try {
            store.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(collapse));
        } catch (e) {
            // degrade silently
        }
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

    /** `levels=` is always sent, even empty -- an empty value means "counts only" server-side, whereas omitting it would default to critical. */
    function buildGroupsUrl(windowValue, levels) {
        return (
            "/api/dashboard/groups?window=" + encodeURIComponent(windowValue) +
            "&levels=" + encodeURIComponent(normalizeLevels(levels).join(","))
        );
    }

    // ------------------------------------------------------------------
    // Pure logic: small display helpers
    // ------------------------------------------------------------------

    /** Group-header label for the clientless group (master_admin only ever sees it). */
    function clientDisplayValue(client) {
        return client ? client : "(no client)";
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

    function fetchGroups(windowValue, levels) {
        return fetchJson(buildGroupsUrl(windowValue, levels));
    }

    // ==================================================================
    // DOM rendering / wiring -- browser only (see file header). Nothing
    // below this line runs unless init() is called.
    // ==================================================================

    const state = {
        windowValue: DEFAULT_WINDOW,
        levels: DEFAULT_LEVELS.slice(),
        collapse: {},
        lastUpdated: null, // epoch seconds of the last successful fetch
        refreshTimer: null,
        tickTimer: null,
    };

    let els = null;

    function renderCards(counts) {
        els.cardsContainer.textContent = "";
        LEVELS_ORDER.forEach(function (level) {
            const count = (counts && counts[level]) || 0;
            const selected = state.levels.indexOf(level) !== -1;

            const card = document.createElement("button");
            card.type = "button";
            card.className = "dashboard-card" + (selected ? " selected" : "");
            card.id = "dashboard-card-" + level;
            card.setAttribute("data-level", level);
            card.setAttribute("aria-pressed", selected ? "true" : "false");
            card.title = (selected ? "Hide " : "Show ") + LEVEL_LABELS[level] + " alerts";

            const countEl = document.createElement("div");
            countEl.className = "dashboard-card-count";
            countEl.id = "dashboard-card-" + level + "-count";
            countEl.textContent = String(count);

            const labelEl = document.createElement("div");
            labelEl.className = "dashboard-card-label";
            labelEl.textContent = LEVEL_LABELS[level] || level;

            card.appendChild(countEl);
            card.appendChild(labelEl);

            // Pulse ONLY while the critical count is > 0 -- toggled here,
            // never via a bare CSS selector, so a critical card at 0 never
            // pulses.
            if (level === "critical" && count > 0) {
                card.classList.add("pulse");
            }

            card.addEventListener("click", function () {
                onLevelToggle(level);
            });

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
        td.title = abs.textContent;

        td.appendChild(rel);
        td.appendChild(abs);
        return td;
    }

    function createLevelBadge(level, text) {
        const badge = document.createElement("span");
        badge.className = "badge badge-" + level;
        badge.textContent = text;
        return badge;
    }

    function createLevelCell(level) {
        const td = document.createElement("td");
        td.className = "col-level";
        td.appendChild(createLevelBadge(level, LEVEL_LABELS[level] || String(level)));
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
        tr.appendChild(createLevelCell(alert.level));
        tr.appendChild(createAlertCell(alert.title, alert.message));

        tr.addEventListener("click", function () {
            openModal(alert);
        });

        return tr;
    }

    /** Per-level count chips for a group header; levels outside the current selection are muted. */
    function createCountBadges(counts) {
        const wrap = document.createElement("span");
        wrap.className = "group-badges";
        LEVELS_ORDER.forEach(function (level) {
            const count = (counts && counts[level]) || 0;
            if (count === 0) {
                return;
            }
            const badge = createLevelBadge(level, count + " " + level);
            if (state.levels.indexOf(level) === -1) {
                badge.classList.add("badge-muted");
            }
            wrap.appendChild(badge);
        });
        return wrap;
    }

    function createMeta(text) {
        const meta = document.createElement("span");
        meta.className = "group-meta tiny";
        meta.textContent = text;
        return meta;
    }

    /**
     * Builds a <details> group whose open state comes from isGroupOpen() and
     * whose user toggles are recorded via recordCollapseChoice(). The
     * `toggle` event also fires for the initial programmatic `open`, but
     * that state already equals what isGroupOpen() chose, so recording it
     * is a no-op for explicit choices and a no-op for defaults.
     */
    function createGroup(className, key, matched, summaryChildren) {
        const details = document.createElement("details");
        details.className = className + (matched > 0 ? "" : " group-dim");
        details.open = isGroupOpen(key, matched, state.collapse);
        details.dataset.groupKey = key;

        const summary = document.createElement("summary");
        summary.className = "group-summary";
        summaryChildren.forEach(function (child) {
            summary.appendChild(child);
        });
        details.appendChild(summary);

        details.addEventListener("toggle", function () {
            const next = recordCollapseChoice(state.collapse, key, details.open, matched);
            if (JSON.stringify(next) !== JSON.stringify(state.collapse)) {
                state.collapse = next;
                persistCollapseState(state.collapse);
            }
        });

        return details;
    }

    function createSourceGroup(clientName, group) {
        const name = document.createElement("span");
        name.className = "group-name mono";
        name.textContent = group.source;

        const children = [name, createCountBadges(group.counts), createMeta("last " + timeAgo(group.last_seen))];
        if (group.matched > group.alerts.length) {
            children.push(createMeta("showing " + group.alerts.length + " of " + group.matched));
        }

        const details = createGroup("group-source", sourceKey(clientName, group.source), group.matched, children);

        if (group.alerts.length === 0) {
            let total = 0;
            LEVELS_ORDER.forEach(function (level) {
                total += (group.counts && group.counts[level]) || 0;
            });
            const empty = document.createElement("div");
            empty.className = "group-empty tiny";
            empty.textContent =
                state.levels.length === 0
                    ? "No levels selected -- " + total + " alerts in window"
                    : "No " + levelsLabel(state.levels) + " alerts -- " + total + " other alerts in window";
            details.appendChild(empty);
            return details;
        }

        const wrap = document.createElement("div");
        wrap.className = "dashboard-table-wrap";
        const table = document.createElement("table");
        table.className = "group-table";
        const tbody = document.createElement("tbody");
        group.alerts.forEach(function (alert) {
            tbody.appendChild(createRow(alert));
        });
        table.appendChild(tbody);
        wrap.appendChild(table);
        details.appendChild(wrap);
        return details;
    }

    function createClientGroup(clientGroup) {
        const name = document.createElement("span");
        name.className = "group-name" + (clientGroup.client ? "" : " group-name-empty");
        name.textContent = clientDisplayValue(clientGroup.client);

        const sourceCount = clientGroup.sources.length;
        const details = createGroup("group-client", clientKey(clientGroup.client), clientGroup.matched, [
            name,
            createCountBadges(clientGroup.counts),
            createMeta(sourceCount + (sourceCount === 1 ? " source" : " sources")),
            createMeta("last " + timeAgo(clientGroup.last_seen)),
        ]);

        const body = document.createElement("div");
        body.className = "group-client-body";
        clientGroup.sources.forEach(function (group) {
            body.appendChild(createSourceGroup(clientGroup.client, group));
        });
        details.appendChild(body);
        return details;
    }

    /** Rebuilds the whole tree in one replaceChildren() call, so a refresh doesn't jump the scroll position. */
    function renderGroups(data) {
        const clients = (data && data.clients) || [];
        const fragment = document.createDocumentFragment();
        clients.forEach(function (clientGroup) {
            fragment.appendChild(createClientGroup(clientGroup));
        });
        els.groups.replaceChildren(fragment);

        els.emptyState.hidden = clients.length > 0;
        els.groupsShowing.textContent =
            state.levels.length === 0
                ? "No levels selected -- click a card above to show alerts"
                : "Latest " + ((data && data.per_group) || 30) + " per source: " + levelsLabel(state.levels);
    }

    function setAllGroupsOpen(open) {
        // Each assignment fires that group's own `toggle` listener, which
        // records the choice -- no separate bookkeeping needed here.
        els.groups.querySelectorAll("details").forEach(function (details) {
            details.open = open;
        });
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
        els.modalLevelBadge.textContent = LEVEL_LABELS[alert.level] || String(alert.level);
        els.modalLevelBadge.className = "badge badge-" + alert.level;

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

    /** Full (re)load of cards + groups -- used on initial load, timeframe/level change, and each 60s auto-refresh tick. */
    function loadDashboard(windowValue) {
        state.windowValue = windowValue;

        return Promise.all([fetchSummary(windowValue), fetchGroups(windowValue, state.levels)])
            .then(function (results) {
                const summary = results[0];
                const groups = results[1];

                renderCards(summary.counts);
                renderGroups(groups);

                state.lastUpdated =
                    typeof summary.generated_at === "number" ? summary.generated_at : Math.floor(Date.now() / 1000);
                updateUpdatedIndicator();
                hideErrorBanner();
            })
            .catch(handleFetchError);
    }

    function onLevelToggle(level) {
        state.levels = toggleLevel(state.levels, level);
        persistLevels(state.levels);
        loadDashboard(state.windowValue);
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
            groups: document.getElementById("dashboard-groups"),
            groupsShowing: document.getElementById("dashboard-groups-showing"),
            emptyState: document.getElementById("dashboard-empty-state"),
            expandAllBtn: document.getElementById("dashboard-expand-all"),
            collapseAllBtn: document.getElementById("dashboard-collapse-all"),
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

        state.levels = readPersistedLevels();
        state.collapse = readCollapseState();

        const persisted = readPersistedWindow();
        els.windowSelect.value = persisted;

        els.windowSelect.addEventListener("change", onWindowChange);
        els.expandAllBtn.addEventListener("click", function () {
            setAllGroupsOpen(true);
        });
        els.collapseAllBtn.addEventListener("click", function () {
            setAllGroupsOpen(false);
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
        DEFAULT_LEVELS: DEFAULT_LEVELS,
        MESSAGE_TRUNCATE_LENGTH: MESSAGE_TRUNCATE_LENGTH,
        LEVELS_ORDER: LEVELS_ORDER,
        isValidWindow: isValidWindow,
        readPersistedWindow: readPersistedWindow,
        persistWindow: persistWindow,
        normalizeLevels: normalizeLevels,
        readPersistedLevels: readPersistedLevels,
        persistLevels: persistLevels,
        toggleLevel: toggleLevel,
        levelsLabel: levelsLabel,
        clientKey: clientKey,
        sourceKey: sourceKey,
        isGroupOpen: isGroupOpen,
        recordCollapseChoice: recordCollapseChoice,
        readCollapseState: readCollapseState,
        persistCollapseState: persistCollapseState,
        truncateMessage: truncateMessage,
        buildSummaryUrl: buildSummaryUrl,
        buildGroupsUrl: buildGroupsUrl,
        clientDisplayValue: clientDisplayValue,
        DashboardApiError: DashboardApiError,
        fetchSummary: fetchSummary,
        fetchGroups: fetchGroups,
    };

    window.AlertManagerDashboard = AlertManagerDashboard;

    if (typeof module !== "undefined" && module.exports) {
        module.exports = AlertManagerDashboard;
    }
})(typeof window !== "undefined" ? window : globalThis);
