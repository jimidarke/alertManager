"""
Tests for src/gateway/dashboard/routes.py (Task 22): the `dashboard`
blueprint (`GET /dashboard`, `GET /api/dashboard/summary`,
`GET /api/dashboard/groups`), wired through the real `create_app()` (so
ProxyFix/CSRFProtect/blueprint registration are exactly what production
gets), with Postgres/MQTT mocked exactly as tests/test_gateway_app.py
establishes, a FakeUserStore swapped onto app.extensions afterward, and a
FakePool/FakeCursor swapped onto app.extensions['db_pool'] that records
every `cursor.execute(sql, params)` call verbatim -- no live Postgres is
available in this environment (and per project rule, docker/docker-compose
is never invoked from automation), so the security property under test
("does the code construct the right WHERE clause for this principal") is
verified by asserting on the EXACT SQL text and bound parameters a mocked
cursor received, not by running real queries.

Covers, in priority order (per this task's brief):
- THE full client-scoping matrix: master_admin (ALL_CLIENTS sentinel) sees
  everything with NO client filter at all, including null/empty client;
  a single-client-scoped user gets a concrete `client = ANY(%s)` filter to
  exactly their one client, plus the explicit NULL/'' exclusion; a
  multi-client agent gets exactly their (sorted) client list, no others;
  a principal with zero visible clients gets an always-empty-array filter
  (deny, not allow); a principal whose ONLY scope is the clientless ''
  scope still gets the explicit `client != ''` clause even though '' is
  literally in their visible-clients array (the one genuinely load-bearing
  half of the exclusion -- see routes.py's own module docstring section 2).
- Discipline: a `?client=` query string parameter is never read for
  scoping purposes -- the SQL/params are identical with or without one.
- Window enum: all 8 valid values produce the exact documented `since`;
  any other value is 400.
- Zero-filled `counts` (a level with 0 matching rows still appears).
- `/groups`: `levels` parsing (default critical, empty -> counts only with
  the rows query skipped, unknown -> 400), exact SQL for both queries, the
  per-group cap bound as a param, and `_build_group_tree`'s alphabetical
  (clientless-last) client -> source tree with zero-filled counts and
  `matched` summed over the selected levels.
- 503 (with a machine-checkable `error` field) when the DB pool can't be
  reached OR a query fails mid-flight -- for both API routes and the
  `/dashboard` page's DB probe (200-with-body vs. 503-with-warning-banner).
- Session-only auth: Basic/Bearer headers are ignored entirely (never
  resolve a principal for this blueprint); anonymous /api/* requests get a
  plain 401 (not a redirect); an anonymous /dashboard page request DOES
  redirect to /login (browser-page policy, per gateway.principal's
  `_prefers_json()`).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.dashboard import routes as dashboard_routes  # noqa: E402
from userstore import PrincipalData  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: real create_app() with Postgres/MQTT mocked, fakes swapped in
# ---------------------------------------------------------------------------

def _principal_data(
    username="alice",
    user_id=1,
    status="active",
    default_client=None,
    roles=frozenset(),
    scopes=None,
) -> PrincipalData:
    return PrincipalData(
        user_id=user_id,
        username=username,
        status=status,
        default_client=default_client,
        must_change_password=False,
        roles=roles,
        scopes=scopes or {},
    )


class FakeUserStore:
    """Controllable stand-in for userstore.UserStore -- no DB involved. get_principal() backs session resolution."""

    def __init__(self):
        self._users = {}
        self.get_principal_calls = []

    def add_user(self, username, password=None, **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = data
        return data

    def get_principal(self, username):
        self.get_principal_calls.append(username)
        return self._users.get(username)

    def verify_login(self, username, password, *, source_ip=None):
        return None

    def verify_token(self, token):
        return None


class FakeMqttPublisher:
    def is_connected(self):
        return True

    def forward_to_mqtt(self, alert_data):
        return True


class FakeCursor:
    """
    Records every execute(sql, params) call verbatim, in order, and serves
    canned fetchall()/fetchone() results in the order given -- exactly one
    result per execute() call that reads it. `execute_exception`, when
    set, is raised on every execute() call (used for the mid-query-failure
    503 test).
    """

    def __init__(self, fetchall_results=None, fetchone_results=None, execute_exception=None):
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])
        self.execute_exception = execute_exception
        self.executed = []  # [(sql, params), ...]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.execute_exception is not None:
            raise self.execute_exception

    def fetchall(self):
        return self.fetchall_results.pop(0)

    def fetchone(self):
        return self.fetchone_results.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor
        self.rollback_called = False

    def cursor(self, cursor_factory=None):
        return self._cursor

    def rollback(self):
        self.rollback_called = True


class FakePool:
    """Controllable stand-in for the psycopg2 ThreadedConnectionPool -- no real DB involved."""

    def __init__(self, cursor: FakeCursor = None, getconn_exception: Exception = None):
        self._cursor = cursor
        self.getconn_exception = getconn_exception
        self.connections = []
        self.putconn_calls = []

    def getconn(self):
        if self.getconn_exception is not None:
            raise self.getconn_exception
        conn = FakeConnection(self._cursor)
        self.connections.append(conn)
        return conn

    def putconn(self, conn):
        self.putconn_calls.append(conn)


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "e" * 32)
    monkeypatch.setenv("POSTGRES_HOST", "test-postgres")
    monkeypatch.setenv("MQTT_BROKER", "test-broker")
    return GatewayConfig.from_env()


@pytest.fixture
def app(config, mocker):
    mocker.patch("gateway.db.psycopg2_pool.ThreadedConnectionPool", return_value=mocker.MagicMock())
    mocker.patch("gateway.db.UserStore.from_env", return_value=mocker.MagicMock())
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)
    flask_app = create_app(config)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def store(app) -> FakeUserStore:
    fake = FakeUserStore()
    app.extensions["user_store"] = fake
    return fake


@pytest.fixture(autouse=True)
def mqtt(app):
    app.extensions["mqtt_publisher"] = FakeMqttPublisher()


@pytest.fixture
def client(app):
    return app.test_client()


def _install_pool(app, cursor: FakeCursor = None, getconn_exception: Exception = None) -> FakePool:
    pool = FakePool(cursor=cursor, getconn_exception=getconn_exception)
    app.extensions["db_pool"] = pool
    return pool


def _login_session(client, principal: PrincipalData) -> None:
    """Bypass the real /login POST+CSRF flow (that's Task 19's own test file's concern) -- set the session cookie directly, matching `gateway.principal._resolve_session`'s exact `{uid, u}` contract."""
    with client.session_transaction() as sess:
        sess["uid"] = principal.user_id
        sess["u"] = principal.username


def _basic(username, password):
    import base64

    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


MASTER_ADMIN_SCOPES = {"*": frozenset({"logs:read"})}


# ---------------------------------------------------------------------------
# Session-only auth enforcement
# ---------------------------------------------------------------------------

def test_dashboard_page_redirects_anonymous_browser_request(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_summary_401_json_not_redirect_for_anonymous_api_request(client):
    resp = client.get("/api/dashboard/summary")
    assert resp.status_code == 401
    assert "Location" not in resp.headers


def test_groups_401_json_not_redirect_for_anonymous_api_request(client):
    resp = client.get("/api/dashboard/groups")
    assert resp.status_code == 401
    assert "Location" not in resp.headers


def test_summary_ignores_basic_auth_headers_session_only(client, store):
    # Session-only: Basic/Bearer are never in SESSION_ONLY's method tuple,
    # so resolve_principal never even tries them -- a presented (but
    # inapplicable) credential still yields the standard 401 challenge,
    # never a 200.
    store.add_user("alice", "hunter2", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    resp = client.get("/api/dashboard/summary", headers=_basic("alice", "hunter2"))
    assert resp.status_code == 401


def test_summary_succeeds_for_session_authenticated_user(client, store, mocker):
    principal = store.add_user("alice", scopes={"revysmc": frozenset({"logs:read"})})
    _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# THE full client-scoping matrix -- the highest-priority tests in this task.
# Exact SQL text + bound params, verified against a mocked cursor.
# ---------------------------------------------------------------------------

_SUMMARY_UNSCOPED_SQL = "SELECT level, COUNT(*) AS count FROM alerts WHERE timestamp >= to_timestamp(%s) GROUP BY level"
_SUMMARY_SCOPED_SQL = (
    "SELECT level, COUNT(*) AS count FROM alerts WHERE timestamp >= to_timestamp(%s) "
    "AND client = ANY(%s) AND client IS NOT NULL AND client != '' GROUP BY level"
)


def test_scope_master_admin_sees_all_clients_no_filter_including_null_empty(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")
    assert resp.status_code == 200

    cur = pool._cursor
    assert len(cur.executed) == 1
    sql, params = cur.executed[0]
    assert sql == _SUMMARY_UNSCOPED_SQL
    # No client filter at all -- only `since` is bound. This is the ONLY
    # path that can ever see client IS NULL / '' rows.
    assert params == [2_000_000_000 - 86400]
    assert resp.get_json()["scope"] == "all"


def test_principal_data_wildcard_scope_without_master_admin_role_is_all_clients_but_not_admin():
    # Direct, no-Flask sanity check of the exact premise the CRITICAL
    # review finding rests on: constructed straight against the real
    # PrincipalData class (not a mock/fixture), proving
    # visible_clients()'s ALL_CLIENTS sentinel and is_master_admin() are
    # independent computations over disjoint fields (scopes vs. roles) --
    # NOT bundled the way every fixture in this file's `store.add_user()`
    # calls constructs a real master_admin. See routes.py's own module
    # docstring section 2, "Security review finding".
    principal = _principal_data(
        username="wildcard-reader", roles=frozenset(), scopes={"*": frozenset({"logs:read"})}
    )
    assert principal.is_master_admin() is False
    assert principal.visible_clients("logs:read") is dashboard_routes.ALL_CLIENTS


def test_scope_non_master_admin_wildcard_scope_excludes_null_empty_and_has_no_client_any_filter(
    client, store, mocker
):
    # THE fix this review round is about: a non-admin role holding an
    # explicit client='*' scope (a real, supported config --
    # tools/user-manager.py's `add-scope` subcommand explicitly allows
    # granting '*' to any role) must NOT hit the fully-unfiltered
    # master_admin branch. It sees every NAMED client (no `client =
    # ANY(...)` -- there's no enumerable list), but null/empty rows are
    # still excluded, exactly like every other non-master-admin principal.
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("wildcard-reader", roles=frozenset(), scopes={"*": frozenset({"logs:read"})})
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")
    assert resp.status_code == 200

    sql, params = pool._cursor.executed[0]
    assert sql == (
        "SELECT level, COUNT(*) AS count FROM alerts WHERE timestamp >= to_timestamp(%s) "
        "AND client IS NOT NULL AND client != '' GROUP BY level"
    )
    assert "client = ANY" not in sql  # no enumerable client list -- every named client is visible
    assert params == [2_000_000_000 - 86400]  # no client-list param bound at all
    assert resp.get_json()["scope"] == "all"


def test_scope_master_admin_vs_non_admin_wildcard_produce_different_sql_for_the_same_all_clients_visibility(
    client, store, mocker
):
    # Decouples the two conditions the pre-fix tests always bundled
    # together: same visible_clients() == ALL_CLIENTS result, DIFFERENT
    # is_master_admin(), and the generated SQL/response must differ.
    mocker.patch("time.time", return_value=2_000_000_000)

    admin = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    pool_admin = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, admin)
    resp_admin = client.get("/api/dashboard/summary?window=24h")
    admin_sql, admin_params = pool_admin._cursor.executed[0]

    non_admin = store.add_user("wildcard-reader", roles=frozenset(), scopes={"*": frozenset({"logs:read"})})
    pool_non_admin = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, non_admin)
    resp_non_admin = client.get("/api/dashboard/summary?window=24h")
    non_admin_sql, non_admin_params = pool_non_admin._cursor.executed[0]

    assert admin_sql != non_admin_sql
    assert "IS NOT NULL" not in admin_sql
    assert "IS NOT NULL AND client != ''" in non_admin_sql
    # Both report scope == "all" in the response (both see every named
    # client) -- the DIFFERENCE that matters is invisible in `scope` and
    # only shows up in the actual WHERE clause (null/empty exclusion).
    assert resp_admin.get_json()["scope"] == resp_non_admin.get_json()["scope"] == "all"


def test_scope_non_master_admin_wildcard_applies_to_groups_endpoint_too(client, store, mocker):
    # Same fix, /groups side -- verified independently since it builds
    # its SQL via _group_counts_query/_group_rows_query, not
    # _summary_query.
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("wildcard-reader", roles=frozenset(), scopes={"*": frozenset({"logs:read"})})
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?window=24h")
    assert resp.status_code == 200

    count_sql, count_params = pool._cursor.executed[0]
    rows_sql, rows_params = pool._cursor.executed[1]
    assert "client = ANY" not in count_sql
    assert "client = ANY" not in rows_sql
    assert "client IS NOT NULL AND client != ''" in count_sql
    assert "client IS NOT NULL AND client != ''" in rows_sql
    assert count_params == [2_000_000_000 - 86400]
    assert rows_params == [2_000_000_000 - 86400, ["critical"], 30]


@pytest.mark.parametrize(
    "scopes,roles,expected_client_params",
    [
        ({"*": frozenset({"logs:read"})}, frozenset({"master_admin"}), []),
        ({"clientb": frozenset({"logs:read"}), "clienta": frozenset({"logs:read"})}, frozenset(), [["clienta", "clientb"]]),
        ({"": frozenset({"logs:read"})}, frozenset(), [[""]]),
        ({"revysmc": frozenset({"alerts:write"})}, frozenset(), [[]]),
    ],
)
def test_scope_groups_both_queries_carry_the_same_scope_clause(
    client, store, mocker, scopes, roles, expected_client_params
):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("someone", roles=roles, scopes=scopes)
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?window=24h&levels=critical,warning")
    assert resp.status_code == 200

    count_sql, count_params = pool._cursor.executed[0]
    rows_sql, rows_params = pool._cursor.executed[1]
    clause, _ = dashboard_routes._scope_clause(
        principal.visible_clients("logs:read"), principal.is_master_admin()
    )
    assert clause in count_sql and clause in rows_sql
    if roles:
        assert "IS NOT NULL" not in count_sql and "IS NOT NULL" not in rows_sql
    else:
        assert "client IS NOT NULL AND client != ''" in count_sql
        assert "client IS NOT NULL AND client != ''" in rows_sql
    assert count_params == [2_000_000_000 - 86400] + expected_client_params
    assert rows_params == [2_000_000_000 - 86400, ["critical", "warning"]] + expected_client_params + [30]


def test_scope_single_client_user_gets_exactly_their_client_filtered_plus_null_exclusion(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("viewer", scopes={"revysmc": frozenset({"logs:read"})})
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")
    assert resp.status_code == 200

    sql, params = pool._cursor.executed[0]
    assert sql == _SUMMARY_SCOPED_SQL
    assert params == [2_000_000_000 - 86400, ["revysmc"]]
    assert resp.get_json()["scope"] == ["revysmc"]


def test_scope_multi_client_agent_gets_exactly_their_sorted_client_list_no_others(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user(
        "agent", scopes={"clientb": frozenset({"logs:read"}), "clienta": frozenset({"logs:read"})}
    )
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")

    sql, params = pool._cursor.executed[0]
    assert sql == _SUMMARY_SCOPED_SQL
    # Sorted, deterministic order; NOT dict/set iteration order.
    assert params == [2_000_000_000 - 86400, ["clienta", "clientb"]]
    assert resp.get_json()["scope"] == ["clienta", "clientb"]


def test_scope_user_with_no_matching_scopes_gets_always_empty_filter_deny_not_allow(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    # A session-authenticated user holding zero logs:read scopes for any
    # client -- not a 403 (see routes.py's own docstring section 1 for why
    # require_permission was NOT used here), but an always-empty client
    # array, which matches nothing.
    principal = store.add_user("nobody", scopes={"revysmc": frozenset({"alerts:write"})})
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")
    assert resp.status_code == 200

    sql, params = pool._cursor.executed[0]
    assert sql == _SUMMARY_SCOPED_SQL
    assert params == [2_000_000_000 - 86400, []]
    assert resp.get_json()["scope"] == []
    assert resp.get_json()["total"] == 0


def test_scope_clientless_scope_still_gets_explicit_null_exclusion_not_implied_by_any(client, store, mocker):
    # The one genuinely load-bearing half of the exclusion (routes.py
    # module docstring section 2): a non-master_admin role holding an
    # explicit client='' scope has '' literally IN its visible-clients
    # array -- `client = ANY(%s)` alone WOULD match a client='' row for
    # this principal. The query must still carry the explicit
    # `client != ''` clause to close that.
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("misconfigured", roles=frozenset(), scopes={"": frozenset({"logs:read"})})
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary?window=24h")
    assert resp.status_code == 200

    sql, params = pool._cursor.executed[0]
    assert sql == _SUMMARY_SCOPED_SQL
    assert "client IS NOT NULL AND client != ''" in sql
    assert params == [2_000_000_000 - 86400, [""]]  # '' literally in the array...
    assert resp.get_json()["total"] == 0  # ...but the explicit clause still excludes it


# ---------------------------------------------------------------------------
# Discipline: no `client` query parameter is ever read for scoping
# ---------------------------------------------------------------------------

def test_client_query_param_has_zero_effect_on_scoping(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("viewer", scopes={"revysmc": frozenset({"logs:read"})})

    pool_a = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)
    client.get("/api/dashboard/summary?window=24h")
    sql_a, params_a = pool_a._cursor.executed[0]

    pool_b = _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    client.get("/api/dashboard/summary?window=24h&client=some-other-client")
    sql_b, params_b = pool_b._cursor.executed[0]

    assert (sql_a, params_a) == (sql_b, params_b)
    assert params_a == [2_000_000_000 - 86400, ["revysmc"]]  # never "some-other-client"


def test_source_never_reads_client_query_param():
    # Belt-and-suspenders static check: this blueprint's whole design
    # rests on scoping coming ONLY from principal.visible_clients(), never
    # a request-supplied filter -- assert the literal pattern is absent
    # from the source, not just behaviorally unobserved above.
    source = Path(dashboard_routes.__file__).read_text()
    assert 'args.get("client"' not in source
    assert "args['client']" not in source
    assert 'args["client"]' not in source


# ---------------------------------------------------------------------------
# Window enum validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "window,seconds",
    [
        ("1h", 3600),
        ("6h", 21600),
        ("12h", 43200),
        ("24h", 86400),
        ("7d", 604800),
        ("14d", 1209600),
        ("30d", 2592000),
        ("all", 7776000),
    ],
)
def test_window_enum_produces_exact_since(client, store, mocker, window, seconds):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get(f"/api/dashboard/summary?window={window}")
    assert resp.status_code == 200
    assert resp.get_json()["since"] == 2_000_000_000 - seconds


def test_window_missing_defaults_to_24h(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    assert resp.get_json()["since"] == 2_000_000_000 - 86400


@pytest.mark.parametrize("bad_window", ["2h", "24H", "", "1 day", "week", "365d"])
def test_window_invalid_value_is_400(client, store, bad_window):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get(f"/api/dashboard/summary?window={bad_window}")
    assert resp.status_code == 400


def test_window_invalid_value_is_400_for_groups_too(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?window=bogus")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Zero-filled counts
# ---------------------------------------------------------------------------

def test_summary_zero_fills_all_five_levels_when_none_match(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    body = resp.get_json()
    assert body["counts"] == {"critical": 0, "warning": 0, "notice": 0, "info": 0, "debug": 0}
    assert body["total"] == 0


def test_summary_zero_fills_levels_missing_from_partial_results(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    rows = [{"level": "critical", "count": 5}, {"level": "info", "count": 12}]
    _install_pool(client.application, FakeCursor(fetchall_results=[rows]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    body = resp.get_json()
    assert body["counts"] == {"critical": 5, "warning": 0, "notice": 0, "info": 12, "debug": 0}
    assert body["total"] == 17


# ---------------------------------------------------------------------------
# /api/dashboard/groups -- levels parsing, exact SQL, tree assembly
# ---------------------------------------------------------------------------

_TS = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
_TS_EPOCH = int(_TS.timestamp())


def _count_row(client_name, source, level, count, ts=_TS):
    return {"client": client_name, "source": source, "level": level, "count": count, "last_seen": ts}


def _alert_row(alert_id, client_name, source, level="critical", ts=_TS):
    return {
        "id": alert_id,
        "timestamp": ts,
        "level": level,
        "category": "system",
        "title": f"T{alert_id}",
        "message": f"M{alert_id}",
        "source": source,
        "client": client_name,
        "metadata": None,
        "ingestion_source": "http",
    }


def test_groups_exact_sql_for_master_admin(client, store, mocker):
    mocker.patch("time.time", return_value=2_000_000_000)
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    client.get("/api/dashboard/groups?window=1h&levels=warning,critical")

    count_sql, count_params = pool._cursor.executed[0]
    rows_sql, rows_params = pool._cursor.executed[1]
    assert count_sql == (
        "SELECT COALESCE(client, '') AS client, source, level, COUNT(*) AS count, MAX(timestamp) AS last_seen "
        "FROM alerts WHERE timestamp >= to_timestamp(%s) GROUP BY 1, 2, 3"
    )
    assert count_params == [2_000_000_000 - 3600]
    assert rows_sql == (
        "SELECT id, timestamp, level, category, title, message, source, client, metadata, ingestion_source "
        "FROM (SELECT id, timestamp, level, category, title, message, source, client, metadata, ingestion_source, "
        "ROW_NUMBER() OVER (PARTITION BY COALESCE(client, ''), source ORDER BY timestamp DESC, id DESC) AS rn "
        "FROM alerts WHERE timestamp >= to_timestamp(%s) AND level = ANY(%s)) ranked "
        "WHERE rn <= %s ORDER BY timestamp DESC, id DESC"
    )
    # Levels come back in canonical order regardless of request order.
    assert rows_params == [2_000_000_000 - 3600, ["critical", "warning"], 30]


def test_groups_levels_default_to_critical(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups")
    assert resp.get_json()["levels"] == ["critical"]
    assert resp.get_json()["per_group"] == 30
    assert pool._cursor.executed[1][1][1] == ["critical"]


def test_groups_levels_are_trimmed_lowercased_and_deduped(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?levels=%20Debug%20,info,INFO,")
    assert resp.status_code == 200
    assert resp.get_json()["levels"] == ["info", "debug"]


def test_groups_empty_levels_skips_rows_query(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    pool = _install_pool(client.application, FakeCursor(fetchall_results=[[_count_row("acme-co", "web-01", "info", 3)]]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?levels=")
    assert resp.status_code == 200
    assert len(pool._cursor.executed) == 1  # counts only
    body = resp.get_json()
    assert body["levels"] == []
    group = body["clients"][0]["sources"][0]
    assert group["counts"]["info"] == 3
    assert group["matched"] == 0
    assert group["alerts"] == []


@pytest.mark.parametrize("bad_levels", ["fatal", "critical,bogus", "error"])
def test_groups_unknown_level_is_400(client, store, bad_levels):
    # Note "error" is an INGESTION alias for critical, not a stored level --
    # the dashboard only accepts the five canonical stored values.
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor(fetchall_results=[[], []]))
    _login_session(client, principal)

    resp = client.get(f"/api/dashboard/groups?levels={bad_levels}")
    assert resp.status_code == 400


def test_groups_builds_sorted_tree_with_counts_matched_and_alerts(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    later = datetime(2026, 7, 20, 13, 0, 0, tzinfo=timezone.utc)
    count_rows = [
        _count_row("", "cron", "critical", 1),
        _count_row("zeta", "b-src", "info", 9),
        _count_row("acme-co", "web-02", "debug", 4, ts=later),
        _count_row("acme-co", "Web-01", "critical", 2),
        _count_row("acme-co", "Web-01", "warning", 5),
        _count_row("acme-co", "Web-01", "info", 40),
    ]
    alert_rows = [
        _alert_row(11, "acme-co", "Web-01", "warning", ts=later),
        _alert_row(10, "acme-co", "Web-01", "critical"),
        _alert_row(12, None, "cron", "critical"),
    ]
    _install_pool(client.application, FakeCursor(fetchall_results=[count_rows, alert_rows]))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups?levels=critical,warning")
    assert resp.status_code == 200
    clients = resp.get_json()["clients"]

    # Alphabetical clients, clientless '' always last.
    assert [c["client"] for c in clients] == ["acme-co", "zeta", ""]

    acme = clients[0]
    assert [s["source"] for s in acme["sources"]] == ["Web-01", "web-02"]  # case-insensitive
    assert acme["counts"] == {"critical": 2, "warning": 5, "notice": 0, "info": 40, "debug": 4}
    assert acme["matched"] == 7
    assert acme["last_seen"] == int(later.timestamp())

    web01 = acme["sources"][0]
    assert web01["counts"] == {"critical": 2, "warning": 5, "notice": 0, "info": 40, "debug": 0}
    assert web01["matched"] == 7  # may exceed len(alerts) -- the "showing 2 of 7" case
    assert web01["last_seen"] == _TS_EPOCH
    assert [a["id"] for a in web01["alerts"]] == [11, 10]  # SQL order (newest first) preserved
    assert web01["alerts"][0] == {
        "id": 11,
        "timestamp": int(later.timestamp()),
        "level": "warning",
        "category": "system",
        "title": "T11",
        "message": "M11",
        "source": "Web-01",
        "client": "acme-co",
        "metadata": None,
        "ingestion_source": "http",
    }

    web02 = acme["sources"][1]
    assert web02["matched"] == 0
    assert web02["alerts"] == []

    zeta = clients[1]
    assert zeta["matched"] == 0
    assert zeta["counts"]["info"] == 9

    clientless = clients[2]
    assert [a["id"] for a in clientless["sources"][0]["alerts"]] == [12]  # NULL client row -> '' group


def test_groups_message_is_full_untruncated(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    row = _alert_row(1, "acme-co", "src")
    row["message"] = "x" * 5000
    _install_pool(
        client.application,
        FakeCursor(fetchall_results=[[_count_row("acme-co", "src", "critical", 1)], [row]]),
    )
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups")
    assert len(resp.get_json()["clients"][0]["sources"][0]["alerts"][0]["message"]) == 5000


def test_groups_ignores_out_of_enum_level_in_counts():
    tree = dashboard_routes._build_group_tree(
        [_count_row("a", "s", "critical", 1), _count_row("a", "s", "bogus", 99)], [], ["critical"]
    )
    assert tree[0]["counts"] == {"critical": 1, "warning": 0, "notice": 0, "info": 0, "debug": 0}
    assert tree[0]["matched"] == 1


# ---------------------------------------------------------------------------
# 503 on DB pool down / query failure
# ---------------------------------------------------------------------------

def test_summary_503_when_pool_getconn_fails(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, getconn_exception=RuntimeError("connection refused"))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["status"] == "error"
    assert body["error"] == "database unavailable"


def test_summary_503_when_query_execution_fails_and_rolls_back(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    cur = FakeCursor(execute_exception=RuntimeError("syntax error at or near"))
    pool = _install_pool(client.application, cur)
    _login_session(client, principal)

    resp = client.get("/api/dashboard/summary")
    assert resp.status_code == 503
    assert pool.connections[0].rollback_called is True
    assert pool.putconn_calls == pool.connections  # connection still returned to the pool


def test_groups_503_when_pool_getconn_fails(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, getconn_exception=RuntimeError("pool exhausted"))
    _login_session(client, principal)

    resp = client.get("/api/dashboard/groups")
    assert resp.status_code == 503
    assert resp.get_json()["error"] == "database unavailable"


def test_dashboard_page_503_with_warning_banner_when_db_down(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, getconn_exception=RuntimeError("connection refused"))
    _login_session(client, principal)

    resp = client.get("/dashboard")
    assert resp.status_code == 503
    assert resp.mimetype == "text/html"
    assert "temporarily unavailable" in resp.get_data(as_text=True)


def test_dashboard_page_200_when_db_ok(client, store):
    principal = store.add_user("root", roles=frozenset({"master_admin"}), scopes=MASTER_ADMIN_SCOPES)
    _install_pool(client.application, FakeCursor())
    _login_session(client, principal)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "temporarily unavailable" not in resp.get_data(as_text=True)
    assert "Alert Manager - Dashboard" in resp.get_data(as_text=True)
