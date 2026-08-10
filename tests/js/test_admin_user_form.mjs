// Tests for src/static/js/admin-user-form.js (Task 27, post-review fix).
//
// Same harness as tests/js/test_common.mjs / tests/js/test_dashboard.mjs
// (Task 24/25): Node's built-in `node:test`/`node:assert`, no framework,
// no package.json -- see test_common.mjs's own header for the full "why".
//
// admin-user-form.js's own header comment documents the exact bug this
// file exists to pin down: routes.py's `_parse_scope_pairs()` pairs the
// Nth submitted `scope_client` value with whatever field is literally
// named `scope_permissions_N` (a row's position among the OTHERS still in
// the form, 0-based). Removing a row without renumbering the survivors'
// checkbox names breaks that pairing -- reviewer-reproduced concretely:
// with 3 rows (acme/logs:read @0, beta/alerts:write @1, gamma/logs:read
// @2), removing acme (first) left beta/gamma's checkboxes still named
// `_1`/`_2` while `scope_client` now submits `["beta","gamma"]` at
// positions 0/1 -- beta's grant silently vanishes (no field named `_0`)
// and gamma silently inherits BETA's old `_1` checkbox (alerts:write)
// instead of its own (logs:read). Removing the first of only 2 rows is
// worse: the survivor's permissions map to nothing and are dropped
// entirely, tripping update_user()'s "admin emptied every scope row" path
// and revoking the user's entire personal scope role.
//
// `renumberScopeRows(rows)` is the pure fix, parameterized over anything
// shaped like `{ querySelectorAll(selector) -> ArrayLike<{name}> }` --
// real DOM elements satisfy that natively in the browser; the fake row
// objects below satisfy it here with zero DOM/jsdom dependency (this repo
// has neither). This is the SAME function admin-user-form.js's real
// init() calls after every add/remove, not a reimplementation of it, so a
// passing test here is proof about the actual shipped logic.
//
// What this file deliberately does NOT test (documented gap, same class
// as dashboard.js's own DOM-wiring gap): the real innerHTML-based "clone
// the <template>, insert into the DOM" behavior of the "Add another
// scope" button, and the real click-event delegation wiring in init() --
// both need a real browser/DOM. Covered instead by
// tests/test_gateway_admin_templates.py's structural/element-ID checks
// and careful source reading (admin-user-form.js's init() is a small,
// direct translation of the same renumberScopeRows() calls tested here).
//
// Run with: node tests/js/test_admin_user_form.mjs

import assert from "node:assert/strict";
import { test } from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const modulePath = path.join(__dirname, "..", "..", "src", "static", "js", "admin-user-form.js");

const require = createRequire(import.meta.url);
const { renumberScopeRows, applyServiceAccountDefaults } = require(modulePath);

// ---------------------------------------------------------------------
// Fake "row" objects -- the minimal shape renumberScopeRows() actually
// calls: `.querySelectorAll(selector)` returning an array-like of
// `{name}` objects. The selector argument itself is ignored (the fake
// only ever holds the checkbox-representing objects a real
// `.scope-row` element's `querySelectorAll('input[type="checkbox"]')`
// would return), matching this codebase's established "inject the
// smallest fake matching exactly the interface used" pattern (see
// test_dashboard.mjs's makeFakeStorage).
// ---------------------------------------------------------------------

function makeFakeRow(checkboxNames) {
    const checkboxes = checkboxNames.map((name) => ({ name }));
    return {
        checkboxes, // exposed for assertions
        querySelectorAll() {
            return checkboxes;
        },
    };
}

// ---------------------------------------------------------------------
// Basic renumbering behavior
// ---------------------------------------------------------------------

test("already-contiguous rows are left unchanged (idempotent)", () => {
    const rows = [
        makeFakeRow(["scope_permissions_0"]),
        makeFakeRow(["scope_permissions_1"]),
        makeFakeRow(["scope_permissions_2"]),
    ];
    renumberScopeRows(rows);
    assert.equal(rows[0].checkboxes[0].name, "scope_permissions_0");
    assert.equal(rows[1].checkboxes[0].name, "scope_permissions_1");
    assert.equal(rows[2].checkboxes[0].name, "scope_permissions_2");
});

test("every checkbox within one row gets the identical new name", () => {
    // A row's permission checkboxes all share one name (differentiated by
    // `value`, one per permission) -- user_form.html's scope-row markup.
    const row = makeFakeRow(["scope_permissions_5", "scope_permissions_5", "scope_permissions_5"]);
    renumberScopeRows([row]);
    for (const checkbox of row.checkboxes) {
        assert.equal(checkbox.name, "scope_permissions_0");
    }
});

test("empty row list is a no-op", () => {
    assert.doesNotThrow(() => renumberScopeRows([]));
});

// ---------------------------------------------------------------------
// The reviewer's exact reproduction: 3 rows, remove the FIRST
// ---------------------------------------------------------------------

test("removing the first of 3 rows renumbers the survivors contiguously (reviewer's exact scenario)", () => {
    // Starting state: acme/logs:read @0, beta/alerts:write @1,
    // gamma/logs:read @2 -- the DOM after the browser deletes acme's row
    // (its own checkbox object is simply gone; beta/gamma's checkbox
    // objects are STILL the same objects, still carrying their stale
    // "_1"/"_2" names, exactly as a real .remove() would leave them
    // before this fix runs).
    const beta = makeFakeRow(["scope_permissions_1"]);
    beta.checkboxes[0].value = "alerts:write";
    const gamma = makeFakeRow(["scope_permissions_2"]);
    gamma.checkboxes[0].value = "logs:read";

    const remainingRowsInDomOrder = [beta, gamma]; // acme's row is gone

    renumberScopeRows(remainingRowsInDomOrder);

    // beta is now position 0 -- its OWN value (alerts:write) travels with
    // it under its NEW correct name. Nothing was dropped.
    assert.equal(beta.checkboxes[0].name, "scope_permissions_0");
    assert.equal(beta.checkboxes[0].value, "alerts:write");

    // gamma is now position 1 -- its OWN value (logs:read) travels with
    // it too, under ITS new correct name -- not beta's old field, and not
    // beta's old value. This is the exact swap the reviewer reproduced
    // against the unfixed backend: gamma silently inheriting beta's
    // alerts:write. After renumbering, gamma's checkbox is a distinct
    // object from beta's, correctly named, still carrying gamma's own
    // logs:read value.
    assert.equal(gamma.checkboxes[0].name, "scope_permissions_1");
    assert.equal(gamma.checkboxes[0].value, "logs:read");

    // And critically: the two rows never end up sharing a name (which
    // would be exactly the corruption -- two DIFFERENT clients' rows
    // both submitting under one field, silently merging/overwriting).
    assert.notEqual(beta.checkboxes[0].name, gamma.checkboxes[0].name);
});

// ---------------------------------------------------------------------
// Removal at every position: first, middle, last, down to one row
// ---------------------------------------------------------------------

test("removing the MIDDLE of 3 rows renumbers only the row after it", () => {
    // acme @0 stays untouched (still position 0); gamma was @2, now the
    // second (and last) remaining row -> position 1.
    const acme = makeFakeRow(["scope_permissions_0"]);
    const gamma = makeFakeRow(["scope_permissions_2"]);

    renumberScopeRows([acme, gamma]); // beta's row (was @1) is gone

    assert.equal(acme.checkboxes[0].name, "scope_permissions_0");
    assert.equal(gamma.checkboxes[0].name, "scope_permissions_1");
});

test("removing the LAST of 3 rows needs no renumbering (already safe, but must stay a no-op)", () => {
    const acme = makeFakeRow(["scope_permissions_0"]);
    const beta = makeFakeRow(["scope_permissions_1"]);

    renumberScopeRows([acme, beta]); // gamma's row (was @2) is gone

    assert.equal(acme.checkboxes[0].name, "scope_permissions_0");
    assert.equal(beta.checkboxes[0].name, "scope_permissions_1");
});

test("removing down to a single surviving row renumbers it to _0 (prevents the empty-personal-role-revoke bug)", () => {
    // The worst-case the reviewer flagged: only gamma (@2) survives after
    // acme and beta are both removed. Unfixed, gamma's checkbox stays
    // named "_2" while scope_client submits exactly one value at position
    // 0 -- _parse_scope_pairs() finds no "scope_permissions_0" field at
    // all, so gamma's permissions are dropped entirely (not just
    // mis-attributed), the submitted scope set goes empty, and
    // update_user() revokes the user's whole personal scope role as a
    // side effect.
    const gamma = makeFakeRow(["scope_permissions_2"]);
    gamma.checkboxes[0].value = "logs:read";

    renumberScopeRows([gamma]);

    assert.equal(gamma.checkboxes[0].name, "scope_permissions_0");
    assert.equal(gamma.checkboxes[0].value, "logs:read");
});

test("sequential removals (first, then what is now first again) stay correct across multiple calls", () => {
    // Simulates two separate "Remove" clicks in a row (removing rows one
    // at a time, renumbering after each -- exactly what init()'s click
    // handler does on every click, not just once at the end).
    const beta = makeFakeRow(["scope_permissions_1"]);
    const gamma = makeFakeRow(["scope_permissions_2"]);
    const delta = makeFakeRow(["scope_permissions_3"]);

    // First removal: acme (@0) is gone.
    renumberScopeRows([beta, gamma, delta]);
    assert.equal(beta.checkboxes[0].name, "scope_permissions_0");
    assert.equal(gamma.checkboxes[0].name, "scope_permissions_1");
    assert.equal(delta.checkboxes[0].name, "scope_permissions_2");

    // Second removal: what is now the first row (beta) is removed too.
    renumberScopeRows([gamma, delta]);
    assert.equal(gamma.checkboxes[0].name, "scope_permissions_0");
    assert.equal(delta.checkboxes[0].name, "scope_permissions_1");
});

// ---------------------------------------------------------------------
// applyServiceAccountDefaults: "Service account" checkbox convenience
// defaults on the new-mode form (must_change_password off, issue_token
// on) -- a one-shot nudge, not a bound sync.
// ---------------------------------------------------------------------

test("checking service account unchecks must-change-password and checks issue-token", () => {
    const fields = { mustChangePassword: { checked: true }, issueToken: { checked: false } };
    applyServiceAccountDefaults(fields, true);
    assert.equal(fields.mustChangePassword.checked, false);
    assert.equal(fields.issueToken.checked, true);
});

test("unchecking service account is a no-op (not a bound sync -- fields stay as the admin set them)", () => {
    const fields = { mustChangePassword: { checked: false }, issueToken: { checked: true } };
    applyServiceAccountDefaults(fields, false);
    assert.equal(fields.mustChangePassword.checked, false);
    assert.equal(fields.issueToken.checked, true);
});
