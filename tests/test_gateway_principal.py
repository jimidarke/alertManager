"""
Tests for src/gateway/principal.py (Task 18): Principal, resolve_principal()
and its per-method resolvers, and the require_auth/require_permission/
require_master_admin decorators.

No live Postgres/MQTT is available in this environment -- UserStore is a
fake (a plain object with controllable verify_login()/verify_token()
methods) wired onto app.extensions['user_store'], exactly the seam
gateway/db.py wires a real UserStore onto in production.
"""

import sys
from pathlib import Path

import pytest
from flask import Blueprint, Flask, g, request
from werkzeug.exceptions import Forbidden

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from userstore import PrincipalData  # noqa: E402
import gateway.principal as gp  # noqa: E402
from gateway.principal import (  # noqa: E402
    API_ONLY,
    SESSION_ONLY,
    SESSION_OR_API,
    Principal,
    require_auth,
    require_master_admin,
    require_permission,
    resolve_principal,
)


def _principal_data(
    username="alice",
    default_client=None,
    roles=frozenset(),
    scopes=None,
) -> PrincipalData:
    return PrincipalData(
        user_id=1,
        username=username,
        status="active",
        default_client=default_client,
        must_change_password=False,
        roles=roles,
        scopes=scopes or {},
    )


class FakeUserStore:
    """Controllable stand-in for userstore.UserStore -- no DB involved."""

    def __init__(self, login_result=None, token_result=None):
        self.login_result = login_result
        self.token_result = token_result
        self.login_calls = []
        self.token_calls = []

    def verify_login(self, username, password, *, source_ip=None):
        self.login_calls.append((username, password, source_ip))
        return self.login_result

    def verify_token(self, token):
        self.token_calls.append(token)
        return self.token_result


@pytest.fixture
def app():
    flask_app = Flask(__name__)
    flask_app.config["SECRET_KEY"] = "test-secret-key-thats-long-enough"
    return flask_app


def _install_store(app, store):
    app.extensions["user_store"] = store


# ---------------------------------------------------------------------------
# Principal: forwards PrincipalData accessors
# ---------------------------------------------------------------------------

def test_principal_forwards_principal_data_accessors():
    data = _principal_data(username="bob", default_client="revysmc", roles=frozenset({"master_admin"}))
    principal = Principal(data, auth_method="basic")

    assert principal.username == "bob"
    assert principal.default_client == "revysmc"
    assert principal.is_master_admin() is True
    assert principal.auth_method == "basic"
    assert principal.user_id == 1
    assert principal.must_change_password is False


def test_principal_has_permission_delegates_to_data():
    data = _principal_data(scopes={"revysmc": frozenset({"logs:read"})})
    principal = Principal(data, auth_method="bearer")

    assert principal.has_permission("logs:read", "revysmc") is True
    assert principal.has_permission("logs:read", "other-client") is False


# ---------------------------------------------------------------------------
# resolve_principal(): dispatch + precedence
# ---------------------------------------------------------------------------

def test_resolve_principal_raises_on_unknown_method(app):
    with app.test_request_context("/x"):
        with pytest.raises(ValueError, match="unknown auth method"):
            resolve_principal(("carrier-pigeon",))


def test_resolve_principal_returns_none_when_nothing_resolves(app):
    _install_store(app, FakeUserStore(login_result=None, token_result=None))
    with app.test_request_context("/x"):
        assert resolve_principal(SESSION_OR_API) is None


def test_resolve_session_returns_none_without_a_session_cookie(app):
    # Task 19 implemented _resolve_session for real; with no session cookie
    # present at all (this request context never had one set), it must
    # still return None without needing app.extensions['user_store'] to be
    # installed -- real cookie-verification tests for the populated-session
    # case live in tests/test_gateway_auth_routes.py, exercised end-to-end
    # through the auth blueprint's actual /login route.
    with app.test_request_context("/x"):
        assert gp._resolve_session() is None


def test_resolve_principal_basic_resolves_via_verify_login(app):
    data = _principal_data(username="alice")
    _install_store(app, FakeUserStore(login_result=data))
    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        principal = resolve_principal(API_ONLY)
        assert principal is not None
        assert principal.username == "alice"
        assert principal.auth_method == "basic"


def test_resolve_principal_bearer_resolves_via_verify_token(app):
    data = _principal_data(username="node1")
    _install_store(app, FakeUserStore(token_result=data))
    with app.test_request_context("/x", headers={"Authorization": "Bearer amt_sometoken"}):
        principal = resolve_principal(API_ONLY)
        assert principal is not None
        assert principal.username == "node1"
        assert principal.auth_method == "bearer"


def test_resolve_principal_basic_precedes_bearer_when_only_basic_header_present(app):
    # There's only ever one Authorization header, so this really just
    # confirms basic is tried before bearer without erroring out when a
    # bearer resolution would also (hypothetically) be attempted.
    data = _principal_data(username="alice")
    store = FakeUserStore(login_result=data)
    _install_store(app, store)
    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        principal = resolve_principal(("basic", "bearer"))
        assert principal.auth_method == "basic"
    assert store.token_calls == []  # bearer resolver never even tried (basic already matched)


def test_resolve_principal_session_takes_precedence_over_basic(app, mocker):
    # Simulate "Task 19 has landed" by making _resolve_session succeed;
    # confirm session wins over a simultaneously-valid Basic header, per
    # the documented precedence (session -> basic -> bearer).
    session_data = _principal_data(username="session-user")
    mocker.patch.object(gp, "_resolve_session", return_value=Principal(session_data, "session"))

    basic_data = _principal_data(username="basic-user")
    _install_store(app, FakeUserStore(login_result=basic_data))

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        principal = resolve_principal(SESSION_OR_API)
        assert principal.username == "session-user"
        assert principal.auth_method == "session"


def test_resolve_principal_falls_through_to_basic_when_session_resolver_returns_none(app, mocker):
    mocker.patch.object(gp, "_resolve_session", return_value=None)
    data = _principal_data(username="alice")
    _install_store(app, FakeUserStore(login_result=data))

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        principal = resolve_principal(SESSION_OR_API)
        assert principal.username == "alice"
        assert principal.auth_method == "basic"


def test_resolve_principal_stops_dispatch_that_only_includes_session(app, mocker):
    # methods=SESSION_ONLY must never even look at the Authorization header.
    mocker.patch.object(gp, "_resolve_basic", side_effect=AssertionError("must not be called"))
    mocker.patch.object(gp, "_resolve_bearer", side_effect=AssertionError("must not be called"))
    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert resolve_principal(SESSION_ONLY) is None


# ---------------------------------------------------------------------------
# require_auth
# ---------------------------------------------------------------------------

def test_require_auth_bare_usage_sets_principal_and_calls_view(app):
    data = _principal_data(username="alice")
    _install_store(app, FakeUserStore(login_result=data))

    @require_auth
    def view():
        return f"hello {g.principal.username}"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert view() == "hello alice"


def test_require_auth_factory_usage_with_explicit_methods(app):
    data = _principal_data(username="alice")
    _install_store(app, FakeUserStore(login_result=data))

    @require_auth(methods=API_ONLY)
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert view() == "ok"


def test_require_auth_401_when_authorization_header_present_but_invalid(app):
    _install_store(app, FakeUserStore(login_result=None))

    @require_auth
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6d3Jvbmc="}):
        response = view()
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == 'Basic realm="Alert Gateway"'


def test_require_auth_redirects_to_login_when_no_credentials_and_session_allowed(app):
    _install_store(app, FakeUserStore())

    @require_auth
    def view():
        return "ok"

    with app.test_request_context("/logs?file=foo"):
        response = view()
        assert response.status_code == 302
        assert response.headers["Location"].startswith("/login?next=")
        assert "file" in response.headers["Location"]  # next carries the query string


def test_require_auth_401_when_no_credentials_and_session_not_allowed(app):
    _install_store(app, FakeUserStore())

    @require_auth(methods=API_ONLY)
    def view():
        return "ok"

    with app.test_request_context("/alert"):
        response = view()
        assert response.status_code == 401


def test_require_auth_redirect_uses_registered_login_endpoint_when_present(app):
    _install_store(app, FakeUserStore())
    auth_bp = Blueprint("auth", __name__)

    @auth_bp.route("/login")
    def login():
        return "login page"

    app.register_blueprint(auth_bp)

    @require_auth
    def view():
        return "ok"

    with app.test_request_context("/dashboard"):
        response = view()
        assert response.status_code == 302
        assert response.headers["Location"] == "/login?next=/dashboard"


# ---------------------------------------------------------------------------
# require_permission
# ---------------------------------------------------------------------------

def test_require_permission_allows_when_scope_matches(app):
    data = _principal_data(username="alice", scopes={"revysmc": frozenset({"logs:read"})})
    _install_store(app, FakeUserStore(login_result=data))

    @require_permission("logs:read", get_client=lambda: "revysmc")
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert view() == "ok"


def test_require_permission_403_when_scope_does_not_match(app):
    data = _principal_data(username="alice", scopes={"revysmc": frozenset({"logs:read"})})
    _install_store(app, FakeUserStore(login_result=data))

    @require_permission("logs:read", get_client=lambda: "some-other-client")
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        with pytest.raises(Forbidden):
            view()


def test_require_permission_401_when_authentication_itself_fails(app):
    _install_store(app, FakeUserStore(login_result=None))

    @require_permission("logs:read", get_client=lambda: "revysmc")
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6d3Jvbmc="}):
        response = view()
        assert response.status_code == 401


def test_require_permission_normalizes_get_client_result(app):
    data = _principal_data(username="alice", scopes={"revysmc": frozenset({"logs:read"})})
    _install_store(app, FakeUserStore(login_result=data))

    @require_permission("logs:read", get_client=lambda: "  RevySMC  ")
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert view() == "ok"


def test_require_permission_defaults_to_empty_client_when_get_client_omitted(app):
    data = _principal_data(username="alice", scopes={"": frozenset({"alerts:write"})})
    _install_store(app, FakeUserStore(login_result=data))

    @require_permission("alerts:write")
    def view():
        return "ok"

    with app.test_request_context("/x", headers={"Authorization": "Basic YWxpY2U6c2VjcmV0"}):
        assert view() == "ok"


def test_require_permission_body_based_client_matches_post_alert_rule(app):
    # POST /alert's exact rule: payload 'client' else principal.default_client else ''.
    data = _principal_data(username="node1", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    _install_store(app, FakeUserStore(login_result=data))

    def get_client():
        payload = request.get_json(silent=True) or {}
        return payload.get("client") or g.principal.default_client or ""

    @require_permission("alerts:write", get_client=get_client)
    def view():
        return "ok"

    with app.test_request_context(
        "/alert", method="POST", json={}, headers={"Authorization": "Basic bm9kZTE6c2VjcmV0"}
    ):
        assert view() == "ok"  # falls back to principal.default_client


# ---------------------------------------------------------------------------
# require_master_admin
# ---------------------------------------------------------------------------

def test_require_master_admin_allows_master_admin(app, mocker):
    data = _principal_data(username="jimi", roles=frozenset({"master_admin"}))
    mocker.patch.object(gp, "_resolve_session", return_value=Principal(data, "session"))

    @require_master_admin
    def view():
        return "ok"

    with app.test_request_context("/admin/users"):
        assert view() == "ok"


def test_require_master_admin_403_for_non_admin_session(app, mocker):
    data = _principal_data(username="alice", roles=frozenset())
    mocker.patch.object(gp, "_resolve_session", return_value=Principal(data, "session"))

    @require_master_admin
    def view():
        return "ok"

    with app.test_request_context("/admin/users"):
        with pytest.raises(Forbidden):
            view()


def test_require_master_admin_redirects_when_no_session(app):
    _install_store(app, FakeUserStore())

    @require_master_admin
    def view():
        return "ok"

    with app.test_request_context("/admin/users"):
        response = view()
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]


def test_require_master_admin_ignores_basic_bearer_headers(app, mocker):
    # Admin is session-only -- a valid Basic/Bearer credential must not
    # substitute for a session, even if that account happens to be master_admin.
    admin_data = _principal_data(username="jimi", roles=frozenset({"master_admin"}))
    _install_store(app, FakeUserStore(login_result=admin_data))
    mocker.patch.object(gp, "_resolve_session", return_value=None)

    @require_master_admin
    def view():
        return "ok"

    with app.test_request_context("/admin/users", headers={"Authorization": "Basic amltaTpzZWNyZXQ="}):
        response = view()
        # No Authorization-aware redirect suppression should kick in here
        # because require_master_admin only ever passes methods=SESSION_ONLY
        # to require_auth, so Basic is never even attempted -- but since an
        # Authorization header IS present, _auth_failure_response reports 401
        # (matches require_auth's own documented behavior), not a redirect.
        assert response.status_code == 401
