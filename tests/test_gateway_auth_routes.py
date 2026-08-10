"""
Tests for src/gateway/auth/routes.py (Task 19): the `auth` blueprint
(`GET/POST /login`, `POST /logout`, `GET /api/whoami`) wired through the
real `create_app()` (so ProxyFix/CSRFProtect/CredentialCache/IPThrottle/
session-cookie config are all exactly what production gets), with Postgres/
MQTT mocked exactly as tests/test_gateway_app.py already establishes and a
FakeUserStore swapped onto `app.extensions['user_store']` afterward.

Covers: session cookie round-trip + security flags (HttpOnly/Secure/
SameSite=Lax) + 12h sliding lifetime, CSRF required/rejected/accepted,
`next=` open-redirect rejection, the IP throttle's 429 after 20 failures
(checked BEFORE verify_login is even called), login never revealing
username existence, logout, and /api/whoami's 200/401 + JSON shape.
"""

import hashlib
import os
import re
import sys
from base64 import b64encode
from dataclasses import replace
from email.utils import parsedate_to_datetime
from pathlib import Path

import pytest
from freezegun import freeze_time
from itsdangerous import URLSafeTimedSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.auth.routes import _default_next, _is_safe_next  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from userstore import PrincipalData  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: real create_app() with Postgres/MQTT mocked, FakeUserStore swapped in
# ---------------------------------------------------------------------------

def _principal_data(
    username="alice",
    user_id=1,
    status="active",
    default_client=None,
    roles=frozenset(),
    scopes=None,
    must_change_password=False,
) -> PrincipalData:
    return PrincipalData(
        user_id=user_id,
        username=username,
        status=status,
        default_client=default_client,
        must_change_password=must_change_password,
        roles=roles,
        scopes=scopes or {},
    )


class FakeUserStore:
    """Controllable stand-in for userstore.UserStore -- no DB involved."""

    def __init__(self):
        self._users = {}  # username -> {"password": str, "data": PrincipalData}
        self.login_calls = []
        self.get_principal_calls = []
        self.set_password_calls = []

    def add_user(self, username, password, **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = {"password": password, "data": data}
        return data

    def set_status(self, username, status) -> None:
        entry = self._users[username]
        entry["data"] = replace(entry["data"], status=status)

    def set_user_id(self, username, user_id) -> None:
        entry = self._users[username]
        entry["data"] = replace(entry["data"], user_id=user_id)

    def verify_login(self, username, password, *, source_ip=None):
        self.login_calls.append((username, password, source_ip))
        entry = self._users.get(username)
        if entry is None or entry["password"] != password or entry["data"].status != "active":
            return None
        return entry["data"]

    def get_principal(self, username):
        self.get_principal_calls.append(username)
        entry = self._users.get(username)
        return entry["data"] if entry else None

    def set_password(self, username, password, *, must_change_password=False, actor=None, source_ip=None):
        self.set_password_calls.append((username, password, must_change_password, actor, source_ip))
        entry = self._users[username]
        entry["password"] = password
        entry["data"] = replace(entry["data"], must_change_password=must_change_password)


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


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# CSRF token helper -- constructs a valid signed token directly (see
# flask_wtf.csrf.generate_csrf/validate_csrf) via the test client's
# session_transaction(), rather than always scraping a rendered page. This
# is what lets tests get a valid token even when the user is ALREADY
# authenticated (GET /login redirects instead of rendering a form once
# logged in -- see test_get_login_redirects_when_already_authenticated
# below) -- exactly the situation POST /logout needs a token for, and which
# a real browser gets from Task 24's layout.html meta tag (not built yet).
# ---------------------------------------------------------------------------

def _csrf_token(client, app) -> str:
    session_value = hashlib.sha1(os.urandom(64)).hexdigest()
    with client.session_transaction() as sess:
        sess["csrf_token"] = session_value
    serializer = URLSafeTimedSerializer(app.secret_key, salt="wtf-csrf-token")
    return serializer.dumps(session_value)


CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]*)"')


def _scrape_csrf_token(html: str) -> str:
    match = CSRF_INPUT_RE.search(html)
    assert match, "csrf_token hidden input not found in rendered login page"
    return match.group(1)


def _normalize_html(html: str) -> str:
    """Strip the one field that legitimately differs between two otherwise-identical renders."""
    return CSRF_INPUT_RE.sub('name="csrf_token" value="TOKEN"', html)


def _login(client, app, username, password, next_value=None):
    data = {"csrf_token": _csrf_token(client, app), "username": username, "password": password}
    if next_value is not None:
        data["next"] = next_value
    return client.post("/login", data=data)


# ---------------------------------------------------------------------------
# next= validation -- direct unit tests (no app/request needed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value,expected",
    [
        ("/dashboard", True),
        ("/logs?file=foo", True),
        ("/", True),
        (None, False),
        ("", False),
        ("https://evil.com", False),
        ("http://evil.com", False),
        ("//evil.com", False),
        ("///evil.com", False),
        ("/\\evil.com", False),
        ("relative-no-leading-slash", False),
        ("/safe\r\nSet-Cookie: x=y", False),
        ("/safe\nLocation: https://evil.com", False),
    ],
)
def test_is_safe_next(value, expected):
    assert _is_safe_next(value) is expected


# ---------------------------------------------------------------------------
# _default_next() -- final whole-branch review fix: this used to call
# url_for("dashboard.index"), which always raised BuildError (the real
# endpoint is "dashboard.view_dashboard") and silently relied on the
# except branch's hardcoded "/dashboard" fallback to produce the right
# value. Confirm the `try` branch itself now succeeds -- i.e. `url_for`
# resolves a real, live endpoint -- rather than merely observing that the
# end result still happens to be "/dashboard" either way.
# ---------------------------------------------------------------------------

def test_default_next_resolves_dashboard_view_dashboard_endpoint(app):
    with app.test_request_context("/"):
        assert _default_next() == "/dashboard"


def test_default_next_falls_back_to_hardcoded_path_if_url_for_ever_fails(app, mocker):
    mocker.patch("gateway.auth.routes.url_for", side_effect=Exception("blueprint not registered"))
    with app.test_request_context("/"):
        assert _default_next() == "/dashboard"


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------

def test_get_login_renders_a_working_form_with_real_csrf_token(client, store):
    store.add_user("alice", "hunter2")
    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    token = _scrape_csrf_token(html)
    assert token  # non-empty

    # Prove the scraped token is actually functional end-to-end.
    post_resp = client.post("/login", data={"csrf_token": token, "username": "alice", "password": "hunter2"})
    assert post_resp.status_code == 302


def test_get_login_redirects_when_already_authenticated(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2")
    assert resp.status_code == 302

    resp = client.get("/login")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_get_login_with_next_redirects_there_when_already_authenticated(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")

    resp = client.get("/login?next=/logs")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/logs"


def test_get_login_with_unsafe_next_falls_back_when_already_authenticated(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")

    resp = client.get("/login?next=https://evil.com")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


# ---------------------------------------------------------------------------
# POST /login -- CSRF
# ---------------------------------------------------------------------------

def test_post_login_without_csrf_token_is_rejected(client, store):
    store.add_user("alice", "hunter2")
    resp = client.post("/login", data={"username": "alice", "password": "hunter2"})
    assert resp.status_code == 400


def test_post_login_with_bogus_csrf_token_is_rejected(client, store):
    store.add_user("alice", "hunter2")
    with client.session_transaction() as sess:
        sess["csrf_token"] = "something"
    resp = client.post("/login", data={"csrf_token": "not-a-valid-token", "username": "alice", "password": "hunter2"})
    assert resp.status_code == 400


def test_post_login_with_valid_csrf_token_succeeds(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2")
    assert resp.status_code == 302


# ---------------------------------------------------------------------------
# POST /login -- success / session creation / next= handling
# ---------------------------------------------------------------------------

def test_post_login_success_redirects_to_default_next(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_post_login_success_with_safe_next_redirects_there(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2", next_value="/logs?file=foo")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/logs?file=foo"


def test_post_login_rejects_absolute_url_next(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2", next_value="https://evil.com")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_post_login_rejects_protocol_relative_next(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2", next_value="//evil.com")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_post_login_success_grants_a_working_session(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")

    resp = client.get("/api/whoami")
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "alice"


# ---------------------------------------------------------------------------
# POST /login -- never reveal whether the username existed
# ---------------------------------------------------------------------------

def test_login_failure_identical_for_unknown_user_and_wrong_password(client, store):
    store.add_user("alice", "correct-password")

    resp_unknown = _login(client, client.application, "ghost-user", "whatever")
    resp_wrong = _login(client, client.application, "alice", "wrong-password")

    assert resp_unknown.status_code == resp_wrong.status_code == 200
    assert _normalize_html(resp_unknown.get_data(as_text=True)) == _normalize_html(resp_wrong.get_data(as_text=True))


def test_login_failure_does_not_set_a_session_cookie(client, store):
    store.add_user("alice", "correct-password")
    resp = _login(client, client.application, "alice", "wrong-password")
    assert resp.status_code == 200
    # No principal established -- whoami must still be unauthenticated.
    assert client.get("/api/whoami").status_code == 401


# ---------------------------------------------------------------------------
# POST /login -- IP throttle (20 failures / 10 min / IP), checked before verify_login
# ---------------------------------------------------------------------------

def test_ip_throttle_returns_429_after_20_failed_attempts(client, store):
    store.add_user("alice", "correct-password")

    for _ in range(20):
        resp = _login(client, client.application, "alice", "wrong-password")
        assert resp.status_code == 200

    resp = _login(client, client.application, "alice", "wrong-password")
    assert resp.status_code == 429
    assert len(store.login_calls) == 20  # the 21st attempt never reached verify_login


def test_ip_throttle_does_not_block_a_correct_login_before_threshold(client, store):
    store.add_user("alice", "correct-password")
    for _ in range(19):
        _login(client, client.application, "alice", "wrong-password")

    resp = _login(client, client.application, "alice", "correct-password")
    assert resp.status_code == 302  # 20th total attempt, but a SUCCESS -- not blocked


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------

def test_logout_clears_the_session(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")
    assert client.get("/api/whoami").status_code == 200

    resp = client.post("/logout", data={"csrf_token": _csrf_token(client, client.application)})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"

    assert client.get("/api/whoami").status_code == 401


def test_logout_requires_csrf(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")

    resp = client.post("/logout")
    assert resp.status_code == 400
    # Session must still be intact -- the logout never actually happened.
    assert client.get("/api/whoami").status_code == 200


# ---------------------------------------------------------------------------
# GET /api/whoami
# ---------------------------------------------------------------------------

def test_whoami_401_when_unauthenticated(client, store):
    resp = client.get("/api/whoami")
    assert resp.status_code == 401


def test_whoami_returns_expected_fields(client, store):
    store.add_user(
        "alice",
        "hunter2",
        default_client="revysmc",
        roles=frozenset({"master_admin"}),
        must_change_password=True,
    )
    _login(client, client.application, "alice", "hunter2")

    body = client.get("/api/whoami").get_json()
    assert body == {
        "user_id": 1,
        "username": "alice",
        "auth_method": "session",
        "default_client": "revysmc",
        "is_master_admin": True,
        "must_change_password": True,
    }


# ---------------------------------------------------------------------------
# Session cookie security properties
# ---------------------------------------------------------------------------

def test_session_cookie_has_httponly_secure_samesite_lax(client, store):
    store.add_user("alice", "hunter2")
    resp = _login(client, client.application, "alice", "hunter2")
    set_cookie = resp.headers.get("Set-Cookie")
    assert set_cookie is not None
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=Lax" in set_cookie


def _cookie_expires(set_cookie_header: str):
    for part in set_cookie_header.split(";"):
        part = part.strip()
        if part.startswith("Expires="):
            return parsedate_to_datetime(part[len("Expires="):])
    return None


def test_session_sliding_lifetime_extends_expiry_on_each_authenticated_request(client, store):
    store.add_user("alice", "hunter2")

    with freeze_time("2026-01-01 00:00:00"):
        resp1 = _login(client, client.application, "alice", "hunter2")
        expires1 = _cookie_expires(resp1.headers["Set-Cookie"])
        assert expires1 is not None

    with freeze_time("2026-01-01 01:00:00"):  # 1h later, well within the 12h lifetime
        resp2 = client.get("/api/whoami")
        assert resp2.status_code == 200
        set_cookie2 = resp2.headers.get("Set-Cookie")
        assert set_cookie2 is not None, "session cookie must be re-issued (sliding lifetime)"
        expires2 = _cookie_expires(set_cookie2)

    assert expires2 > expires1


def test_session_still_valid_after_11_hours_still_within_12h_window(client, store):
    store.add_user("alice", "hunter2")
    with freeze_time("2026-01-01 00:00:00"):
        _login(client, client.application, "alice", "hunter2")

    with freeze_time("2026-01-01 11:00:00"):
        assert client.get("/api/whoami").status_code == 200


def test_session_expired_after_12h_of_inactivity(client, store):
    store.add_user("alice", "hunter2")
    with freeze_time("2026-01-01 00:00:00"):
        _login(client, client.application, "alice", "hunter2")

    with freeze_time("2026-01-01 12:00:01"):  # just past the 12h PERMANENT_SESSION_LIFETIME
        assert client.get("/api/whoami").status_code == 401


# ---------------------------------------------------------------------------
# Session re-validates live account state on every request
# ---------------------------------------------------------------------------

def test_disabled_account_session_is_rejected_on_next_request(client, store):
    store.add_user("alice", "hunter2")
    _login(client, client.application, "alice", "hunter2")
    assert client.get("/api/whoami").status_code == 200

    store.set_status("alice", "disabled")

    assert client.get("/api/whoami").status_code == 401


def test_session_rejected_if_user_id_no_longer_matches(client, store):
    store.add_user("alice", "hunter2", user_id=1)
    _login(client, client.application, "alice", "hunter2")
    assert client.get("/api/whoami").status_code == 200

    # Simulate the account being deleted and a new, unrelated account
    # created under the same username (a different id).
    store.set_user_id("alice", 999)

    assert client.get("/api/whoami").status_code == 401


# ---------------------------------------------------------------------------
# GET/POST /change-password (v3.0.0) and the must_change_password gate
#
# The column and its six setters predate this release; until now nothing
# enforced it, so an operator could hand out a password the recipient had no
# way to rotate. These tests pin both halves: the route itself, and the
# `before_request` gate in gateway/__init__.py that makes the flag mean
# something -- including the part that must NOT happen (Basic/Bearer callers
# are never redirected to an HTML form).
# ---------------------------------------------------------------------------

def _change_password(client, app, current, new, confirm=None):
    return client.post(
        "/change-password",
        data={
            "csrf_token": _csrf_token(client, app),
            "current_password": current,
            "new_password": new,
            "confirm_password": new if confirm is None else confirm,
        },
    )


def test_get_change_password_redirects_to_login_when_anonymous(client):
    response = client.get("/change-password")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_get_change_password_renders_form_when_authenticated(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = client.get("/change-password")

    assert response.status_code == 200
    assert b"Current password" in response.data
    assert b"csrf_token" in response.data


def test_change_password_happy_path_sets_password_and_clears_flag(client, app, store):
    store.add_user("alice", "correct-password", must_change_password=True)
    _login(client, app, "alice", "correct-password")

    response = _change_password(client, app, "correct-password", "brand-new-password")

    assert response.status_code == 302
    assert "/dashboard" in response.headers["Location"]
    assert store.set_password_calls == [
        ("alice", "brand-new-password", False, "alice", "127.0.0.1"),
    ]


def test_change_password_rejects_wrong_current_password(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = _change_password(client, app, "not-my-password", "brand-new-password")

    assert response.status_code == 200
    assert b"Current password is incorrect" in response.data
    assert store.set_password_calls == []


def test_change_password_rejects_mismatched_confirmation(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = _change_password(client, app, "correct-password", "brand-new-password", confirm="something-else")

    assert response.status_code == 200
    assert b"do not match" in response.data
    assert store.set_password_calls == []


def test_change_password_rejects_too_short_password(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = _change_password(client, app, "correct-password", "short")

    assert response.status_code == 200
    assert b"at least 12 characters" in response.data
    assert store.set_password_calls == []


def test_change_password_rejects_unchanged_password(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = _change_password(client, app, "correct-password", "correct-password")

    assert response.status_code == 200
    assert b"must be different" in response.data
    assert store.set_password_calls == []


def test_change_password_requires_csrf_token(client, app, store):
    store.add_user("alice", "correct-password")
    _login(client, app, "alice", "correct-password")

    response = client.post(
        "/change-password",
        data={
            "current_password": "correct-password",
            "new_password": "brand-new-password",
            "confirm_password": "brand-new-password",
        },
    )

    assert response.status_code == 400
    assert store.set_password_calls == []


def test_gate_redirects_flagged_session_away_from_other_pages(client, app, store):
    store.add_user("alice", "correct-password", must_change_password=True)
    _login(client, app, "alice", "correct-password")

    response = client.get("/dashboard")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/change-password")


def test_gate_allows_flagged_session_to_reach_change_password_and_logout(client, app, store):
    store.add_user("alice", "correct-password", must_change_password=True)
    _login(client, app, "alice", "correct-password")

    assert client.get("/change-password").status_code == 200

    response = client.post("/logout", data={"csrf_token": _csrf_token(client, app)})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_gate_leaves_unflagged_session_alone(client, app, store):
    store.add_user("alice", "correct-password", must_change_password=False)
    _login(client, app, "alice", "correct-password")

    assert client.get("/dashboard").status_code == 200


def test_gate_never_redirects_basic_auth_callers(client, store):
    """
    A machine account carrying the flag must keep working over Basic auth:
    it cannot use an HTML form, and its credential is rotated by an operator
    via tools/user-manager.py. `/health` is unauthenticated, so `/test` (an
    API_ONLY route) is the honest probe here.
    """
    store.add_user("node-1", "node-password", must_change_password=True)

    response = client.post(
        "/test",
        headers={"Authorization": "Basic " + b64encode(b"node-1:node-password").decode()},
    )

    assert response.status_code != 302
