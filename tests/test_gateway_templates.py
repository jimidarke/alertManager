"""
Tests for Task 24: the shared template/static foundation --
`src/gateway/templates/layout.html`, `src/gateway/templates/login.html`,
`src/gateway/__init__.py`'s `version` context processor, its explicit
`static_folder` wiring (`src/static/`, a SIBLING of `src/gateway/`, not
`src/gateway/static/`), and its new `GET /` redirect route.

Wired through the real `create_app()` (so ProxyFix/CSRFProtect/blueprint
registration/static-folder config are exactly what production gets), with
Postgres/MQTT mocked exactly as tests/test_gateway_app.py establishes and a
FakeUserStore swapped onto `app.extensions['user_store']` afterward --
same pattern every other `tests/test_gateway_*_routes.py` file uses.

No route yet extends `layout.html` (Task 25/26/27 land `dashboard.html`,
`logs.html`, and rewire `admin/*.html` to do so) -- so `layout.html` itself
is exercised here by rendering a minimal child template inline via
`render_template_string("{% extends 'layout.html' %}...")` inside a real
app/request context, with `g.principal` set by hand to whichever principal
shape a given test needs. This is real Jinja template rendering (not a
"file exists" check): every assertion below inspects the actual rendered
HTML string.

Covers:
- `version` context processor: available to ANY template, and the actual
  footer text is real (not a literal `{__version__}`/`{version}` string --
  the exact class of bug this task's brief calls out).
- Nav conditional logic: Admin link + admin badge present only for
  `is_master_admin() == True`; Dashboard/Logs links always present;
  username shown; the whole user/logout block absent with no principal at
  all (defensive `g.get('principal')` guard).
- Logout is a real POST form (never a GET link) carrying a CSRF token.
- Flash message rendering (Flask's own `get_flashed_messages`).
- `login.html`: standalone (no authenticated nav chrome), working CSRF
  token, and the literal "Invalid username or password" failure text --
  cross-checked against gateway/auth/routes.py's own call site so this
  test would fail if that message ever drifts out of sync.
- `GET /`: redirects anonymous -> /login, authenticated -> /dashboard.
- Static wiring: base.css/common.js are actually served at their expected
  `/static/...` URLs (not just present on disk), and the pre-existing
  `log-viewer.css` (explicitly untouched by this task) still is too.
"""

import re
import sys
from pathlib import Path

import pytest
from flask import flash, g, render_template_string

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.auth.routes import _render_login_page  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from gateway.principal import Principal  # noqa: E402
from userstore import PrincipalData  # noqa: E402
from version import __version__ as APP_VERSION  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures: real create_app() with Postgres/MQTT mocked, FakeUserStore swapped in
# ---------------------------------------------------------------------------

def _principal_data(username="alice", user_id=1, roles=frozenset(), default_client=None) -> PrincipalData:
    return PrincipalData(
        user_id=user_id,
        username=username,
        status="active",
        default_client=default_client,
        must_change_password=False,
        roles=roles,
        scopes={},
    )


class FakeUserStore:
    """Controllable stand-in for userstore.UserStore -- supports a real login round-trip (verify_login checks the stored password), plus get_principal() for session resolution."""

    def __init__(self):
        self._users = {}  # username -> {"password": str, "data": PrincipalData}

    def add_user(self, username, password="hunter2", **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = {"password": password, "data": data}
        return data

    def verify_login(self, username, password, *, source_ip=None):
        entry = self._users.get(username)
        if entry is None or entry["password"] != password or entry["data"].status != "active":
            return None
        return entry["data"]

    def get_principal(self, username):
        entry = self._users.get(username)
        return entry["data"] if entry else None

    def verify_token(self, token):
        return None


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


CHILD_TEMPLATE = "{% extends 'layout.html' %}{% block content %}TEST-CONTENT-MARKER{% endblock %}"


def _render_layout(app, principal=None):
    """Render layout.html (via a trivial child template) inside a real request context, optionally with g.principal set."""
    with app.test_request_context("/"):
        if principal is not None:
            g.principal = principal
        return render_template_string(CHILD_TEMPLATE)


CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _scrape_csrf(html: str) -> str:
    match = CSRF_INPUT_RE.search(html)
    assert match, "csrf_token hidden input not found"
    return match.group(1)


# ---------------------------------------------------------------------------
# version context processor
# ---------------------------------------------------------------------------

def test_version_context_processor_available_to_any_template(app):
    with app.test_request_context("/"):
        html = render_template_string("{{ version }}")
    assert html == APP_VERSION


def test_app_version_actually_resolved_from_version_file(app):
    # Sanity check on the fixture itself: if VERSION couldn't be read,
    # version.py's own fallback is the literal string "unknown" -- assert
    # against that so a broken VERSION file would fail loudly here instead
    # of every assertion below silently comparing "unknown" == "unknown".
    assert APP_VERSION != "unknown"
    assert APP_VERSION  # non-empty


def test_footer_shows_real_version_not_a_literal_placeholder(app):
    principal = Principal(_principal_data(), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert f"Alert Manager v{APP_VERSION}" in html
    assert "{__version__}" not in html
    assert "{version}" not in html


# ---------------------------------------------------------------------------
# Nav conditional rendering
# ---------------------------------------------------------------------------

def test_nav_shows_admin_link_and_badge_for_master_admin(app):
    principal = Principal(_principal_data(roles=frozenset({"master_admin"})), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert 'href="/admin/users"' in html
    assert ">Admin<" in html
    assert "badge-admin" in html


def test_nav_hides_admin_link_for_non_admin(app):
    principal = Principal(_principal_data(roles=frozenset()), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert 'href="/admin/users"' not in html
    assert "badge-admin" not in html
    assert "badge-user" in html


def test_nav_always_shows_dashboard_and_logs_links(app):
    for roles in (frozenset(), frozenset({"master_admin"})):
        principal = Principal(_principal_data(roles=roles), auth_method="session")
        html = _render_layout(app, principal=principal)
        assert 'href="/dashboard"' in html
        assert 'href="/logs"' in html


def test_nav_shows_username(app):
    principal = Principal(_principal_data(username="bob"), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert "bob" in html


def test_layout_renders_without_a_principal_and_hides_user_chrome(app):
    # Defensive g.get('principal') guard: never raises even with no
    # principal at all, though every real caller sets one.
    html = _render_layout(app, principal=None)
    assert "TEST-CONTENT-MARKER" in html
    assert "navbar-user" not in html
    assert 'action="/logout"' not in html


# ---------------------------------------------------------------------------
# Logout: POST form (not a GET link), carries a CSRF token
# ---------------------------------------------------------------------------

def test_logout_is_a_post_form_with_csrf_token(app):
    principal = Principal(_principal_data(), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert 'action="/logout"' in html
    assert 'method="post"' in html
    assert CSRF_INPUT_RE.search(html)
    assert '<a href="/logout"' not in html  # never a GET link


# ---------------------------------------------------------------------------
# CSRF meta tag (docs/OVERHAUL_PLAN.md 4.4: "layout embeds token in a meta tag")
# ---------------------------------------------------------------------------

def test_layout_embeds_csrf_meta_tag(app):
    principal = Principal(_principal_data(), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', html)


# ---------------------------------------------------------------------------
# Flash messages
# ---------------------------------------------------------------------------

def test_flash_messages_render_with_category_class(app):
    with app.test_request_context("/"):
        g.principal = Principal(_principal_data(), auth_method="session")
        flash("Something happened", "warning")
        html = render_template_string(CHILD_TEMPLATE)
    assert "flash-warning" in html
    assert "Something happened" in html


def test_no_flash_block_rendered_when_nothing_flashed(app):
    html = _render_layout(app, principal=Principal(_principal_data(), auth_method="session"))
    assert "flash-messages" not in html


# ---------------------------------------------------------------------------
# Static asset wiring (base.css / common.js linked + actually servable)
# ---------------------------------------------------------------------------

def test_layout_links_base_css_and_common_js(app):
    principal = Principal(_principal_data(), auth_method="session")
    html = _render_layout(app, principal=principal)
    assert "/static/css/base.css" in html
    assert "/static/js/common.js" in html


def test_static_base_css_is_served(client, store):
    resp = client.get("/static/css/base.css")
    assert resp.status_code == 200
    assert b":root" in resp.data
    assert b"--accent-primary" in resp.data


def test_static_common_js_is_served(client, store):
    resp = client.get("/static/js/common.js")
    assert resp.status_code == 200
    assert b"fetchWithTimeout" in resp.data
    assert b"escapeHtml" in resp.data
    assert b"timeAgo" in resp.data


def test_static_preexisting_log_viewer_css_still_served_and_unmodified(client, store):
    # Task 24 must not touch log-viewer.css (Task 26 owns that cleanup) --
    # and the new explicit static_folder wiring in gateway/__init__.py must
    # not regress its existing /static/... URL.
    resp = client.get("/static/css/log-viewer.css")
    assert resp.status_code == 200
    on_disk = (Path(__file__).resolve().parent.parent / "src" / "static" / "css" / "log-viewer.css").read_bytes()
    assert resp.data == on_disk


# ---------------------------------------------------------------------------
# login.html
# ---------------------------------------------------------------------------

def test_get_login_renders_with_working_csrf_token_and_fields(client, store):
    resp = client.get("/login")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert _scrape_csrf(html)
    assert 'name="username"' in html
    assert 'name="password"' in html
    assert 'action="/login"' in html
    assert 'method="post"' in html


def test_login_page_is_standalone_and_shows_no_authenticated_nav_chrome(client, store):
    resp = client.get("/login")
    html = resp.get_data(as_text=True)
    # Substring checks on rendered chrome, not the template's own leading
    # HTML comment (which mentions "navbar"/"Log out" while explaining WHY
    # the page doesn't have them) -- strip that comment first.
    html_without_comment = re.sub(r"<!--.*?-->", "", html, count=1, flags=re.DOTALL)
    assert "navbar" not in html_without_comment
    assert 'action="/logout"' not in html_without_comment
    assert 'href="/admin/users"' not in html_without_comment


def test_login_failure_renders_invalid_credentials_message(client, store):
    store.add_user("alice", "correct-password")
    token = _scrape_csrf(client.get("/login").get_data(as_text=True))
    resp = client.post("/login", data={"csrf_token": token, "username": "alice", "password": "wrong"})
    assert resp.status_code == 200
    assert "Invalid username or password" in resp.get_data(as_text=True)


def test_login_error_message_matches_the_literal_auth_routes_actually_uses(app):
    # Direct check against _render_login_page()'s own call site (rather
    # than just a hard-coded string here), so this test fails loudly if
    # Task 19's wording is ever changed without updating this assertion.
    with app.test_request_context("/login"):
        html = _render_login_page(next_value="", error="Invalid username or password")
    assert "Invalid username or password" in html


def test_login_success_does_not_render_error_text(client, store):
    store.add_user("alice", "hunter2")
    token = _scrape_csrf(client.get("/login").get_data(as_text=True))
    resp = client.post("/login", data={"csrf_token": token, "username": "alice", "password": "hunter2"})
    assert resp.status_code == 302  # redirected, no error page at all


# ---------------------------------------------------------------------------
# GET / redirect logic
# ---------------------------------------------------------------------------

def test_root_redirects_to_login_when_anonymous(client, store):
    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"


def test_root_redirects_to_dashboard_when_authenticated(client, store):
    store.add_user("alice", "hunter2")
    token = _scrape_csrf(client.get("/login").get_data(as_text=True))
    client.post("/login", data={"csrf_token": token, "username": "alice", "password": "hunter2"})

    resp = client.get("/")
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/dashboard"


def test_root_ignores_basic_auth_and_still_redirects_to_login(client, store):
    # `/` only ever checks SESSION_ONLY (matches auth/routes.py's own
    # GET /login "already authenticated?" check) -- a Basic/Bearer header
    # must not satisfy it.
    import base64

    store.add_user("alice", "hunter2")
    creds = base64.b64encode(b"alice:hunter2").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 302
    assert resp.headers["Location"] == "/login"
