"""
Alert Manager - gateway.dashboard.routes

`dashboard_bp`: `GET /dashboard` (renders `templates/dashboard.html`, built
in Task 25 atop Task 24's `layout.html`), `GET /api/dashboard/summary`,
`GET /api/dashboard/critical` (Task 22, docs/OVERHAUL_PLAN.md section 4.6).

--------------------------------------------------------------------------
1. Auth decorator decision: `require_auth(methods=SESSION_ONLY)`, NOT
   `require_permission`
--------------------------------------------------------------------------
Every other permission-gated route built so far (`POST /alert` in Task 20,
`gateway/logs/files.py`'s `file/{client}/**` tier in Task 21) checks
authorization against exactly ONE client, resolved from the route/body via
`require_permission(permission, get_client=...)`. Both endpoints in this
blueprint are structurally different: they are inherently multi-client --
"give me the alert counts/critical alerts across every client I'm allowed
to see", not "am I allowed to act on client X". There is no single
`client` string to hand `require_permission`/`has_permission()`; the
*authorization itself* is expressed as a SQL WHERE-clause scope, not a
single boolean gate in front of the whole route.

So: `require_auth(methods=SESSION_ONLY)` (authentication only -- any
active session-authenticated principal may reach the view body) plus a
direct call to `principal.visible_clients(DASHBOARD_PERMISSION)` inside
each view, used to build the query's scope predicate (`_scope_clause`
below). This means a session-authenticated user holding ZERO `logs:read`
scopes for any client is not rejected with a 403 -- they simply get back
an all-zero summary / an empty critical list (`visible_clients()` returns
an empty frozenset, `_scope_clause` turns that into `client = ANY(%s)`
with an empty array, which matches no rows in Postgres). This is
deliberate, not an oversight: the "authorization decision" for this
blueprint IS the scoping predicate, enforced identically for every
principal via the exact same code path (there is no separate all-or-
nothing gate to get wrong or forget) -- an empty result set is exactly as
safe as a 403 here, and considerably simpler than threading a synthetic
"any client at all" check through `require_permission` the way
`gateway/logs/files.py`'s own module docstring describes rejecting for
its `ops/**`/flat-file tiers (see `gateway/principal.py`'s "route-based
vs. body-based client resolution" section, and that file's note on why it
also bypassed `require_permission`, for the same class of problem: a
built-for-one-client-per-request decorator doesn't fit a request that is
inherently about a SET of clients).

`SESSION_ONLY` already exists exactly as needed (`gateway/principal.py`'s
per-blueprint auth policy table lists `dashboard` as one of its two
designed consumers, alongside `admin`/Task 23) -- no change to
`gateway/principal.py` was needed for this task. `/api/dashboard/*`
requests get the correct 401-not-redirect treatment for anonymous JSON
callers "for free", via `principal.py`'s existing path-based
`_prefers_json()` check (any `/api/*` path never gets the HTML-redirect
treatment) -- see that module's own docstring. `/dashboard` (not under
`/api/`) DOES redirect an anonymous browser request to `/login`, which is
the desired page-auth UX.

--------------------------------------------------------------------------
2. `_scope_clause`: THE security-critical predicate, and the NULL/''
   exclusion analysis
--------------------------------------------------------------------------
`principal.visible_clients(DASHBOARD_PERMISSION)` (Task 15's scoping
primitive -- see `userstore/store.py`'s `PrincipalData.visible_clients()`
docstring for its full contract) returns either the `ALL_CLIENTS` sentinel
("this principal holds a client='*' scope for this permission -- do not
filter at all") or a `frozenset` of the exact client names visible for
this permission (which may be EMPTY -- "see nothing", never "see
everything").

`_scope_clause(visible, is_master_admin)` converts that into a SQL fragment
+ bound params -- a THREE-way branch, not two, gated on BOTH `visible` AND
`is_master_admin` (`principal.is_master_admin()`, `userstore/store.py`
`PrincipalData.is_master_admin()`):

  - `is_master_admin` True -> `("", [])`: no filter appended at all. This
    is the ONLY branch that may ever see a row with `client IS NULL` or
    `client = ''`.
  - `is_master_admin` False and `visible is ALL_CLIENTS` -> `" AND client
    IS NOT NULL AND client != ''"` (no `client = ANY(...)` -- there is no
    enumerable list to filter to; every NAMED client is legitimately
    visible), but null/empty rows are still excluded exactly like any
    other non-master-admin principal.
  - Otherwise (a concrete frozenset, including empty) ->
    `" AND client = ANY(%s) AND client IS NOT NULL AND client != ''"`,
    bound to `[sorted(visible)]`.

**Security review finding, fixed here (do not regress this):** an earlier
version of this function branched on `visible is ALL_CLIENTS` ALONE,
treating it as a safe proxy for "this is master_admin, show everything
including null/empty". That is wrong, and was caught in review by
constructing `PrincipalData` directly (not through a role-bundling test
fixture): `visible_clients(permission)` (`userstore/store.py`) computes its
`ALL_CLIENTS` return value ENTIRELY from `self.scopes.get("*")` -- it never
reads `self.roles` at all. `is_master_admin()` computes ENTIRELY from
`self.roles` (`MASTER_ADMIN_ROLE in self.roles`) -- it never reads
`self.scopes`. These are two independent computations over disjoint
fields of the same dataclass; nothing enforces that only the seeded
`master_admin` role can hold a `client='*'` scope. This is not a
contrived hypothetical: `role_scopes.client` is unconstrained free-form
TEXT (no CHECK, no code-level guard), and `tools/user-manager.py`'s
already-shipped `add-scope` subcommand explicitly documents `client` as
accepting `'*'` for ANY role -- an operator running
`user-manager.py add-scope some-non-admin-role '*' logs:read` (a
plausible "grant cross-client read access without granting full admin"
request) produces a `PrincipalData` with `visible_clients('logs:read') is
ALL_CLIENTS` and `is_master_admin() is False` simultaneously. The old
code would have shown that principal every row in the table, including
clientless/system alerts -- a direct violation of this blueprint's
"master_admin-only visibility for null/empty client" rule. The fix is to
call and check BOTH `visible_clients(...)` and `is_master_admin()`
explicitly, never inferring one from the other.

Whether that explicit `client IS NOT NULL AND client != ''` half is
redundant with `client = ANY(%s)` alone -- verified against Postgres NULL-
comparison semantics, not assumed, per this task's explicit instruction:

  * The NULL half IS implied already. `NULL = ANY(ARRAY[...])` always
    evaluates to NULL (never TRUE) in Postgres -- `ANY` is defined in
    terms of `=` applied element-wise and OR'd together, and `NULL = x` is
    NULL for every `x`, so the whole `ANY(...)` expression is NULL, and a
    `WHERE` clause treats a NULL condition exactly like FALSE. A
    `client IS NULL` row can therefore never match `client = ANY(%s)` no
    matter what the array contains, for any principal. Keeping
    `client IS NOT NULL` in the SQL anyway is cheap, self-documenting
    defense-in-depth (readable proof of the invariant at the query site,
    and future-proof against a hypothetical future rewrite of this clause
    that stops using `= ANY`) -- not a gap this task needed to close.
  * The empty-string half is NOT implied, and IS the one genuinely
    load-bearing half of this exclusion. `'' = ANY(%s)` is an ordinary
    string comparison, not a NULL comparison -- if a principal's
    `visible_clients()` set happens to literally CONTAIN `''` (a role
    with an explicit `add_scope(role, client='', permission='logs:read')`
    -- exactly the same "clientless scope" misconfiguration
    `gateway/api/routes.py`'s `_require_alert_permission` already has to
    guard against explicitly for `POST /alert`, by requiring
    `is_master_admin()` in addition to `has_permission()` for the
    clientless case), `client = ANY(%s)` WOULD match a `client = ''` row
    for that principal -- `''` is a perfectly ordinary array element as
    far as `=` is concerned. Without the explicit `client != ''` clause, a
    non-master_admin role holding such a scope could see clientless
    alerts through this blueprint, directly contradicting this task's
    brief ("`client IS NULL/''` rows visible to master_admin only" /
    "exclude rows with null/empty client from both endpoints' results
    even if their scope would otherwise seem to match"). This clause is
    what actually closes that gap.

Net conclusion: keep BOTH halves. The NULL half is provably redundant but
harmless and clarifying; the `''` half is NOT redundant and is required
for correctness. Removing either would either lose defense-in-depth
documentation (NULL half) or open a real cross-tenant data leak for a
plausible role-scope misconfiguration (`''` half).

`_scope_clause`/`_scope_repr` are the ONLY functions in this module that
touch client scoping, and every query builder below (`_summary_query`,
`_critical_count_query`, `_critical_rows_query`) routes through
`_scope_clause` -- there is exactly one place a reviewer needs to look to
audit the entire cross-client leak surface of this blueprint. No route in
this module ever reads a `client` query parameter, form field, or JSON
body key for scoping purposes -- the scope is ALWAYS server-derived from
`g.principal.visible_clients(...)`, never from anything request-supplied.

--------------------------------------------------------------------------
3. Window enum, `/critical` pagination, and DB-pool-down 503 shape
--------------------------------------------------------------------------
`window` is validated against the exact 8-value enum the brief specifies
(`_WINDOW_SECONDS`); anything else, including a missing/malformed value
that doesn't match a key, is a 400 (`window` itself missing entirely
defaults to `"24h"` -- a friendly default, not a 400, matching this
codebase's general convention of defaulting optional query params rather
than demanding every one be spelled out, e.g. `logs/routes.py`'s
`position`/`lines` defaults).

`/api/dashboard/critical`'s `limit` is CLAMPED to `MAX_LIMIT` (200), not
rejected with a 400, when a caller asks for more -- a judgment call (the
brief explicitly leaves this open). Clamping was chosen over rejection
because an over-large `limit` is not a caller error in the way a
malformed/negative value is (this is a browser dashboard, not a strict
API contract callers need hard feedback from); silently capping keeps the
page working with a slightly smaller page than requested rather than
surfacing an error for what is, from the caller's perspective, a harmless
request. `limit < 1` and `offset < 0` (or either being non-numeric) ARE
rejected with 400 -- those are not "reasonable but oversized", they are
malformed.

DB pool down: `gateway.db.pooled_cursor()` (hoisted there in the final
whole-branch cleanup pass -- this module originally carried its own copy,
named `_pooled_cursor`, which was the most complete of several
near-identical implementations that had accumulated across blueprints; see
that module's docstring) wraps `pool.getconn()` AND the query
execution/fetch in one exception boundary, normalizing ANY failure there
(pool exhaustion, connection refused, a query erroring out mid-flight) to
`gateway.db.DBUnavailable`, which every route below turns into the SAME
503 shape: `{"status": "error", "error": "database unavailable", "message":
"...", "detail": str(exc)}`. `error` is a fixed, machine-checkable string
Task 25's frontend can key its warning-banner logic off; `detail` is
included because these routes are already session-authenticated (unlike
`gateway/api/routes.py`'s unauthenticated `/health`, which sets the same
precedent of including `str(exc)` in its DB-status detail field even
without auth) -- there's no meaningfully more sensitive audience to
protect it from. `GET /dashboard` uses the SAME probe pattern (a cheap
`SELECT 1` via the same shared `pooled_cursor()`, matching
`gateway/api/routes.py`'s `_check_db_health`) to decide whether to render
`dashboard.html` (Task 25) with a 200 or a warning-banner paragraph with a
503.
"""

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from flask import Blueprint, Response, current_app, g, jsonify, render_template, request

from gateway.db import DBUnavailable, iso_now, pooled_cursor
from gateway.principal import SESSION_ONLY, require_auth
from userstore import ALL_CLIENTS

logger = logging.getLogger("gateway.dashboard")

dashboard_bp = Blueprint("dashboard", __name__)

# The one permission this whole blueprint scopes against -- matches the
# brief's own worked example (`principal.visible_clients('logs:read')`):
# the dashboard is a read-only view over the same alert data the log
# viewer (Task 21) already gates behind `logs:read`, so it reuses that
# permission rather than inventing a new one.
DASHBOARD_PERMISSION = "logs:read"

# Exact window enum -> seconds, per the brief. "all" is a 90-day cap
# (7,776,000s), not literally unbounded -- matching CLAUDE.md's "no
# failsafes, but also no unbounded full-table scans" spirit and the
# brief's explicit value.
_WINDOW_SECONDS: Dict[str, int] = {
    "1h": 3600,
    "6h": 21600,
    "12h": 43200,
    "24h": 86400,
    "7d": 604800,
    "14d": 1209600,
    "30d": 2592000,
    "all": 7776000,
}
DEFAULT_WINDOW = "24h"

# CLAUDE.md's canonical alert-level order -- all five always present in
# `/api/dashboard/summary`'s `counts`, zero-filled.
_LEVELS: Tuple[str, ...] = ("critical", "warning", "notice", "info", "debug")

DEFAULT_LIMIT = 50
MAX_LIMIT = 200


def _db_pool():
    return current_app.extensions["db_pool"]


# ---------------------------------------------------------------------------
# DB access: one exception boundary, normalized to gateway.db.DBUnavailable.
# See module docstring section 3. The pooled-cursor plumbing itself
# (`pooled_cursor`/`DBUnavailable`) lives in gateway/db.py, shared with
# `api/routes.py` and `admin/routes.py` -- this module used to carry its
# own copy (`_pooled_cursor`/`DashboardDBUnavailable`), hoisted out in the
# final whole-branch cleanup pass.
# ---------------------------------------------------------------------------

def _db_unavailable_response(exc: Exception) -> Tuple[Response, int]:
    logger.error("Dashboard DB query failed: %s", exc)
    return (
        jsonify(
            {
                "status": "error",
                "error": "database unavailable",
                "message": "Dashboard data is temporarily unavailable; please try again shortly.",
                "detail": str(exc),
                "timestamp": iso_now(),
            }
        ),
        503,
    )


def _bad_request(message: str) -> Tuple[Response, int]:
    return jsonify({"status": "error", "message": message}), 400


# ---------------------------------------------------------------------------
# SQL construction -- centralized here, and ONLY here. See module docstring
# section 2 for the full NULL/''-exclusion analysis. Every query in this
# blueprint is built by one of the three functions below, all of which
# route client scoping through `_scope_clause`.
# ---------------------------------------------------------------------------

def _scope_clause(visible: Any, is_master_admin: bool) -> Tuple[str, List[Any]]:
    """
    `visible` is `principal.visible_clients(DASHBOARD_PERMISSION)`;
    `is_master_admin` is `principal.is_master_admin()` -- BOTH must be
    passed in and BOTH are consulted, independently. See module docstring
    section 2 ("Security review finding") for why `visible is ALL_CLIENTS`
    is NOT, on its own, a safe proxy for "this is master_admin, show
    everything including null/empty" -- `visible_clients()` and
    `is_master_admin()` are computed from disjoint fields (`scopes` vs.
    `roles`) of the same `PrincipalData`, and nothing ties a `client='*'`
    scope to the master_admin role.

    Returns `(sql_fragment, params)` to append after an existing
    `WHERE timestamp >= to_timestamp(%s)` clause:

      - `is_master_admin` -> `("", [])`: no filter at all. The ONLY branch
        that may ever see `client IS NULL` / `client = ''` rows.
      - not `is_master_admin` and `visible is ALL_CLIENTS` -> every named
        client visible, null/empty excluded, no `client = ANY(...)` (no
        enumerable list exists to filter to).
      - otherwise -> `client = ANY(%s)` bound to `sorted(visible)`, plus
        the same null/empty exclusion.
    """
    if is_master_admin:
        return "", []
    if visible is ALL_CLIENTS:
        return " AND client IS NOT NULL AND client != ''", []
    clients = sorted(visible)
    return " AND client = ANY(%s) AND client IS NOT NULL AND client != ''", [clients]


def _scope_repr(visible: Any) -> Any:
    """Human/frontend-facing representation of what was queried, for the response's `scope` field."""
    if visible is ALL_CLIENTS:
        return "all"
    return sorted(visible)


def _summary_query(since: int, visible: Any, is_master_admin: bool) -> Tuple[str, List[Any]]:
    clause, clause_params = _scope_clause(visible, is_master_admin)
    sql = "SELECT level, COUNT(*) AS count FROM alerts WHERE timestamp >= to_timestamp(%s)" + clause + " GROUP BY level"
    return sql, [since] + clause_params


def _critical_count_query(since: int, visible: Any, is_master_admin: bool) -> Tuple[str, List[Any]]:
    clause, clause_params = _scope_clause(visible, is_master_admin)
    sql = (
        "SELECT COUNT(*) AS count FROM alerts "
        "WHERE timestamp >= to_timestamp(%s) AND level = 'critical'" + clause
    )
    return sql, [since] + clause_params


def _critical_rows_query(
    since: int, visible: Any, is_master_admin: bool, limit: int, offset: int
) -> Tuple[str, List[Any]]:
    clause, clause_params = _scope_clause(visible, is_master_admin)
    sql = (
        "SELECT id, timestamp, category, title, message, source, client, metadata, ingestion_source "
        "FROM alerts WHERE timestamp >= to_timestamp(%s) AND level = 'critical'" + clause +
        " ORDER BY timestamp DESC LIMIT %s OFFSET %s"
    )
    return sql, [since] + clause_params + [limit, offset]


def _serialize_critical_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    `alerts.timestamp` is `TIMESTAMPTZ` (sql/init/01-schema.sql); psycopg2
    hands it back as a timezone-aware `datetime`, converted here to a Unix
    epoch integer per CLAUDE.md's "Timestamps are always communicated ...
    Unix epoch". `message` is passed through FULL/untruncated -- truncation
    is explicitly Task 25's (client-side) job, per the brief.
    """
    ts = row["timestamp"]
    epoch = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
    return {
        "id": row["id"],
        "timestamp": epoch,
        "category": row["category"],
        "title": row["title"],
        "message": row["message"],
        "source": row["source"],
        "client": row["client"],
        "metadata": row["metadata"],
        "ingestion_source": row["ingestion_source"],
    }


# ---------------------------------------------------------------------------
# Query-param parsing/validation
# ---------------------------------------------------------------------------

def _parse_window_seconds(raw: str) -> Tuple[Optional[int], Optional[Tuple[Response, int]]]:
    if raw not in _WINDOW_SECONDS:
        return None, _bad_request(f"invalid window {raw!r}; must be one of {sorted(_WINDOW_SECONDS)}")
    return _WINDOW_SECONDS[raw], None


def _parse_offset(raw: str) -> Tuple[Optional[int], Optional[Tuple[Response, int]]]:
    try:
        offset = int(raw)
    except (TypeError, ValueError):
        return None, _bad_request("offset must be an integer")
    if offset < 0:
        return None, _bad_request("offset must be >= 0")
    return offset, None


def _parse_limit(raw: str) -> Tuple[Optional[int], Optional[Tuple[Response, int]]]:
    """`limit` is clamped to MAX_LIMIT when over, rejected only when non-numeric or < 1. See module docstring section 3."""
    try:
        limit = int(raw)
    except (TypeError, ValueError):
        return None, _bad_request("limit must be an integer")
    if limit < 1:
        return None, _bad_request("limit must be >= 1")
    return min(limit, MAX_LIMIT), None


# ---------------------------------------------------------------------------
# GET /dashboard -- renders templates/dashboard.html (Task 25)
# ---------------------------------------------------------------------------

def _probe_db(pool) -> bool:
    """Cheap `SELECT 1` liveness probe via the shared `pooled_cursor`, mirroring `gateway/api/routes.py`'s `_check_db_health` -- fail-open shape (bool), not raising."""
    try:
        with pooled_cursor(pool) as cur:
            cur.execute("SELECT 1")
        return True
    except DBUnavailable as exc:
        logger.warning("Dashboard page DB probe failed: %s", exc)
        return False


@dashboard_bp.route("/dashboard", methods=["GET"])
@require_auth(methods=SESSION_ONLY)
def view_dashboard():
    """
    Renders `templates/dashboard.html` (Task 25), extending `layout.html`
    (Task 24). Probes the pool up front (the same cheap `SELECT 1` this
    function has always used) purely to decide the initial HTTP status
    code (200 vs. 503) and whether to render a server-side "DB down"
    banner on first paint -- `dashboard.html`'s own JS independently
    fetches `/api/dashboard/summary` and `/api/dashboard/critical` on load
    (and on every subsequent timeframe change / auto-refresh tick) and
    handles a 503 from THOSE calls with its own client-side banner
    (`_db_unavailable_response`'s JSON shape); the two paths are
    independent defense, not a hand-off -- a DB that comes back up between
    this probe and the page's first fetch() self-heals via the client-side
    path with no extra plumbing here.
    """
    db_ok = _probe_db(_db_pool())
    html = render_template("dashboard.html", db_ok=db_ok)
    return html, (200 if db_ok else 503)


# ---------------------------------------------------------------------------
# GET /api/dashboard/summary
# ---------------------------------------------------------------------------

@dashboard_bp.route("/api/dashboard/summary", methods=["GET"])
@require_auth(methods=SESSION_ONLY)
def dashboard_summary():
    """`{counts: {critical..debug}, total, since, scope, generated_at}`. See module docstring sections 1-3."""
    window = request.args.get("window", DEFAULT_WINDOW)
    seconds, error = _parse_window_seconds(window)
    if error is not None:
        return error

    now = int(time.time())
    since = now - seconds
    visible = g.principal.visible_clients(DASHBOARD_PERMISSION)
    is_master_admin = g.principal.is_master_admin()

    sql, params = _summary_query(since, visible, is_master_admin)
    try:
        with pooled_cursor(_db_pool()) as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except DBUnavailable as exc:
        return _db_unavailable_response(exc)

    counts = {level: 0 for level in _LEVELS}
    for row in rows:
        level = row["level"]
        if level in counts:
            counts[level] = row["count"]
        # else: a level outside the canonical five somehow made it into the
        # table (shouldn't happen given chk_level) -- silently excluded
        # from `counts`, but still counted in nothing else, so `total`
        # stays an exact sum of the zero-filled dict rather than drifting
        # from it.

    return jsonify(
        {
            "counts": counts,
            "total": sum(counts.values()),
            "since": since,
            "scope": _scope_repr(visible),
            "generated_at": now,
        }
    )


# ---------------------------------------------------------------------------
# GET /api/dashboard/critical
# ---------------------------------------------------------------------------

@dashboard_bp.route("/api/dashboard/critical", methods=["GET"])
@require_auth(methods=SESSION_ONLY)
def dashboard_critical():
    """`{alerts: [{id, timestamp, category, title, message, source, client, metadata, ingestion_source}, ...], total, ...}`."""
    window = request.args.get("window", DEFAULT_WINDOW)
    seconds, error = _parse_window_seconds(window)
    if error is not None:
        return error

    offset, error = _parse_offset(request.args.get("offset", "0"))
    if error is not None:
        return error

    limit, error = _parse_limit(request.args.get("limit", str(DEFAULT_LIMIT)))
    if error is not None:
        return error

    now = int(time.time())
    since = now - seconds
    visible = g.principal.visible_clients(DASHBOARD_PERMISSION)
    is_master_admin = g.principal.is_master_admin()

    count_sql, count_params = _critical_count_query(since, visible, is_master_admin)
    rows_sql, rows_params = _critical_rows_query(since, visible, is_master_admin, limit, offset)

    try:
        with pooled_cursor(_db_pool()) as cur:
            cur.execute(count_sql, count_params)
            total = cur.fetchone()["count"]
            cur.execute(rows_sql, rows_params)
            rows = cur.fetchall()
    except DBUnavailable as exc:
        return _db_unavailable_response(exc)

    return jsonify(
        {
            "alerts": [_serialize_critical_row(row) for row in rows],
            "total": total,
            "since": since,
            "scope": _scope_repr(visible),
            "generated_at": now,
            "offset": offset,
            "limit": limit,
        }
    )
