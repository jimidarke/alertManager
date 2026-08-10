"""
Tests for src/gateway/db.py (Task 18): pool construction and app.extensions
wiring, with psycopg2's ThreadedConnectionPool and UserStore mocked -- no
live Postgres is available in this environment (and per project rule,
docker/docker-compose is never invoked from automation).

Also covers `iso_now()`/`pooled_cursor()`/`DBUnavailable` (added in the
final whole-branch cleanup pass): shared query plumbing hoisted here from
three near-duplicate implementations that had accumulated in
`api/routes.py`, `dashboard/routes.py`, and `admin/routes.py`. Those
blueprints' own test files cover the resulting HTTP-level behavior
end-to-end (fail-open `/health`, fail-closed 503s on `/api/dashboard/*`,
etc.); the tests here exercise `pooled_cursor()`'s own connection-lifecycle
contract directly, against a fake pool/connection/cursor.
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway.config import GatewayConfig  # noqa: E402
from gateway.db import DBUnavailable, create_db_pool, init_db, iso_now, pooled_cursor  # noqa: E402


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "c" * 32)
    monkeypatch.setenv("POSTGRES_HOST", "test-postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5432")
    monkeypatch.setenv("POSTGRES_DB", "testdb")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("POSTGRES_POOL_SIZE", "4")
    monkeypatch.setenv("POSTGRES_MAX_OVERFLOW", "6")
    return GatewayConfig.from_env()


# ---------------------------------------------------------------------------
# create_db_pool(): connection parameters + fail-fast on construction error
# ---------------------------------------------------------------------------

def test_create_db_pool_passes_correct_connection_parameters(config, mocker):
    mock_pool_cls = mocker.patch("gateway.db.psycopg2_pool.ThreadedConnectionPool")

    create_db_pool(config)

    mock_pool_cls.assert_called_once_with(
        minconn=4,
        maxconn=10,  # minconn(4) + max_overflow(6)
        host="test-postgres",
        port=5432,
        database="testdb",
        user="testuser",
        password="secret",
    )


def test_create_db_pool_returns_the_constructed_pool(config, mocker):
    fake_pool = mocker.MagicMock()
    mocker.patch("gateway.db.psycopg2_pool.ThreadedConnectionPool", return_value=fake_pool)

    result = create_db_pool(config)

    assert result is fake_pool


def test_create_db_pool_exits_on_construction_failure(config, mocker):
    mocker.patch(
        "gateway.db.psycopg2_pool.ThreadedConnectionPool",
        side_effect=Exception("connection refused"),
    )

    with pytest.raises(SystemExit) as exc_info:
        create_db_pool(config)

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# init_db(): app.extensions wiring
# ---------------------------------------------------------------------------

class _FakeApp:
    def __init__(self):
        self.extensions = {}


def test_init_db_wires_pool_and_user_store_onto_app_extensions(config, mocker):
    fake_pool = mocker.MagicMock()
    mocker.patch("gateway.db.create_db_pool", return_value=fake_pool)
    fake_store = mocker.MagicMock()

    app = _FakeApp()
    init_db(app, config, user_store=fake_store)

    assert app.extensions["db_pool"] is fake_pool
    assert app.extensions["user_store"] is fake_store


def test_init_db_defaults_to_user_store_from_env(config, mocker):
    mocker.patch("gateway.db.create_db_pool", return_value=mocker.MagicMock())
    fake_store = mocker.MagicMock()
    mock_from_env = mocker.patch("gateway.db.UserStore.from_env", return_value=fake_store)

    app = _FakeApp()
    init_db(app, config)

    mock_from_env.assert_called_once_with()
    assert app.extensions["user_store"] is fake_store


# ---------------------------------------------------------------------------
# iso_now()
# ---------------------------------------------------------------------------

def test_iso_now_returns_utc_iso8601_with_trailing_z():
    value = iso_now()
    # e.g. "2026-07-20T12:34:56.789012+00:00Z" -- isoformat() of a
    # pytz.UTC-aware datetime, plus the literal trailing "Z" every JSON
    # response in this package uses.
    assert value.endswith("Z")
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+00:00Z$", value)


# ---------------------------------------------------------------------------
# pooled_cursor(): connection-lifecycle contract shared by
# api/routes.py, dashboard/routes.py, admin/routes.py
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, execute_exception=None):
        self.execute_exception = execute_exception
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if self.execute_exception is not None:
            raise self.execute_exception

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.rollback_called = False

    def cursor(self, cursor_factory=None):
        self.cursor_factory = cursor_factory
        return self._cursor

    def rollback(self):
        self.rollback_called = True


class _FakePool:
    def __init__(self, cursor=None, getconn_exception=None):
        self._cursor = cursor
        self.getconn_exception = getconn_exception
        self.connections = []
        self.putconn_calls = []

    def getconn(self):
        if self.getconn_exception is not None:
            raise self.getconn_exception
        conn = _FakeConnection(self._cursor)
        self.connections.append(conn)
        return conn

    def putconn(self, conn):
        self.putconn_calls.append(conn)


def test_pooled_cursor_yields_a_working_cursor_and_returns_connection_to_pool():
    from psycopg2.extras import RealDictCursor

    cur = _FakeCursor()
    pool = _FakePool(cursor=cur)

    with pooled_cursor(pool) as yielded:
        yielded.execute("SELECT 1")

    assert cur.executed == [("SELECT 1", None)]
    assert pool.connections[0].cursor_factory is RealDictCursor
    assert pool.putconn_calls == pool.connections


def test_pooled_cursor_raises_db_unavailable_when_getconn_fails():
    pool = _FakePool(getconn_exception=RuntimeError("connection refused"))

    with pytest.raises(DBUnavailable, match="connection refused"):
        with pooled_cursor(pool):
            pass

    # No connection was ever obtained, so there's nothing to return.
    assert pool.putconn_calls == []


def test_pooled_cursor_raises_db_unavailable_and_rolls_back_on_query_failure():
    cur = _FakeCursor(execute_exception=RuntimeError("syntax error at or near"))
    pool = _FakePool(cursor=cur)

    with pytest.raises(DBUnavailable, match="syntax error"):
        with pooled_cursor(pool) as yielded:
            yielded.execute("SELECT garbage")

    assert pool.connections[0].rollback_called is True
    # Connection is still returned to the pool even after a mid-query failure.
    assert pool.putconn_calls == pool.connections


def test_pooled_cursor_always_returns_connection_even_when_caller_body_raises():
    cur = _FakeCursor()
    pool = _FakePool(cursor=cur)

    with pytest.raises(DBUnavailable):
        with pooled_cursor(pool):
            raise ValueError("caller-side failure, not a DB error")

    assert pool.putconn_calls == pool.connections
