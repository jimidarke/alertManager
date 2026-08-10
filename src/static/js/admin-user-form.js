/**
 * Alert Manager - admin user_form.html page script (Task 27, revised
 * post-review)
 *
 * Drives templates/admin/user_form.html's repeatable scope-row fieldset:
 * "add another scope row" and per-row "Remove". Originally an inline
 * <script> block inside the template itself; pulled out into this static
 * file so the one invariant this code exists to protect has real,
 * `node:test`-driven regression coverage (tests/js/test_admin_user_form.mjs)
 * instead of relying on source reading alone -- see "THE invariant" below
 * for exactly what a first version of this code got wrong, and how.
 *
 * --------------------------------------------------------------------
 * THE invariant this file exists to protect (read before editing)
 * --------------------------------------------------------------------
 * `src/gateway/admin/routes.py`'s `_parse_scope_pairs()` pairs the Nth
 * submitted `scope_client` value (a repeated field, no index in its own
 * name -- browsers serialize repeated fields in document order) with
 * whatever field is literally named `scope_permissions_N`. It assumes a
 * row's position among the OTHER rows still in the form (0-based,
 * document order) always equals the numeric suffix on that row's
 * permission-checkbox `name` attributes.
 *
 * Removing a row breaks that unless every REMAINING row's checkboxes are
 * renamed afterward to stay contiguous with their new position -- this is
 * true for removing ANY row, not just a non-last one: an earlier version
 * of this file deleted the DOM node on "Remove" and never renumbered what
 * was left. Concretely, with 3 rows (acme/logs:read @0, beta/alerts:write
 * @1, gamma/logs:read @2), removing the FIRST row left beta's and gamma's
 * checkboxes still named `_1`/`_2` while `scope_client` now submits
 * `["beta", "gamma"]` at positions 0/1 -- `_parse_scope_pairs()` looks for
 * `scope_permissions_0` (doesn't exist -> beta's grant silently vanishes)
 * and `scope_permissions_1` (still exists, but it's BETA's old checkbox --
 * gamma silently inherits beta's alerts:write instead of its own
 * logs:read). Removing the first of only 2 rows is worse: the sole
 * survivor's permissions map to a nonexistent field and are dropped
 * entirely, which trips `update_user()`'s "admin emptied every scope row"
 * path and REVOKES THE USER'S ENTIRE PERSONAL SCOPE ROLE.
 *
 * The fix: `renumberScopeRows()` below re-derives every remaining row's
 * checkbox names from its CURRENT position, called after both "add" and
 * "remove" (not just once at page load) -- so this stays correct
 * regardless of how many rows were added/removed, and in what order.
 *
 * --------------------------------------------------------------------
 * Testability (mirrors common.js / dashboard.js exactly)
 * --------------------------------------------------------------------
 * `renumberScopeRows(rows)` is deliberately parameterized over anything
 * shaped like `{ querySelectorAll(selector) -> ArrayLike<{name}> }` rather
 * than reading `document`/a real container directly -- a real DOM
 * NodeList of `.scope-row` elements satisfies that shape natively in the
 * browser (Element.querySelectorAll), and a plain fake object satisfies
 * it under plain Node with no DOM/jsdom needed (this repo has neither --
 * see common.js's own header comment on why: no package.json, framework-
 * free by convention). The SAME function runs both places, so a passing
 * test is proof about the actual shipped renumbering logic, not a
 * reimplementation of it that could itself drift out of sync.
 *
 * `init()` and everything it wires (getElementById, appendChild,
 * addEventListener, innerHTML-based row cloning) is DOM/browser-only,
 * gated the same way dashboard.js's own init() is -- only invoked when
 * `document` exists, so requiring this module under plain Node is
 * side-effect-free and exercises only `renumberScopeRows`. The row-add
 * innerHTML-cloning path is therefore a documented gap here (same class
 * dashboard.js's own header comment already carries for its DOM
 * rendering), verified by source reading + the structural/element-ID
 * checks in tests/test_gateway_admin_templates.py, not a real-browser test.
 */

(function (window) {
    "use strict";

    // ------------------------------------------------------------------
    // Pure logic: scope-row checkbox renumbering
    // ------------------------------------------------------------------

    /**
     * Rewrites every scope-permission checkbox's `name` to
     * `"scope_permissions_" + <row's 0-based position in `rows`>`, for
     * every row in `rows`, in order. A row's checkboxes all share one name
     * (differentiated by `value`, one per permission) -- see
     * user_form.html's scope-row markup -- so every checkbox in a row gets
     * the identical new name.
     *
     * @param {ArrayLike<{querySelectorAll: function(string): ArrayLike<{name: string}>}>} rows
     *   Real `.scope-row` DOM elements (or NodeList) in the browser; a
     *   plain array of fake row objects under test.
     */
    function renumberScopeRows(rows) {
        Array.prototype.forEach.call(rows, function (row, index) {
            var checkboxes = row.querySelectorAll('input[type="checkbox"]');
            Array.prototype.forEach.call(checkboxes, function (checkbox) {
                checkbox.name = "scope_permissions_" + index;
            });
        });
    }

    /**
     * New-mode "Service account" checkbox convenience defaults: when
     * checked, un-checks "Require password change" and checks "Issue a
     * Bearer token". A one-shot nudge on the checkbox's own `change` event
     * -- NOT a bound sync -- so both target fields stay independently
     * user-editable afterward (unchecking "Service account" again is a
     * no-op, it never forces the other two fields back).
     *
     * Parameterized the same way renumberScopeRows() is: over anything
     * shaped like `{ checked: bool }` rather than reading real checkbox
     * elements directly, so it's testable under plain Node with fakes.
     *
     * @param {{mustChangePassword: {checked: boolean}, issueToken: {checked: boolean}}} fields
     * @param {boolean} serviceAccountChecked
     */
    function applyServiceAccountDefaults(fields, serviceAccountChecked) {
        if (serviceAccountChecked) {
            fields.mustChangePassword.checked = false;
            fields.issueToken.checked = true;
        }
    }

    // ==================================================================
    // DOM rendering / wiring -- browser only (see file header). Nothing
    // below this line runs unless init() is called.
    // ==================================================================

    function init() {
        var container = document.getElementById("scope-rows-container");
        var addButton = document.getElementById("add-scope-row-btn");
        var template = document.getElementById("scope-row-template");
        if (!container || !addButton || !template) {
            return;
        }

        addButton.addEventListener("click", function () {
            var nextIndex = container.querySelectorAll(".scope-row").length;
            var html = template.innerHTML.split("__ROW_INDEX__").join(String(nextIndex));
            var wrapper = document.createElement("div");
            wrapper.innerHTML = html.trim();
            container.appendChild(wrapper.firstElementChild);
            // Belt-and-suspenders: nextIndex already made the freshly
            // added row's own name correct, but running the SAME
            // renumber pass here (rather than trusting that alone) keeps
            // renumberScopeRows() the single source of truth regardless
            // of any earlier removal history.
            renumberScopeRows(container.querySelectorAll(".scope-row"));
        });

        // Event delegation: works for both server-rendered rows and rows
        // added later by the button above.
        container.addEventListener("click", function (event) {
            var button = event.target.closest(".scope-row-remove");
            if (!button) {
                return;
            }
            var row = button.closest(".scope-row");
            if (row) {
                row.remove();
                // THE fix -- see file header. Must run after every
                // removal, not just once, and must recompute from the
                // live DOM (not a persisted counter) so any sequence of
                // adds/removes stays correct.
                renumberScopeRows(container.querySelectorAll(".scope-row"));
            }
        });

        var serviceAccountCheckbox = document.getElementById("user-form-service-account");
        var mustChangePasswordCheckbox = document.getElementById("user-form-must-change-password");
        var issueTokenCheckbox = document.getElementById("user-form-issue-token");
        if (serviceAccountCheckbox && mustChangePasswordCheckbox && issueTokenCheckbox) {
            serviceAccountCheckbox.addEventListener("change", function () {
                applyServiceAccountDefaults(
                    { mustChangePassword: mustChangePasswordCheckbox, issueToken: issueTokenCheckbox },
                    serviceAccountCheckbox.checked
                );
            });
        }
    }

    if (typeof document !== "undefined") {
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", init);
        } else {
            init();
        }
    }

    // ------------------------------------------------------------------
    // Export (mirrors common.js's / dashboard.js's own dual browser/Node
    // export seam)
    // ------------------------------------------------------------------

    const AlertManagerAdminUserForm = {
        renumberScopeRows: renumberScopeRows,
        applyServiceAccountDefaults: applyServiceAccountDefaults,
    };

    window.AlertManagerAdminUserForm = AlertManagerAdminUserForm;

    if (typeof module !== "undefined" && module.exports) {
        module.exports = AlertManagerAdminUserForm;
    }
})(typeof window !== "undefined" ? window : globalThis);
