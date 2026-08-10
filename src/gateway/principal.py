"""
Alert Manager - gateway.principal

The authorization seam every later Phase 4 blueprint task (19-23) builds
on: `Principal` (wraps Task 15's `PrincipalData` with request-scoped
metadata), `resolve_principal()` (inspects the current Flask request and
returns a `Principal` or `None`), and three decorators --
`require_auth`, `require_permission`, `require_master_admin`.

This module intentionally does NOT implement session-cookie mechanics
(Flask's hand-rolled signed-cookie session with `{uid, u, iat}` payload,
docs/OVERHAUL_PLAN.md section 4.4) or the CredentialCache -- both are
Task 19's job. What it DOES do is design the *interface* Task 19 plugs
into, and implement Basic/Bearer resolution now (directly against
`UserStore.verify_login()`/`verify_token()`) since those need no session
machinery at all and are needed by every later blueprint regardless of
whether Task 19 has landed.

--------------------------------------------------------------------------
Per-blueprint auth policy (who is allowed to authenticate how)
--------------------------------------------------------------------------
Three method tuples cover every blueprint described in the plan:

    API_ONLY     = ("basic", "bearer")             -- api blueprint (Task 20)
    SESSION_ONLY = ("session",)                     -- dashboard (22), admin (23)
    SESSION_OR_API (== DEFAULT_AUTH_METHODS)        -- logs blueprint (Task 21)
                 = ("session", "basic", "bearer")

`api` must never accept a session cookie at all (so its CSRF exemption is
safe -- no cookie means nothing to forge, per plan 4.4); `logs` accepts
either, to keep existing curl/shell-test workflows (`tools/tests/*.sh`)
working alongside the new browser UI; `dashboard`/`admin` are
session-only, browser-only surfaces.

--------------------------------------------------------------------------
resolve_principal(): precedence and how Task 19 extends it
--------------------------------------------------------------------------
`resolve_principal(methods)` tries each method in `methods`, IN ORDER, and
returns the first `Principal` produced. The default/recommended order is
session -> basic -> bearer:

- A session cookie represents an already-established, explicitly-initiated
  browser login; Basic/Bearer are per-request credentials typically
  presented by scripts/devices. If a request somehow carries both a valid
  session cookie AND a Basic/Bearer header (unusual, but possible -- e.g.
  a browser tab with a stale session cookie also sending an Authorization
  header from a saved-credentials extension), the session is treated as
  the authoritative "who is logically using the browser right now"
  identity.
- This ordering is moot for `api` (session is never in its `methods`
  tuple) and for `dashboard`/`admin` (basic/bearer are never in theirs);
  it only actually matters for `logs`, where both are allowed.

Each method dispatches to its own top-level resolver function
(`_resolve_session` / `_resolve_basic` / `_resolve_bearer`), looked up
dynamically by name (`_resolve_<method>`) on every call rather than bound
once at import time -- this is what lets Task 19 (or a test) replace
`gateway.principal._resolve_session` in place (monkeypatch/`mocker.patch`)
and have `resolve_principal` immediately pick up the replacement, with no
other change needed anywhere in this module:

- `_resolve_session` (Task 19) now verifies Flask's signed-cookie session
  (`{uid, u, iat}`, set by `gateway/auth/routes.py`'s `POST /login`) and
  re-checks the account's live `status` on every call -- see that
  function's own docstring for the full mechanism (cookie signature/expiry
  is Flask/itsdangerous's job; the 12h sliding lifetime comes from Flask's
  built-in `SESSION_REFRESH_EACH_REQUEST`). Only this function's *body*
  changed; `resolve_principal`'s dispatch/precedence logic and both
  decorators built on top of it are untouched, exactly as designed.
- `_resolve_basic` / `_resolve_bearer` call `UserStore.verify_login()` /
  `verify_token()` directly (a real, DB-backed implementation, not a stub)
  -- this sits on the eventual hot path Task 19's `CredentialCache`
  (`gateway/auth/cache.py`) is designed to front (docs/OVERHAUL_PLAN.md
  section 4.4/`userstore/store.py`'s own module docstring: "the one path
  that *could* be hot ... is designed to sit behind a gateway-side
  CredentialCache"). Task 19 built `CredentialCache` itself and documented
  exactly how to wrap these two functions with it, but deliberately left
  the actual wiring to Task 20's `api` blueprint/`POST /alert` route (see
  `gateway/auth/cache.py`'s "Integration note for Task 20") -- these two
  functions are unchanged by Task 19.

--------------------------------------------------------------------------
Anonymous-request failure behavior
--------------------------------------------------------------------------
`require_auth`/`require_permission`/`require_master_admin` all fail the
same way, via `_auth_failure_response`:

- If the request carries an `Authorization` header (Basic or Bearer) that
  failed to resolve, the response is always a 401 with a
  `WWW-Authenticate: Basic` challenge -- regardless of whether "session"
  is also an allowed method for this route. This is what keeps
  `tools/tests/*.sh` (curl with `-u user:pass` against `/logs`,
  `/api/logs/*`) working exactly as before: a script that deliberately
  sent credentials and got them wrong should see a 401, never a 302 to an
  HTML login page it can't do anything with.
- Otherwise, if "session" is an allowed method (i.e. this is a
  browser-facing route, and there was no credential to reject -- an
  anonymous GET with no session cookie and no Authorization header) AND
  the request is not JSON/API-style (see `_prefers_json()` below), the
  response is a 302 redirect to `/login?next=<current request path>`.
- Otherwise (session not allowed, e.g. the `api` blueprint; OR no
  Authorization header was presented but the request IS JSON/API-style;
  OR an Authorization header was presented and failed to resolve) the
  response is the same 401 challenge.

The redirect target is resolved via `url_for("auth.login", next=...)` when
that endpoint exists (Task 19 registers a blueprint named `auth` with a
`login` view), falling back to a hard-coded `/login?next=...` path
otherwise -- so this module works standalone today, before Task 19's
blueprint is registered, and automatically picks up the real endpoint
once it is.

--------------------------------------------------------------------------
`_prefers_json()`: closing the redirect-vs-401 gap for mixed-auth
blueprints (Task 21)
--------------------------------------------------------------------------
Task 19's review flagged a gap specific to a blueprint that mixes
"session" with "basic"/"bearer" (only `logs`, via `SESSION_OR_API`, as of
Task 21): a genuinely anonymous JSON request -- no `-u`, no bearer token,
e.g. a bare `curl http://host/api/logs/list` -- would get an HTML 302 to
`/login` instead of a clean 401, purely because "session" happened to
also be an allowed method for that route. (Requests presenting *wrong*
Basic/Bearer credentials were never affected -- see the first bullet
above -- so no existing `tools/tests/*.sh` workflow, which always passes
`-u`, was ever broken by this; only the fully-credential-less case was
wrong.)

`_prefers_json()` is a cheap, centrally-applied content-negotiation
signal: any request path starting with `/api/` is treated as JSON/API-
style and never gets the redirect treatment, regardless of which auth
methods the route allows. This matches the codebase's existing URL
convention (`/api/whoami`, `/api/logs/list`, `/api/logs/tail`,
`/api/logs/chunk` are all spelled this way) and needs no per-route opt-in
-- landing the fix here, once, means EVERY current and future `/api/*`
route under a session-allowing policy is automatically correct, rather
than each one having to bypass `require_auth` and hand-roll its own
`resolve_principal()` call the way Task 19's `/api/whoami` originally did
(see that route's own docstring in gateway/auth/routes.py for why, on
reflection, its bypass is being LEFT AS IS rather than simplified: it
predates this fix for an unrelated reason -- JSON-shaped error bodies --
that this fix does not address).

An explicit `prefers_json: bool` kwarg on `require_auth`/
`require_permission` was considered as the alternative mechanism (the
brief sketches both); the path-based check was chosen because it requires
zero changes at any of this task's three new call sites (or `/api/whoami`,
had it used `require_auth`) and cannot be forgotten at a future one.

--------------------------------------------------------------------------
require_permission(): route-based vs. body-based client resolution
--------------------------------------------------------------------------
`require_permission(permission, get_client=None, methods=...)` needs a
`client` string to call `principal.has_permission(permission, client)`
with, and the plan describes two shapes for where that client comes from:

1. Route/query-based, e.g. `GET /api/logs/tail?file=file/{client}/...`
   (Task 21 parses the `file` query param itself to pull the client
   segment out -- that parsing is `logs/files.py`'s own concern, not
   this module's).
2. Body-based, e.g. `POST /alert`'s JSON payload: effective client =
   payload `client` else `principal.default_client` else `''`.

Both shapes are just different zero-argument callables, so `get_client`
is exactly that: an optional `Callable[[], str]` evaluated *inside the
request context* (it may read `request.view_args`, `request.args`,
`request.get_json()`, `g.principal`, anything). `require_permission`
normalizes whatever it returns (`.strip().lower()`, matching this
project's established client-string-normalization convention -- see
CLAUDE.md "String Normalization" and `PrincipalData.has_permission`'s own
"callers must pass an already-lowercased value" contract) so call sites
never have to repeat that themselves:

    # route/query-based (Task 21):
    require_permission("logs:read", get_client=lambda: extract_client_from_path(request.args["file"]))

    # body-based (Task 20, POST /alert):
    require_permission(
        "alerts:write",
        get_client=lambda: (request.get_json(silent=True) or {}).get("client") or g.principal.default_client or "",
    )

If `get_client` is omitted entirely, the effective client is `""` (the
clientless/global scope) -- a reasonable default for a permission that
isn't meaningfully client-scoped in practice.

Note on the route/query-based example above, as actually built in Task 21:
`gateway/logs/files.py`'s `is_log_file_authorized()` ended up NOT going
through `require_permission` at all, even though its `file/{client}/**`
tier is exactly one `has_permission('logs:read', client)` check. Two of
its three other tiers (`ops/**`, flat files) need `is_master_admin()`
specifically, not a `has_permission` call against some synthetic
placeholder "client" value -- doable in principle (master_admin's
`('*', '*')` scope matches any client argument), but that would make
correctness depend on no other role ever accidentally being granted a
scope for a client literally named e.g. `"ops"`, which is a subtler
invariant than just calling `is_master_admin()` outright. It also needs
to filter a whole directory LISTING (`/api/logs/list`) per-file, not
gate one single route -- a shape `require_permission` (built for exactly
one permission/client pair per request) doesn't fit. So `logs`'s three
routes all use plain `require_auth` and call
`files.is_log_file_authorized()` explicitly in the view body instead --
see `gateway/logs/files.py`'s and `gateway/logs/routes.py`'s own module
docstrings.
"""

from functools import wraps
from typing import Any, Callable, Optional, Sequence, Tuple

from flask import Response, abort, current_app, g, redirect, request, session, url_for

from userstore import PrincipalData, UserStore

# ---------------------------------------------------------------------------
# Method-tuple constants -- see module docstring's "per-blueprint auth
# policy" section. Blueprint authors should use these names rather than
# writing out the literal tuples, so the mapping from blueprint -> policy
# stays visible at every call site.
# ---------------------------------------------------------------------------

API_ONLY: Tuple[str, ...] = ("basic", "bearer")
SESSION_ONLY: Tuple[str, ...] = ("session",)
SESSION_OR_API: Tuple[str, ...] = ("session", "basic", "bearer")
DEFAULT_AUTH_METHODS: Tuple[str, ...] = SESSION_OR_API


# ---------------------------------------------------------------------------
# Principal
# ---------------------------------------------------------------------------

class Principal:
    """
    Wraps one request's authenticated `PrincipalData` (Task 15) with
    request-scoped metadata: which credential resolved it. `auth_method`
    is exposed for callers that need to know *how* a request
    authenticated -- e.g. Task 19's CSRF setup only needs to protect
    session-authenticated requests (Basic/Bearer requests carry no cookie,
    so nothing to forge), and this is the natural place to check that.

    All `PrincipalData` read-only accessors are forwarded directly so
    callers can write `g.principal.has_permission(...)` /
    `g.principal.is_master_admin()` / `g.principal.default_client` without
    reaching through a `.data` attribute.
    """

    __slots__ = ("data", "auth_method")

    def __init__(self, data: PrincipalData, auth_method: str):
        self.data = data
        self.auth_method = auth_method

    def __repr__(self) -> str:
        return f"Principal(username={self.data.username!r}, auth_method={self.auth_method!r})"

    # -- forwarded PrincipalData accessors -----------------------------------

    @property
    def user_id(self) -> int:
        return self.data.user_id

    @property
    def username(self) -> str:
        return self.data.username

    @property
    def status(self) -> str:
        return self.data.status

    @property
    def default_client(self) -> Optional[str]:
        return self.data.default_client

    @property
    def must_change_password(self) -> bool:
        return self.data.must_change_password

    def is_master_admin(self) -> bool:
        return self.data.is_master_admin()

    def has_permission(self, permission: str, client: str) -> bool:
        return self.data.has_permission(permission, client)

    def visible_clients(self, permission: str):
        return self.data.visible_clients(permission)


# ---------------------------------------------------------------------------
# resolve_principal() and its per-method resolvers
# ---------------------------------------------------------------------------

def _user_store() -> UserStore:
    return current_app.extensions["user_store"]


def _resolve_session() -> Optional[Principal]:
    """
    Verify Flask's own signed-cookie session, populated by Task 19's
    `gateway/auth/routes.py` `POST /login`: the payload is exactly
    `{uid, u, iat}` (docs/OVERHAUL_PLAN.md section 4.4) --
    `session['uid']`/`session['u']` are checked here; `iat` (issued-at
    epoch) is stored for display/audit purposes but is not re-checked here,
    because Flask/itsdangerous already verifies the cookie's signature and
    its own embedded timestamp against `PERMANENT_SESSION_LIFETIME` when it
    *loads* the cookie, before this function ever runs -- a tampered or
    expired cookie simply deserializes to an empty session, so `uid`/`u`
    being present at all already means "this cookie is authentic and no
    older than the configured lifetime".

    The 12h SLIDING half of "12h sliding lifetime" comes from Flask's own
    `SESSION_REFRESH_EACH_REQUEST` (True by default) combined with
    `session.permanent = True` (set once at login, and persisted inside the
    signed payload itself, so it survives across requests without needing
    to be re-set here): Flask re-issues a fresh `Set-Cookie` with a renewed
    expiry on every response for a permanent session, with zero extra
    scheduling code needed in this module. Re-asserting `session.permanent
    = True` below is defensive/cheap insurance, not load-bearing.

    Beyond what Flask's cookie machinery already guarantees, this function
    re-checks the account's *current* `status` against the DB on every
    single call -- deliberately NOT cached (see Task 19's
    `gateway/auth/cache.py` module docstring, "Invalidation on password
    change/disable", for why that's an acceptable difference from the
    Basic/Bearer path CredentialCache exists for: this is the low-frequency
    human-browser path, not the high-frequency node path that cache
    fronts). A disabled account's session stops working on its very next
    request, and a mismatched `user_id` (e.g. the account was deleted and a
    new, unrelated account later reused the same username) invalidates the
    cookie outright.

    The result is memoized on `g` for the duration of the request. This is
    NOT the caching the paragraph above rules out -- the memo dies with the
    request, so "a disabled account's session stops working on its very next
    request" still holds exactly. It exists because the
    `must_change_password` enforcement hook (`gateway/__init__.py`) runs as a
    `before_request` and needs the same principal `require_auth` is about to
    resolve moments later; without the memo every authenticated browser
    request would issue two identical `get_principal()` joins instead of one.
    `g._session_principal` is deliberately distinct from the public
    `g.principal` that `require_auth` sets on success: this one is "whatever
    the cookie resolves to, including None", and is set even on routes that
    never reach a `require_auth` decorator.
    """
    if "_session_principal" in g:
        return g._session_principal

    principal: Optional[Principal] = None
    uid = session.get("uid")
    username = session.get("u")

    if uid is not None and username is not None:
        data = _user_store().get_principal(username)
        if data is None or data.user_id != uid or data.status != "active":
            session.clear()
        else:
            session.permanent = True
            principal = Principal(data, auth_method="session")

    g._session_principal = principal
    return principal


def _resolve_basic() -> Optional[Principal]:
    """Resolve HTTP Basic credentials via `UserStore.verify_login()`. Real (DB-backed), not a stub."""
    auth = request.authorization
    if auth is None or auth.type != "basic":
        return None

    data = _user_store().verify_login(auth.username, auth.password, source_ip=request.remote_addr)
    if data is None:
        return None
    return Principal(data, auth_method="basic")


def _resolve_bearer() -> Optional[Principal]:
    """Resolve a Bearer token via `UserStore.verify_token()`. Real (DB-backed), not a stub."""
    auth = request.authorization
    if auth is None or auth.type != "bearer":
        return None

    data = _user_store().verify_token(auth.token)
    if data is None:
        return None
    return Principal(data, auth_method="bearer")


_VALID_METHODS = frozenset({"session", "basic", "bearer"})


def resolve_principal(methods: Sequence[str] = DEFAULT_AUTH_METHODS) -> Optional[Principal]:
    """
    Try each method in `methods`, in order, returning the first resolved
    `Principal`. Returns `None` if none of them resolve (or `methods` is
    empty). Raises `ValueError` if `methods` contains an unknown method
    name -- a programming error at the call site, not a request-time
    condition, so it is not caught here.

    Resolvers are looked up dynamically (`globals()[f"_resolve_{method}"]`)
    on every call -- see module docstring -- rather than bound once via a
    dict built at import time, specifically so `_resolve_session` (etc.)
    can be monkeypatched/`mocker.patch`-ed in place by Task 19 or by tests.
    """
    for method in methods:
        if method not in _VALID_METHODS:
            raise ValueError(f"unknown auth method {method!r}; must be one of {sorted(_VALID_METHODS)}")

        resolver = globals()[f"_resolve_{method}"]
        principal = resolver()
        if principal is not None:
            return principal

    return None


# ---------------------------------------------------------------------------
# Failure response: 401 challenge vs. redirect-to-login. See module
# docstring's "Anonymous-request failure behavior" section.
# ---------------------------------------------------------------------------

def _login_redirect(next_path: str) -> Response:
    try:
        target = url_for("auth.login", next=next_path)
    except Exception:
        # Task 19's `auth` blueprint / `login` endpoint doesn't exist yet
        # (or was registered under a different name) -- fall back to the
        # URL the plan documents verbatim (docs/OVERHAUL_PLAN.md 4.5:
        # "anonymous HTML requests -> 302 /login?next=").
        from urllib.parse import urlencode

        target = "/login" + (("?" + urlencode({"next": next_path})) if next_path else "")
    return redirect(target)


def _prefers_json() -> bool:
    """
    True for any request path starting with `/api/` -- see module
    docstring's "`_prefers_json()`" section for the full rationale. A
    request matching this never gets the HTML login-redirect treatment,
    even when "session" is an allowed method and no credentials were
    presented at all.
    """
    return request.path.startswith("/api/")


def _auth_failure_response(methods: Sequence[str]) -> Response:
    presented_credentials = bool(request.authorization)

    if "session" in methods and not presented_credentials and not _prefers_json():
        return _login_redirect(request.full_path if request.query_string else request.path)

    return Response(
        "Authentication required\n",
        401,
        {"WWW-Authenticate": 'Basic realm="Alert Gateway"'},
    )


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------

def require_auth(view: Optional[Callable] = None, *, methods: Sequence[str] = DEFAULT_AUTH_METHODS):
    """
    Require ANY of `methods` (see the API_ONLY / SESSION_ONLY /
    SESSION_OR_API constants above) to resolve a `Principal`. Usable bare
    (default `DEFAULT_AUTH_METHODS`, i.e. the `logs` blueprint's policy) or
    as a factory:

        @require_auth
        def view(): ...

        @require_auth(methods=API_ONLY)
        def view(): ...

    On success: sets `g.principal` and calls the view. On failure: see
    `_auth_failure_response` above (401 challenge or 302 to `/login`).
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any):
            principal = resolve_principal(methods)
            if principal is None:
                return _auth_failure_response(methods)
            g.principal = principal
            return fn(*args, **kwargs)

        return wrapped

    if view is not None:
        return decorator(view)
    return decorator


def require_permission(
    permission: str,
    *,
    get_client: Optional[Callable[[], str]] = None,
    methods: Sequence[str] = DEFAULT_AUTH_METHODS,
):
    """
    Require auth (exactly like `require_auth(methods=methods)`) AND
    `principal.has_permission(permission, client)`, where `client` comes
    from calling `get_client()` (an arbitrary zero-arg callable evaluated
    inside the request context) and normalizing its result, or `""` if
    `get_client` is omitted. See module docstring's "require_permission():
    route-based vs. body-based client resolution" section for the two
    shapes this supports and worked examples.

    Always used as a factory (`permission` is a required positional arg):

        @require_permission("logs:read", get_client=lambda: request.view_args["client"])
        def view(): ...

    On success: `g.principal` is set (by the underlying `require_auth`)
    and the view is called. On failure: the same 401/redirect as
    `require_auth` if authentication itself failed, or a plain 403 (via
    Flask's `abort`) if authentication succeeded but the permission check
    failed.
    """
    auth_decorator = require_auth(methods=methods)

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def permission_checked(*args: Any, **kwargs: Any):
            client = _normalize_client(get_client()) if get_client is not None else ""
            if not g.principal.has_permission(permission, client):
                abort(403)
            return fn(*args, **kwargs)

        return auth_decorator(permission_checked)

    return decorator


def require_master_admin(fn: Callable) -> Callable:
    """
    Require a session-authenticated (never Basic/Bearer -- admin has no
    machine API in v1, per docs/OVERHAUL_PLAN.md section 4.8) principal
    holding the `master_admin` role. Always bare (no configuration --
    every admin route wants exactly this policy):

        @require_master_admin
        def view(): ...

    On success: `g.principal` is set and the view is called. On failure:
    the same 302-to-login as `require_auth(methods=SESSION_ONLY)` if there
    is no valid session, or a plain 403 if there is a valid session but the
    principal is not master_admin.
    """

    @wraps(fn)
    def admin_checked(*args: Any, **kwargs: Any):
        if not g.principal.is_master_admin():
            abort(403)
        return fn(*args, **kwargs)

    return require_auth(methods=SESSION_ONLY)(admin_checked)


def _normalize_client(client: Optional[str]) -> str:
    return (client or "").strip().lower()
