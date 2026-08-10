"""
Tests for src/gateway/logs/routes.py (Task 21): the `logs` blueprint
(`GET /logs`, `GET /api/logs/list`, `GET /api/logs/tail`,
`GET /api/logs/chunk`), wired through the real `create_app()` (so
ProxyFix/CSRFProtect/blueprint registration are exactly what production
gets), with Postgres/MQTT mocked exactly as tests/test_gateway_app.py
establishes, a FakeUserStore swapped onto app.extensions afterward, and
`gateway.logs.files.LOG_DIR` monkeypatched to a real temp directory so
these tests exercise the actual filesystem (no live Postgres/MQTT
available in this environment, and per project rule, docker/docker-compose
is never invoked from automation -- but this blueprint's read path is
purely filesystem-based, so it's fully testable for real here).

Covers:
- `GET /logs`: exists, session-OR-Basic/Bearer auth, anonymous browser
  request redirects (still, per this blueprint's HTML-page policy).
- `GET /api/logs/list`: ported-unchanged behavior (recursive listing,
  authorization filtering, ops/ hidden unless include_ops + master_admin),
  and the centralized `_auth_failure_response` fix: an anonymous JSON
  request gets 401, NOT a redirect.
- `GET /api/logs/tail`: `?stat=1`, forward reads, 400/403/404 paths.
- `GET /api/logs/chunk`: backward reads, 400/403/404 paths.
"""

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.logs import files as logfiles  # noqa: E402
from userstore import PrincipalData  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: real create_app() with Postgres/MQTT mocked, fakes swapped in,
# LOG_DIR pointed at a real temp directory.
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
    """Controllable stand-in for userstore.UserStore -- no DB involved."""

    def __init__(self):
        self._users = {}
        self.login_calls = []

    def add_user(self, username, password, **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = {"password": password, "data": data}
        return data

    def verify_login(self, username, password, *, source_ip=None):
        self.login_calls.append((username, password, source_ip))
        entry = self._users.get(username)
        if entry is None or entry["password"] != password or entry["data"].status != "active":
            return None
        return entry["data"]

    def verify_token(self, token):
        return None


class FakeMqttPublisher:
    def __init__(self, connected=True):
        self._connected = connected

    def is_connected(self):
        return self._connected

    def forward_to_mqtt(self, alert_data):
        return True


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


@pytest.fixture
def log_dir(tmp_path, mocker):
    mocker.patch.object(logfiles, "LOG_DIR", str(tmp_path))
    return tmp_path


def _basic(username, password):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _write_log(log_dir, relpath, content=b"line1\nline2\n"):
    path = log_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# GET /logs
# ---------------------------------------------------------------------------

def test_view_logs_redirects_anonymous_browser_request(client):
    resp = client.get("/logs")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_view_logs_renders_for_basic_authenticated_user(client, store, log_dir):
    store.add_user("alice", "hunter2")
    resp = client.get("/logs", headers=_basic("alice", "hunter2"))
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    assert "Alert Manager - Logs" in resp.get_data(as_text=True)


def test_view_logs_401_when_wrong_basic_credentials(client, store):
    store.add_user("alice", "hunter2")
    resp = client.get("/logs", headers=_basic("alice", "wrong"))
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/logs/list
# ---------------------------------------------------------------------------

def test_list_log_files_401_json_not_redirect_for_anonymous_request(client, log_dir):
    # The centralized principal.py fix: /api/* paths never get the
    # HTML-redirect treatment, even though "session" is an allowed method
    # for this blueprint and no credentials were presented at all.
    resp = client.get("/api/logs/list")
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="Alert Gateway"'
    # Critically: NOT a redirect.
    assert "Location" not in resp.headers


def test_list_log_files_returns_only_files_the_scoped_user_may_read(client, store, log_dir):
    _write_log(log_dir, "ops/alert-router.2026-07-20.log")
    _write_log(log_dir, "file/revysmc/2026-07-20.log")
    _write_log(log_dir, "file/some-other-client/2026-07-20.log")
    _write_log(log_dir, "file/2026-07-20.log")

    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get("/api/logs/list", headers=_basic("viewer", "secret"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["files"] == ["file/revysmc/2026-07-20.log"]
    assert body["user"]["username"] == "viewer"
    assert body["user"]["is_master_admin"] is False


def test_list_log_files_master_admin_sees_everything_except_ops_by_default(client, store, log_dir):
    _write_log(log_dir, "ops/alert-router.2026-07-20.log")
    _write_log(log_dir, "file/revysmc/2026-07-20.log")
    _write_log(log_dir, "file/2026-07-20.log")

    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/list", headers=_basic("root", "secret"))
    body = resp.get_json()
    assert set(body["files"]) == {"file/revysmc/2026-07-20.log", "file/2026-07-20.log"}


def test_list_log_files_master_admin_with_include_ops_sees_ops_too(client, store, log_dir):
    _write_log(log_dir, "ops/alert-router.2026-07-20.log")
    _write_log(log_dir, "file/2026-07-20.log")

    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/list?include_ops=true", headers=_basic("root", "secret"))
    body = resp.get_json()
    assert set(body["files"]) == {"ops/alert-router.2026-07-20.log", "file/2026-07-20.log"}


def test_list_log_files_non_admin_include_ops_is_ignored(client, store, log_dir):
    _write_log(log_dir, "ops/alert-router.2026-07-20.log")
    _write_log(log_dir, "file/revysmc/2026-07-20.log")

    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get("/api/logs/list?include_ops=true", headers=_basic("viewer", "secret"))
    body = resp.get_json()
    assert body["files"] == ["file/revysmc/2026-07-20.log"]


def test_list_log_files_404_when_log_dir_missing(client, store, tmp_path, mocker):
    mocker.patch.object(logfiles, "LOG_DIR", str(tmp_path / "does-not-exist"))
    store.add_user("viewer", "secret")
    resp = client.get("/api/logs/list", headers=_basic("viewer", "secret"))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/logs/tail
# ---------------------------------------------------------------------------

def test_tail_log_401_json_not_redirect_for_anonymous_request(client, log_dir):
    resp = client.get("/api/logs/tail?file=file/revysmc/x.log")
    assert resp.status_code == 401
    assert "Location" not in resp.headers


def test_tail_log_stat_returns_file_size_only(client, store, log_dir):
    _write_log(log_dir, "file/revysmc/x.log", b"line1\nline2\n")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get("/api/logs/tail?file=file/revysmc/x.log&stat=1", headers=_basic("viewer", "secret"))
    assert resp.status_code == 200
    assert resp.get_json() == {"file_size": len(b"line1\nline2\n")}


def test_tail_log_forward_read_from_position(client, store, log_dir):
    _write_log(log_dir, "file/revysmc/x.log", b"line1\nline2\nline3\n")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get("/api/logs/tail?file=file/revysmc/x.log&position=6", headers=_basic("viewer", "secret"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert body["lines"] == ["line2", "line3"]
    assert body["position"] == len(b"line1\nline2\nline3\n")
    assert body["eof"] is True


def test_tail_log_403_for_file_outside_scope(client, store, log_dir):
    _write_log(log_dir, "file/some-other-client/x.log")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get(
        "/api/logs/tail?file=file/some-other-client/x.log", headers=_basic("viewer", "secret")
    )
    assert resp.status_code == 403


def test_tail_log_403_for_ops_file_without_master_admin(client, store, log_dir):
    _write_log(log_dir, "ops/alert-router.log")
    store.add_user("viewer", "secret", scopes={"*": frozenset({"logs:read"})})
    resp = client.get("/api/logs/tail?file=ops/alert-router.log", headers=_basic("viewer", "secret"))
    assert resp.status_code == 403


def test_tail_log_404_for_missing_file(client, store, log_dir):
    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/tail?file=file/revysmc/missing.log", headers=_basic("root", "secret"))
    assert resp.status_code == 404


def test_tail_log_400_for_directory_traversal(client, store, log_dir):
    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/tail?file=../etc/passwd", headers=_basic("root", "secret"))
    assert resp.status_code == 400


def test_tail_log_substitutes_date_placeholder(client, store, log_dir):
    today = logfiles.substitute_date("{date}")
    _write_log(log_dir, f"file/revysmc/{today}.log", b"hello\n")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get(
        "/api/logs/tail?file=file/revysmc/{date}.log&stat=1", headers=_basic("viewer", "secret")
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"file_size": len(b"hello\n")}


# ---------------------------------------------------------------------------
# GET /api/logs/chunk
# ---------------------------------------------------------------------------

def test_chunk_log_401_json_not_redirect_for_anonymous_request(client, log_dir):
    resp = client.get("/api/logs/chunk?file=file/revysmc/x.log")
    assert resp.status_code == 401
    assert "Location" not in resp.headers


def test_chunk_log_requires_file_parameter(client, store, log_dir):
    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/chunk", headers=_basic("root", "secret"))
    assert resp.status_code == 400


def test_chunk_log_defaults_end_to_current_file_size(client, store, log_dir):
    _write_log(log_dir, "file/revysmc/x.log", b"line1\nline2\nline3\n")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get("/api/logs/chunk?file=file/revysmc/x.log", headers=_basic("viewer", "secret"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lines"] == ["line1", "line2", "line3"]
    assert body["start"] == 0
    assert body["end"] == len(b"line1\nline2\nline3\n")
    assert body["eof"] is True


def test_chunk_log_explicit_end_reads_backward_from_that_offset(client, store, log_dir):
    content = b"line1\nline2\nline3\n"
    _write_log(log_dir, "file/revysmc/x.log", content)
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    end = len(b"line1\nline2\n")
    resp = client.get(
        f"/api/logs/chunk?file=file/revysmc/x.log&end={end}", headers=_basic("viewer", "secret")
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["lines"] == ["line1", "line2"]
    assert body["start"] == 0
    assert body["end"] == end
    assert body["eof"] is True


def test_chunk_log_403_for_file_outside_scope(client, store, log_dir):
    _write_log(log_dir, "file/some-other-client/x.log")
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    resp = client.get(
        "/api/logs/chunk?file=file/some-other-client/x.log", headers=_basic("viewer", "secret")
    )
    assert resp.status_code == 403


def test_chunk_log_404_for_missing_file(client, store, log_dir):
    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/chunk?file=file/revysmc/missing.log", headers=_basic("root", "secret"))
    assert resp.status_code == 404


def test_chunk_log_400_for_directory_traversal(client, store, log_dir):
    store.add_user("root", "secret", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})})
    resp = client.get("/api/logs/chunk?file=../etc/passwd", headers=_basic("root", "secret"))
    assert resp.status_code == 400


def test_chunk_then_tail_compose_end_to_end_through_the_real_routes(client, store, log_dir):
    # End-to-end proof (through the HTTP layer, not just files.py directly)
    # that chunk's `start` and tail's `position` compose without gap/overlap.
    content = b"".join(f"row-{i}\n".encode() for i in range(5))
    _write_log(log_dir, "file/revysmc/x.log", content)
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})
    headers = _basic("viewer", "secret")

    chunk_resp = client.get("/api/logs/chunk?file=file/revysmc/x.log", headers=headers).get_json()
    assert chunk_resp["eof"] is True
    assert chunk_resp["lines"] == [f"row-{i}" for i in range(5)]

    # Append new content, then tail forward from the chunk's own `end`
    # (== file_size at that point) -- must pick up exactly the new lines.
    with open(log_dir / "file/revysmc/x.log", "ab") as f:
        f.write(b"row-5\n")

    tail_resp = client.get(
        f"/api/logs/tail?file=file/revysmc/x.log&position={chunk_resp['end']}", headers=headers
    ).get_json()
    assert tail_resp["lines"] == ["row-5"]
    assert tail_resp["eof"] is True


def test_chunk_log_413_when_a_single_entry_exceeds_the_widen_ceiling(client, store, log_dir, mocker):
    # Review finding: read_chunk_backward's widen fallback must stay
    # bounded (files.MAX_WIDEN_BYTES) rather than reading an entire large
    # file into memory in one request. Ceiling constants patched down
    # (same mechanism, smaller scale) purely for test speed -- the log
    # file itself is real, written to log_dir, not mocked.
    mocker.patch.object(logfiles, "MAX_CHUNK_BYTES", 10_000)
    mocker.patch.object(logfiles, "MAX_WIDEN_BYTES", 100_000)

    content = b"z" * 500_000  # one giant "line", no '\n' anywhere, 5x the patched ceiling
    _write_log(log_dir, "file/revysmc/huge.log", content)
    store.add_user("viewer", "secret", scopes={"revysmc": frozenset({"logs:read"})})

    resp = client.get("/api/logs/chunk?file=file/revysmc/huge.log", headers=_basic("viewer", "secret"))
    assert resp.status_code == 413
    body = resp.get_json()
    assert body["status"] == "error"
    assert body["message"] == "Log entry too large to read in one chunk"
