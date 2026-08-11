"""
Alert Manager - gateway.admin.routes

`admin_bp`: master_admin-only, session-only, server-rendered user
administration -- `GET /admin/users` (list), `GET/POST /admin/users/new`
(create), `GET/POST /admin/users/<username>/edit` (edit), `POST
.../disable`, `POST .../enable`, `POST .../reset-password`, `POST
.../tokens/new` (issue a Bearer token), `POST .../tokens/<id>/revoke`.
Every handler
is a thin orchestration of direct `UserStore` (Task 15) calls -- the same
primitives `tools/user-manager.py`'s CLI (Task 16) calls one at a time from
argv, just composed here from a web form instead. `UserStore` (and beneath
it the DB's own CHECK constraints) remains the single authority on what is
a valid user/role/scope; nothing here re-implements or duplicates that
validation.

No delete route in v1 (`disable` covers deactivation; `UserStore.delete_user()`
exists for other purposes -- CLI cleanup/testing -- and is deliberately not
wired to any route here, per the brief).

--------------------------------------------------------------------------
1. Auth: `require_master_admin` (Task 18/19) on every route
--------------------------------------------------------------------------
`gateway.principal.require_master_admin` already does exactly what this
whole blueprint needs: session-only auth (admin has no machine API in v1)
AND `g.principal.is_master_admin()`. No new decorator/policy needed here.

--------------------------------------------------------------------------
2. CSRF: automatic, no exemption
--------------------------------------------------------------------------
Unlike `gateway/api/__init__.py` (which calls `csrf.exempt(api_bp)` because
that blueprint never accepts a session cookie), this blueprint is
session-only and every mutating route is a POST -- exactly Flask-WTF
`CSRFProtect`'s default-protected method set. `gateway/__init__.py`'s
`create_app()` already calls `csrf.init_app(app)` globally; nothing in this
package opts out, so every POST below requires a valid `csrf_token` form
field the moment `admin_bp` is registered. Verified end-to-end in this
task's own tests (a POST without the token is rejected with 400), the same
way `tests/test_gateway_auth_routes.py` verifies it for `/login`.

--------------------------------------------------------------------------
3. THE self-lockout guard rails -- exact identity/role-membership mechanism
--------------------------------------------------------------------------
Task 22's review found a real bug from inferring "is this master_admin"
from a *scope* value (`visible_clients()`'s `ALL_CLIENTS` sentinel) instead
of `PrincipalData.is_master_admin()` directly -- the two read disjoint
fields (`scopes` vs. `roles`) and can disagree. This task carries the same
class of risk for its own two guard rails ("can't disable your own
account", "can't remove your own master_admin role"), so both are
implemented against real, freshly-fetched identity/role data, never an
inferred proxy:

- **Identity**: `g.principal.user_id` (an `int`, populated by
  `require_auth`'s `_resolve_session` from a live `UserStore.get_principal()`
  call made for THIS request) is compared against the TARGET account's own
  `id`/`user_id`, freshly fetched via `UserStore.get_user()` (disable) or
  `UserStore.get_principal()` (edit) in the same request -- never a
  username string compare alone (usernames are unique, so it would also be
  correct, but comparing the numeric primary key is the precise, unambiguous
  check the task brief asks for).
- **Role-membership effect**: the edit guard compares `MASTER_ADMIN_ROLE in
  current.roles` (the TARGET's real, just-fetched `PrincipalData.roles` --
  never `g.principal`'s own roles, and never a scope/permission check) against
  whether the role checkbox set the admin just submitted still contains it.
  Both sides of the comparison come from `PrincipalData.roles` /
  `MASTER_ADMIN_ROLE` (`userstore.MASTER_ADMIN_ROLE`, matching
  `PrincipalData.is_master_admin()`'s own definition exactly:
  `MASTER_ADMIN_ROLE in self.roles`) -- no scope, no `has_permission()`,
  no `visible_clients()` involved anywhere in this decision.

Rejection response: **400**, consistently, for every guard-rail and
validation failure in this blueprint (see section 5 below for why 400 was
chosen over flash+redirect).

Out of scope, explicitly (per the brief's "When You're in Over Your Head"):
role *deletion* (which would indirectly de-admin every holder of that role)
has no route in this blueprint at all -- `UserStore.create_role()`/
`list_roles()` are read/create-only from here, there is no
"delete a role" admin-UI action to guard. If a future task adds one, it
would need this same class of check (would deleting role X remove the
CURRENT session's own master_admin-equivalent access), but that is not a
gap this task leaves open, since the capability doesn't exist yet at all.

--------------------------------------------------------------------------
4. Per-user ad-hoc scopes: the "personal role" convention
--------------------------------------------------------------------------
The identity schema (`sql/migrations/0002_identity.sql`) is strictly
role-based: `role_scopes(role_id, client, permission)` belongs to a ROLE,
never directly to a user -- `UserStore` has no "grant this (client,
permission) scope to this one user" method, only `add_scope(role_name, ...)`.
The brief's form design ("role checkboxes from `list_roles()`" PLUS
"repeatable scope rows" on the SAME per-user form) needs a coherent mapping
onto that role-only model, which is not spelled out anywhere in
`UserStore`'s interface or the plan doc -- this is a genuine design decision
made by this task, documented here explicitly for review/Task 27:

Every user gets at most one **personal role**, named `f"user:{username}"`
(`PERSONAL_ROLE_PREFIX = "user:"`), created on demand the first time any
scope row is submitted for that user, holding EXACTLY the (client,
permission) pairs entered via the form's scope rows. It is granted to the
user via the normal `grant_role()` call, exactly like any other role. This
personal role is:

- **Hidden from the general role-checkbox list** (`_selectable_roles()`
  filters out anything starting with `PERSONAL_ROLE_PREFIX`) -- it must
  never appear as a generic pickable role for some OTHER user, and a
  submitted `roles` field naming one is rejected as unknown (both
  `create_user`/`update_user` validate submitted role names against
  `_selectable_roles()`'s own filtered set, below).
- **Reconciled, not just appended, on edit**: `PATCH`-like semantics --
  the submitted scope rows are diffed against the personal role's CURRENT
  `role_scopes` rows (fetched via `_role_scope_rows()`, a small read-only
  query keyed on the role's own composite primary key `(role_id, client,
  permission)`), and `UserStore.add_scope()`/`remove_scope()` are called
  for exactly the added/removed pairs. This is what makes UNCHECKING a
  permission in the edit form actually take effect, not just an
  additive-only form. If an edit removes every scope row, the now-empty
  personal role is `revoke_role()`d from the user too (not deleted --
  `UserStore` has no route-exposed role deletion here -- just ungranted),
  so it doesn't linger as a dangling, permission-less grant.
- **Never shown as a "role" in the admin UI's Roles column**
  (`list_users()` filters `principal.roles` the same way
  `_selectable_roles()` filters `list_roles()`) -- but its scopes are NOT
  hidden: `principal.scopes` is `UserStore`'s own cross-role aggregate, so
  a scope granted only via the personal role still appears in the Scopes
  column exactly like one granted via a named role. Only the synthetic
  role NAME is suppressed, never the grant.

`_role_scope_rows()` below reads this current-scopes state via
`UserStore.list_role_scopes(role_id)` -- the read-only counterpart to
`add_scope()`/`remove_scope()` that `UserStore` was missing at the time
this task originally shipped (back when the only option was the
aggregated cross-role `get_principal()` join, which can't be un-mixed back
into per-role contributions when a user holds more than one role, or a
raw SQL query issued directly against `role_scopes` here). That gap was
closed in the final whole-branch review: `UserStore` gained
`list_role_scopes()` (and `list_all_scope_clients()` for the client
datalist, section 6), and `_role_scope_rows()` was updated to call it
instead of touching `app.extensions['db_pool']` directly -- see that
function's own docstring below. It never writes anything; every actual
mutation still goes through `UserStore.add_scope()`/`remove_scope()`, so
`UserStore` remains the single authority on what scope is valid (invalid
permissions are still rejected by `add_scope`'s own `ALLOWED_PERMISSIONS`
check, mirrored here only as an early, cleaner 400 -- see
`_parse_scope_pairs()`).

--------------------------------------------------------------------------
5. Response shape for validation/guard-rail failures: 400, always
--------------------------------------------------------------------------
Two categories of POST failure exist in this blueprint:

- **Full-form routes** (`create_user`, `update_user`): re-render
  `admin/user_form.html` with the submitted values preserved and an
  `error` message, status 400. This lets the admin fix one field without
  re-typing everything -- the standard web-form UX -- and is directly
  usable by Task 27's real template (it already has to accept an `error`
  context var for this to work at all).
- **Single-action routes** (`disable_user`/`reset_password`'s not-found /
  guard-rail cases): a plain-text 400/404 response, no template. These
  routes carry no form fields besides the CSRF token, so there is nothing
  meaningful to "re-render" -- Task 27's UI is expected to trigger these
  from a button/small form on the list page, and can show the response
  text (or a fixed message) however it likes.

flash()+redirect (Flask's `session`-backed one-shot message queue) was
considered and rejected for BOTH categories: this blueprint has no
`layout.html` yet (Task 24) to render flashed messages inside, and 400 is
simpler to assert exactly in tests (a status code, not a redirect chain +
flashed-message scrape) for what this task's brief calls out as the
highest-priority thing to get right here. Consistent across every route in
this blueprint -- no route in this file ever redirects except on SUCCESS.

--------------------------------------------------------------------------
6. Clients registry (sql/migrations/0004_clients.sql)
--------------------------------------------------------------------------
`_active_client_names()`: registered, active client names, read straight
from `UserStore.list_clients(active_only=True)` -- feeds the Default
client / Scope client `<select>` options on the create/edit-user form.
Supersedes the former `_distinct_clients()` best-effort autocomplete
datalist (built from `alerts.client` UNIONed with
`UserStore.list_all_scope_clients()`'s `role_scopes.client` scan), which
existed only because there was no real client registry to query directly.
`create_user()`/`update_user()` validate every submitted scope-row client
and `default_client` against this same registered/active set (the `'*'`
wildcard sentinel stays exempt -- it is never a `clients` table row); an
unregistered client is rejected the same 400-and-re-render way any other
validation failure in this blueprint is (section 5 above). The
`/admin/clients` routes below (list/new/create/disable/enable) are the
admin UI for the registry itself -- same `admin_bp`, same
master_admin/CSRF/400-re-render conventions as every other route in this
file; no self-lockout-style guard rail, since a client has no "self"
concept the way a user disabling their own account does.

--------------------------------------------------------------------------
7. Templates: real, Task 27-built (`src/gateway/templates/admin/`)
--------------------------------------------------------------------------
Every handler below calls `render_template()` with the real final template
names/paths (`admin/users_list.html`, `admin/user_form.html`,
`admin/password_result.html`, `admin/clients_list.html`,
`admin/client_form.html`, under `src/gateway/templates/` -- Flask's
default template folder relative to this package's root_path, matching
`docs/OVERHAUL_PLAN.md` section 4.3's tree), extending the shared
`layout.html` (Task 24). The context variables each user-form view passes
(`users`, `mode`, `username`, `roles`, `selected_roles`, `scope_rows`,
`tokens`, `issue_token`, `token_name`, `default_client`, `clients`,
`allowed_permissions`, `error`, `password`, `token`) are the contract
Task 27's templates are built against; see
`tests/test_gateway_admin_templates.py` for the template-level test
coverage.

--------------------------------------------------------------------------
8. Bearer tokens: web-UI equivalent of tools/user-manager.py's create-token/
   list-tokens/revoke-token
--------------------------------------------------------------------------
`UserStore.create_token()`/`list_tokens()`/`revoke_token()` predate this
addition (CLI-only until now). Tokens issued through this blueprint always
pass `expires_at=None` (never expire) -- no expiry-picker UI, matching the
CLI's own default when `--expires-at` is omitted. The New User form can
issue one token at account-creation time (`issue_token`/`token_name` form
fields on `create_user()`, alongside a purely client-side "Service
account" checkbox that just nudges `must_change_password`/`issue_token`'s
defaults via JS -- nothing server-side reads `service_account` at all).
The edit page additionally lists every token for that account and can
issue more or revoke existing ones. `revoke_token()`'s route verifies the
target token_id actually belongs to the account named in the URL (via
`list_tokens(user_id=...)`) before revoking it, the same "never trust a
path parameter alone" discipline as the disable/edit guard rails in
section 3 above, even though there is no self-lockout risk here (tokens
never gate a browser session's own access).
"""

import logging
import re
import secrets
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple

from flask import Blueprint, Response, abort, current_app, g, make_response, redirect, render_template, request, url_for

from gateway.principal import require_master_admin
from userstore import ALLOWED_PERMISSIONS, MASTER_ADMIN_ROLE, ClientNotFoundError, UserNotFoundError, UserStore

logger = logging.getLogger("gateway.admin")

admin_bp = Blueprint("admin", __name__)

# Create-only, per the brief verbatim: 3-32 chars, lowercase letters/digits/-/_.
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")

# Same identifier convention as USERNAME_RE, applied to registered client
# names -- a client name is now first-class registry data (not free text)
# and becomes a URL path segment (/admin/clients/<name>/disable).
CLIENT_NAME_RE = re.compile(r"^[a-z0-9_-]{1,255}$")

# Per the brief verbatim -- deliberately NOT tools/user-manager.py's own
# GENERATED_PASSWORD_BYTES (18); this task's brief specifies 12 explicitly.
GENERATED_PASSWORD_BYTES = 12

# See module docstring section 4. Distinguishes an auto-managed per-user
# scope-holder role from any real, general-purpose role an operator named
# via the CLI (`master_admin`, `<client>-node`, `<client>-viewer`, ...) --
# none of which would ever collide with this prefix in practice, but the
# check is explicit (`_selectable_roles()` filters it out; the `roles`
# form field is validated against that same filtered set) rather than
# relied upon by convention alone.
PERSONAL_ROLE_PREFIX = "user:"

# v1 default for a brand-new scope row -- matches the brief exactly. Passed
# to the template as a hint for what a freshly-added blank row should
# pre-check; not enforced server-side (a submitted row may carry any subset
# of ALLOWED_PERMISSIONS, or none).
DEFAULT_SCOPE_PERMISSIONS = frozenset({"logs:read"})

# Fallback token name when the submitted `token_name` field is blank --
# not a validated field beyond auth_tokens.name's own VARCHAR(255) NOT NULL,
# so an empty submission silently falls back rather than rejecting the form.
DEFAULT_TOKEN_NAME = "default"


# ---------------------------------------------------------------------------
# Small accessors
# ---------------------------------------------------------------------------

def _user_store() -> UserStore:
    return current_app.extensions["user_store"]


def _actor() -> str:
    return g.principal.username


def _source_ip() -> Optional[str]:
    return request.remote_addr


def _personal_role_name(username: str) -> str:
    return f"{PERSONAL_ROLE_PREFIX}{username}"


def _normalize_username(username: str) -> str:
    """Same normalization UserStore itself applies internally -- done here too so guard-rail/personal-role-name logic in this file always operates on the canonical form, not whatever case the URL happened to carry."""
    return username.strip().lower()


def _normalize_client(client: str) -> str:
    """Same strip+lower transform as _normalize_username, named separately for readability at call sites that are conceptually about a client string, not a username (CLAUDE.md "String Normalization" applies identically to both)."""
    return client.strip().lower()


def _token_name(form) -> str:
    """Submitted `token_name`, trimmed, or DEFAULT_TOKEN_NAME if blank/omitted."""
    return form.get("token_name", "").strip() or DEFAULT_TOKEN_NAME


# ---------------------------------------------------------------------------
# Read-only DB helpers -- see module docstring sections 4 and 6.
# ---------------------------------------------------------------------------

def _active_client_names() -> List[str]:
    """
    Registered, active client names -- feeds the Default client / Scope
    client <select> options on the create/edit-user form. Supersedes the
    former `_distinct_clients()` best-effort autocomplete datalist (built
    from `alerts.client` UNIONed with `role_scopes.client`) now that
    `clients` (sql/migrations/0004_clients.sql) is a real registry to
    query directly.
    """
    return [c["name"] for c in _user_store().list_clients(active_only=True)]


def _role_scope_rows(role_id: int) -> List[Dict[str, str]]:
    """
    Every (client, permission) row `role_scopes` holds for one role_id --
    via `UserStore.list_role_scopes()`, `UserStore` remaining the single
    owner of every role_scopes read/write per its own module charter (this
    used to be a raw SQL query issued directly against
    `app.extensions['db_pool']` here, a charter leak flagged in the final
    whole-branch review and fixed by adding the read method `UserStore`
    was missing). Used only to reconcile the personal per-user role's scope
    set against a submitted edit form (see module docstring section 4).
    Unlike `_distinct_clients()`, this feeds a WRITE-reconciliation
    decision, so a DB failure here is NOT caught-and-degraded -- it
    propagates (CLAUDE.md "do not implement failsafes"), the same as any
    other `UserStore` call in this blueprint.
    """
    return _user_store().list_role_scopes(role_id)


# ---------------------------------------------------------------------------
# Form parsing -- see module docstring section 4 for the field-name contract
# Task 27's template/JS must match.
# ---------------------------------------------------------------------------

def _parse_scope_pairs(form) -> List[Tuple[str, str]]:
    """
    Parse repeatable scope rows into a flat list of (client, permission)
    pairs, one pair per checked permission checkbox per row.

    Field-name contract (documented here since Task 27 builds the actual
    template/JS against it): `scope_client` is a repeated field, one value
    per row IN ORDER (`request.form.getlist("scope_client")`); each row's
    checked permissions are `scope_permissions_<row_index>` (0-based, same
    order), a repeated checkbox field. A row whose client is blank/
    whitespace-only is skipped entirely regardless of which permission
    boxes (if any) are checked for it -- trivial to end up with a trailing
    blank row from the "add scope row" JS that was never filled in, and an
    empty client is never a valid scope.
    """
    pairs: List[Tuple[str, str]] = []
    for index, raw_client in enumerate(form.getlist("scope_client")):
        client = raw_client.strip().lower()
        if not client:
            continue
        for permission in form.getlist(f"scope_permissions_{index}"):
            pairs.append((client, permission))
    return pairs


def _group_scope_pairs(pairs: Iterable[Tuple[str, str]]) -> List[Tuple[str, List[str]]]:
    """Flat (client, permission) pairs -> [(client, [permissions...]), ...], grouped in first-seen order, for redisplaying a form (GET edit, or a 400 re-render)."""
    grouped: "OrderedDict[str, List[str]]" = OrderedDict()
    for client, permission in pairs:
        bucket = grouped.setdefault(client, [])
        if permission not in bucket:
            bucket.append(permission)
    return list(grouped.items())


def _selectable_roles() -> List[Dict[str, Any]]:
    """Every role except personal per-user scope-holder roles -- see module docstring section 4. These are never a general-purpose checkbox."""
    return [r for r in _user_store().list_roles() if not r["name"].startswith(PERSONAL_ROLE_PREFIX)]


# ---------------------------------------------------------------------------
# Shared form rendering (GET new/edit, and 400 re-renders on validation/
# guard-rail failure)
# ---------------------------------------------------------------------------

def _render_user_form(
    *,
    mode: str,
    username: Optional[str] = None,
    selected_roles: Optional[Iterable[str]] = None,
    scope_rows: Optional[List[Tuple[str, List[str]]]] = None,
    tokens: Optional[List[Dict[str, Any]]] = None,
    issue_token: bool = False,
    token_name: str = DEFAULT_TOKEN_NAME,
    default_client: Optional[str] = None,
    error: Optional[str] = None,
    status: int = 200,
):
    context = {
        "mode": mode,
        "username": username,
        "roles": _selectable_roles(),
        "selected_roles": set(selected_roles or ()),
        "scope_rows": scope_rows or [],
        "tokens": tokens or [],
        "issue_token": issue_token,
        "token_name": token_name,
        "default_client": default_client,
        "clients": _active_client_names(),
        "allowed_permissions": sorted(ALLOWED_PERMISSIONS),
        "default_scope_permissions": DEFAULT_SCOPE_PERMISSIONS,
        "master_admin_role": MASTER_ADMIN_ROLE,
        "error": error,
    }
    return render_template("admin/user_form.html", **context), status


# ---------------------------------------------------------------------------
# GET /admin/users -- list
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users", methods=["GET"])
@require_master_admin
def list_users():
    """
    `roles` deliberately excludes personal per-user scope-holder roles
    (`PERSONAL_ROLE_PREFIX`, module docstring section 4) -- same filter
    `_selectable_roles()` applies to the checkbox list, applied here too so
    an admin looking at this table can't mistake a user's ad-hoc scope
    bucket (`user:bob`) for a real, admin-assignable role (`master_admin`,
    `acme-viewer`, ...). This does NOT hide the underlying scopes: `scopes`
    is `principal.scopes`, `UserStore`'s own aggregated join across EVERY
    role the user holds (including the personal one) -- a scope granted
    only via the personal role still shows up in the Scopes column exactly
    as it would if it came from a named role; only the synthetic role
    NAME is suppressed from the Roles column, never the grant itself.
    """
    store = _user_store()
    rows = []
    for user in store.list_users():
        principal = store.get_principal(user["username"])
        visible_roles = (
            sorted(r for r in principal.roles if not r.startswith(PERSONAL_ROLE_PREFIX))
            if principal is not None
            else []
        )
        rows.append(
            {
                **user,
                "roles": visible_roles,
                "scopes": principal.scopes if principal is not None else {},
                "is_master_admin": principal.is_master_admin() if principal is not None else False,
            }
        )
    # master_admin_role: small context addition for Task 27's real template
    # (list_users() previously passed only users/current_user_id) -- lets
    # users_list.html give the master_admin ROLE TAG the same red warning
    # treatment user_form.html's checkbox already gets, without the
    # template hardcoding the "master_admin" string a second time.
    return render_template(
        "admin/users_list.html",
        users=rows,
        current_user_id=g.principal.user_id,
        master_admin_role=MASTER_ADMIN_ROLE,
    )


# ---------------------------------------------------------------------------
# GET/POST /admin/users/new -- create
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/new", methods=["GET"])
@require_master_admin
def new_user_form():
    return _render_user_form(mode="new", scope_rows=[("", [])])


@admin_bp.route("/admin/users/new", methods=["POST"])
@require_master_admin
def create_user():
    store = _user_store()
    username_raw = request.form.get("username", "")
    username = _normalize_username(username_raw)

    submitted_roles = [r for r in request.form.getlist("roles") if r]
    scope_pairs = _parse_scope_pairs(request.form)
    issue_token = request.form.get("issue_token") is not None
    token_name = _token_name(request.form)
    default_client = _normalize_client(request.form.get("default_client", "")) or None

    def _reject(message: str):
        return _render_user_form(
            mode="new",
            username=username_raw,
            selected_roles=submitted_roles,
            scope_rows=_group_scope_pairs(scope_pairs),
            issue_token=issue_token,
            token_name=token_name,
            default_client=default_client,
            error=message,
            status=400,
        )

    if not USERNAME_RE.match(username):
        return _reject(f"Username must match {USERNAME_RE.pattern} (3-32 chars: lowercase letters, digits, '-', '_').")

    if store.get_user(username) is not None:
        return _reject(f"Username {username!r} is already taken.")

    known_role_names = {r["name"] for r in _selectable_roles()}
    for role_name in submitted_roles:
        if role_name not in known_role_names:
            return _reject(f"Unknown role {role_name!r}.")

    for _client, permission in scope_pairs:
        if permission not in ALLOWED_PERMISSIONS:
            return _reject(f"Invalid permission {permission!r}.")

    # Clients are a real registry now (sql/migrations/0004_clients.sql), not
    # freeform text -- every scope row's client (except the `'*'` wildcard
    # sentinel, which is never a table row) and default_client must already
    # be registered and active.
    known_client_names = {c["name"] for c in _user_store().list_clients(active_only=True)}
    for client, _permission in scope_pairs:
        if client != "*" and client not in known_client_names:
            return _reject(f"Unknown client {client!r}. Register it on the Clients page first.")
    if default_client and default_client not in known_client_names:
        return _reject(f"Unknown client {default_client!r}. Register it on the Clients page first.")

    password = secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    actor, source_ip = _actor(), _source_ip()

    # v3.0.0: `must_change_password` is now actually enforced (a
    # `before_request` gate in gateway/__init__.py redirects browser sessions
    # to /change-password until it clears), so it is no longer free to set it
    # unconditionally. Machine accounts authenticate with Basic/Bearer and are
    # never gated, but flagging them would still be a lie in the admin list and
    # in GET /api/whoami -- so the operator chooses. Checked by default: the
    # common case is creating an account for a human, and the generated
    # password below is one the admin has necessarily seen.
    must_change_password = request.form.get("must_change_password") is not None

    user_id = store.create_user(
        username,
        password,
        default_client=default_client,
        must_change_password=must_change_password,
        status="active",
        actor=actor,
        source_ip=source_ip,
    )

    for role_name in submitted_roles:
        store.grant_role(username, role_name, actor=actor, source_ip=source_ip)

    if scope_pairs:
        personal_role = _personal_role_name(username)
        store.create_role(personal_role, description=f"Per-user scopes for {username}", actor=actor, source_ip=source_ip)
        for client, permission in scope_pairs:
            store.add_scope(personal_role, client, permission, actor=actor, source_ip=source_ip)
        store.grant_role(username, personal_role, actor=actor, source_ip=source_ip)

    token = store.create_token(token_name, user_id, actor=actor, source_ip=source_ip) if issue_token else None

    resp = make_response(render_template(
        "admin/password_result.html",
        username=username,
        password=password,
        token=token,
        token_name=token_name if token else None,
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# GET/POST /admin/users/<username>/edit -- edit (username NOT editable)
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/edit", methods=["GET"])
@require_master_admin
def edit_user_form(username: str):
    store = _user_store()
    username_norm = _normalize_username(username)
    principal = store.get_principal(username_norm)
    if principal is None:
        abort(404)

    selected_roles = [r for r in principal.roles if not r.startswith(PERSONAL_ROLE_PREFIX)]

    personal_role = store.get_role(_personal_role_name(username_norm))
    existing_pairs = (
        [(row["client"], row["permission"]) for row in _role_scope_rows(personal_role["id"])]
        if personal_role is not None
        else []
    )

    return _render_user_form(
        mode="edit",
        username=username_norm,
        selected_roles=selected_roles,
        scope_rows=_group_scope_pairs(existing_pairs) or [("", [])],
        tokens=store.list_tokens(user_id=principal.user_id),
    )


@admin_bp.route("/admin/users/<username>/edit", methods=["POST"])
@require_master_admin
def update_user(username: str):
    store = _user_store()
    username_norm = _normalize_username(username)

    # Fresh, authoritative fetch of the TARGET's current identity/roles --
    # never inferred from g.principal, never from a scope/permission check.
    # See module docstring section 3.
    current = store.get_principal(username_norm)
    if current is None:
        abort(404)

    submitted_roles = {r for r in request.form.getlist("roles") if r}
    scope_pairs = _parse_scope_pairs(request.form)

    def _reject(message: str):
        return _render_user_form(
            mode="edit",
            username=username_norm,
            selected_roles=submitted_roles,
            scope_rows=_group_scope_pairs(scope_pairs) or [("", [])],
            tokens=store.list_tokens(user_id=current.user_id),
            error=message,
            status=400,
        )

    # --- THE self-lockout guard rail (module docstring section 3) ---
    is_self = current.user_id == g.principal.user_id
    currently_master_admin = MASTER_ADMIN_ROLE in current.roles
    desired_master_admin = MASTER_ADMIN_ROLE in submitted_roles
    if is_self and currently_master_admin and not desired_master_admin:
        return _reject("You cannot remove the master_admin role from your own account.")

    known_role_names = {r["name"] for r in _selectable_roles()}
    for role_name in submitted_roles:
        if role_name not in known_role_names:
            return _reject(f"Unknown role {role_name!r}.")

    for _client, permission in scope_pairs:
        if permission not in ALLOWED_PERMISSIONS:
            return _reject(f"Invalid permission {permission!r}.")

    # See create_user()'s identical check: clients are a real registry now,
    # not freeform text. `'*'` stays exempt (never a table row).
    known_client_names = {c["name"] for c in _user_store().list_clients(active_only=True)}
    for client, _permission in scope_pairs:
        if client != "*" and client not in known_client_names:
            return _reject(f"Unknown client {client!r}. Register it on the Clients page first.")

    actor, source_ip = _actor(), _source_ip()

    # -- reconcile role_scopes-derived roles (excluding the personal role, which is managed separately below) --
    current_roles = {r for r in current.roles if not r.startswith(PERSONAL_ROLE_PREFIX)}
    for role_name in sorted(submitted_roles - current_roles):
        store.grant_role(username_norm, role_name, actor=actor, source_ip=source_ip)
    for role_name in sorted(current_roles - submitted_roles):
        store.revoke_role(username_norm, role_name, actor=actor, source_ip=source_ip)

    # -- reconcile the personal per-user scope role -- see module docstring section 4 --
    personal_role_name = _personal_role_name(username_norm)
    personal_role = store.get_role(personal_role_name)
    existing_pairs = (
        {(row["client"], row["permission"]) for row in _role_scope_rows(personal_role["id"])}
        if personal_role is not None
        else set()
    )
    desired_pairs = set(scope_pairs)
    to_add = desired_pairs - existing_pairs
    to_remove = existing_pairs - desired_pairs

    if to_add and personal_role is None:
        store.create_role(
            personal_role_name, description=f"Per-user scopes for {username_norm}", actor=actor, source_ip=source_ip
        )
        store.grant_role(username_norm, personal_role_name, actor=actor, source_ip=source_ip)

    for client, permission in sorted(to_add):
        store.add_scope(personal_role_name, client, permission, actor=actor, source_ip=source_ip)
    for client, permission in sorted(to_remove):
        store.remove_scope(personal_role_name, client, permission, actor=actor, source_ip=source_ip)

    # If the edit emptied out every scope row, the personal role would
    # otherwise stay granted-but-empty forever (UserStore has no
    # "delete role" call site here, and there'd be nothing left for it to
    # hold). Revoking the now-pointless grant is cheap cleanup, not a
    # correctness fix -- list_users()'s roles column already filters
    # PERSONAL_ROLE_PREFIX names regardless, so a lingering empty grant was
    # invisible either way, but there's no reason to leave the dangling
    # user_roles row around once it grants nothing.
    if not desired_pairs and personal_role is not None:
        store.revoke_role(username_norm, personal_role_name, actor=actor, source_ip=source_ip)

    return redirect(url_for("admin.list_users"))


# ---------------------------------------------------------------------------
# POST /admin/users/<username>/disable -- the OTHER self-lockout guard rail
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/disable", methods=["POST"])
@require_master_admin
def disable_user(username: str):
    store = _user_store()
    username_norm = _normalize_username(username)

    target = store.get_user(username_norm)
    if target is None:
        abort(404)

    # THE self-disable guard rail: numeric id comparison against the
    # session's own g.principal.user_id (populated from a live
    # UserStore.get_principal() call by _resolve_session). See module
    # docstring section 3.
    if target["id"] == g.principal.user_id:
        return Response("You cannot disable your own account.\n", status=400, mimetype="text/plain")

    store.disable_user(username_norm, actor=_actor(), source_ip=_source_ip())
    return redirect(url_for("admin.list_users"))


# ---------------------------------------------------------------------------
# POST /admin/users/<username>/enable -- no guard rail (never a lockout risk)
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/enable", methods=["POST"])
@require_master_admin
def enable_user(username: str):
    store = _user_store()
    username_norm = _normalize_username(username)
    try:
        store.enable_user(username_norm, actor=_actor(), source_ip=_source_ip())
    except UserNotFoundError:
        abort(404)
    return redirect(url_for("admin.list_users"))


# ---------------------------------------------------------------------------
# POST /admin/users/<username>/reset-password -- always server-generated
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/reset-password", methods=["POST"])
@require_master_admin
def reset_password(username: str):
    """
    No guard rail: resetting YOUR OWN password is not a lockout risk (the
    new password is shown to you immediately, in the same response, by the
    same already-authenticated session) -- only disabling/de-admin-ing
    your own account are guarded, exactly per the brief.
    """
    store = _user_store()
    username_norm = _normalize_username(username)
    if store.get_user(username_norm) is None:
        abort(404)

    password = secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)
    store.set_password(
        username_norm, password, must_change_password=True, actor=_actor(), source_ip=_source_ip()
    )

    resp = make_response(render_template(
        "admin/password_result.html", username=username_norm, password=password, token=None, token_name=None,
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# POST /admin/users/<username>/tokens/new -- issue a Bearer token for an
# existing account (create_user() above can also issue one AT creation
# time; this is the equivalent for an account that already exists). Always
# non-expiring (expires_at=None) -- no expiry picker in the web UI.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/tokens/new", methods=["POST"])
@require_master_admin
def issue_token(username: str):
    store = _user_store()
    username_norm = _normalize_username(username)
    target = store.get_user(username_norm)
    if target is None:
        abort(404)

    token_name = _token_name(request.form)
    token = store.create_token(token_name, target["id"], actor=_actor(), source_ip=_source_ip())

    resp = make_response(render_template(
        "admin/password_result.html",
        username=username_norm,
        password=None,
        token=token,
        token_name=token_name,
    ))
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# POST /admin/users/<username>/tokens/<token_id>/revoke -- ownership is
# verified via list_tokens(user_id=...) BEFORE calling revoke_token(), so
# an admin can never revoke a token belonging to a different account by
# editing the token_id in this account's URL.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/users/<username>/tokens/<int:token_id>/revoke", methods=["POST"])
@require_master_admin
def revoke_token(username: str, token_id: int):
    store = _user_store()
    username_norm = _normalize_username(username)
    target = store.get_user(username_norm)
    if target is None:
        abort(404)

    owned_ids = {t["id"] for t in store.list_tokens(user_id=target["id"])}
    if token_id not in owned_ids:
        abort(404)

    store.revoke_token(token_id, actor=_actor(), source_ip=_source_ip())
    return redirect(url_for("admin.edit_user_form", username=username_norm))


# ---------------------------------------------------------------------------
# GET /admin/clients -- list; GET/POST /admin/clients/new -- create;
# POST .../disable, POST .../enable -- soft-disable/re-enable
#
# The clients registry (sql/migrations/0004_clients.sql): promotes "client"
# from pure free text into a real, admin-managed list. No delete route --
# same "no hard delete for a named entity" convention as users/roles;
# disable/enable is the full lifecycle. No self-lockout-style guard rail
# here -- there is no "self" concept for a client, unlike a user disabling
# their own account or removing their own master_admin role.
# ---------------------------------------------------------------------------

@admin_bp.route("/admin/clients", methods=["GET"])
@require_master_admin
def list_clients():
    return render_template("admin/clients_list.html", clients=_user_store().list_clients())


@admin_bp.route("/admin/clients/new", methods=["GET"])
@require_master_admin
def new_client_form():
    return render_template("admin/client_form.html", name=None, description=None, error=None)


@admin_bp.route("/admin/clients/new", methods=["POST"])
@require_master_admin
def create_client():
    store = _user_store()
    name_raw = request.form.get("name", "")
    name = _normalize_client(name_raw)
    description = request.form.get("description", "").strip() or None

    def _reject(message: str):
        return render_template(
            "admin/client_form.html", name=name_raw, description=description, error=message
        ), 400

    if not CLIENT_NAME_RE.match(name):
        return _reject(
            f"Client name must match {CLIENT_NAME_RE.pattern} (1-255 chars: lowercase letters, digits, '-', '_')."
        )
    if name == "*":
        return _reject("'*' is reserved as the wildcard scope sentinel and cannot be a registered client name.")
    if store.get_client(name) is not None:
        return _reject(f"Client {name!r} is already registered.")

    store.create_client(name, description, actor=_actor(), source_ip=_source_ip())
    return redirect(url_for("admin.list_clients"))


@admin_bp.route("/admin/clients/<name>/disable", methods=["POST"])
@require_master_admin
def disable_client(name: str):
    try:
        _user_store().disable_client(_normalize_client(name), actor=_actor(), source_ip=_source_ip())
    except ClientNotFoundError:
        abort(404)
    return redirect(url_for("admin.list_clients"))


@admin_bp.route("/admin/clients/<name>/enable", methods=["POST"])
@require_master_admin
def enable_client(name: str):
    try:
        _user_store().enable_client(_normalize_client(name), actor=_actor(), source_ip=_source_ip())
    except ClientNotFoundError:
        abort(404)
    return redirect(url_for("admin.list_clients"))
