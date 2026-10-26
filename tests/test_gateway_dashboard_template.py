"""
Tests for Task 25's `templates/dashboard.html` + its wiring into
`gateway/dashboard/routes.py`'s `view_dashboard()` (`GET /dashboard`).

Task 22 (`GET /dashboard`) originally rendered a hand-rolled placeholder
HTML string (see that route's now-superseded docstring, still visible in
git history) whose own module docstring explicitly said "Task 24/25 build
the real template + JS" -- this file verifies that hand-off actually
happened: `view_dashboard()` now calls `render_template("dashboard.html",
db_ok=...)`, and `dashboard.html` (Jinja's `{% extends %}`) correctly pulls
in `layout.html`'s nav/CSRF-meta/footer chrome (Task 24) rather than
standing alone.

Same fixture pattern as tests/test_gateway_dashboard_routes.py (real
`create_app()`, Postgres/MQTT mocked, a FakePool/FakeCursor swapped onto
`app.extensions['db_pool']` -- no live Postgres in this environment) and
tests/test_gateway_templates.py (Task 24's own template test file) --
this file is deliberately narrow and only covers what's specific to
dashboard.html/its route, not a re-test of layout.html's own nav logic
(already covered by test_gateway_templates.py) or the JSON API routes
(already covered by test_gateway_dashboard_routes.py).

Covers:
- `{% extends "layout.html" %}` actually pulls in layout.html's shared
  chrome (navbar, CSRF meta tag, common.js include, version footer) --
  proof of successful inheritance, not just "the file exists".
- dashboard.html's own `extra_head`/`extra_scripts` blocks link
  dashboard.css/dashboard.js at their expected `/static/...` URLs.
- Every element ID dashboard.js's `init()` looks up via
  `document.getElementById(...)` is actually present in the rendered
  markup (a static contract check between the two files, independent of
  any real browser/DOM).
- The route renders successfully end-to-end for both the DB-up (200) and
  DB-down (503, server-rendered warning banner) cases -- extending
  test_gateway_dashboard_routes.py's own `test_dashboard_page_200_when_db_ok`
  / `test_dashboard_page_503_with_warning_banner_when_db_down` (which this
  file does not duplicate) with assertions specific to the REAL template
  now in place (window <select> options, card grid / table / dialog
  containers, all 8 window enum values selectable).
- Anonymous request still redirects to /login (session-only auth
  unaffected by the template swap).
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from userstore import PrincipalData  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures -- same shape as tests/test_gateway_dashboard_routes.py
# ---------------------------------------------------------------------------

def _principal_data(username="alice", user_id=1, roles=frozenset(), scopes=None) -> PrincipalData:
    return PrincipalData(
        user_id=user_id,
        username=username,
        status="active",
        default_client=None,
        must_change_password=False,
        roles=roles,
        scopes=scopes or {},
    )


class FakeUserStore:
    def __init__(self):
        self._users = {}

    def add_user(self, username, **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = data
        return data

    def get_principal(self, username):
        return self._users.get(username)

    def verify_login(self, username, password, *, source_ip=None):
        return None

    def verify_token(self, token):
        return None


class FakeCursor:
    def execute(self, sql, params=None):
        return None

    def fetchone(self):
        return {"?column?": 1}

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor=None, raise_on_cursor=False):
        self._cursor = cursor or FakeCursor()
        self._raise_on_cursor = raise_on_cursor

    def cursor(self, cursor_factory=None):
        if self._raise_on_cursor:
            raise RuntimeError("connection refused")
        return self._cursor

    def rollback(self):
        pass


class FakePool:
    """Controllable stand-in for the psycopg2 pool -- `db_down` makes `_probe_db`'s cheap SELECT 1 fail."""

    def __init__(self, db_down=False):
        self.db_down = db_down

    def getconn(self):
        if self.db_down:
            raise RuntimeError("connection refused")
        return FakeConnection()

    def putconn(self, conn):
        pass


class FakeMqttPublisher:
    def is_connected(self):
        return True

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


def _install_pool(app, db_down=False) -> FakePool:
    pool = FakePool(db_down=db_down)
    app.extensions["db_pool"] = pool
    return pool


def _login_session(client, principal: PrincipalData) -> None:
    with client.session_transaction() as sess:
        sess["uid"] = principal.user_id
        sess["u"] = principal.username


# ---------------------------------------------------------------------------
# extends layout.html -- real inheritance, not just "the file exists"
# ---------------------------------------------------------------------------

def test_dashboard_template_source_extends_layout_html():
    template_path = (
        Path(__file__).resolve().parent.parent / "src" / "gateway" / "templates" / "dashboard.html"
    )
    source = template_path.read_text()
    assert re.search(r'{%\s*extends\s+["\']layout\.html["\']\s*%}', source), (
        "dashboard.html must extend layout.html via Jinja's {% extends %} tag"
    )


def test_dashboard_page_pulls_in_layout_chrome(client, store):
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=False)
    _login_session(client, principal)

    html = client.get("/dashboard").get_data(as_text=True)

    # layout.html's own chrome (Task 24), only present if inheritance
    # actually happened -- a standalone dashboard.html (like login.html
    # deliberately is) would not have these.
    assert 'class="navbar"' in html
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', html)
    assert "/static/js/common.js" in html
    assert re.search(r"Alert Manager v\S+", html)
    assert 'href="/dashboard"' in html  # nav link, active on this page
    assert 'class="nav-link active"' in html or "active" in html


def test_dashboard_page_links_its_own_css_and_js():
    # Asserts against the static template source for the two extra blocks
    # (extra_head/extra_scripts) directly, independent of any route/DB
    # state -- the rendered-page equivalent is covered by
    # test_dashboard_page_pulls_in_layout_chrome and the 200/503 tests
    # below, which check the resolved /static/... URLs instead.
    template_path = (
        Path(__file__).resolve().parent.parent / "src" / "gateway" / "templates" / "dashboard.html"
    )
    source = template_path.read_text()
    assert "css/dashboard.css" in source
    assert "js/dashboard.js" in source
    assert "{% block extra_head %}" in source
    assert "{% block extra_scripts %}" in source


# ---------------------------------------------------------------------------
# Every element ID dashboard.js's init() looks up must exist in the
# rendered markup -- a static contract check between the two files.
# ---------------------------------------------------------------------------

DASHBOARD_JS_ELEMENT_IDS = [
    "dashboard-window-select",
    "dashboard-updated",
    "dashboard-error-banner",
    "dashboard-cards",
    "dashboard-groups-section",
    "dashboard-groups",
    "dashboard-groups-showing",
    "dashboard-empty-state",
    "dashboard-expand-all",
    "dashboard-collapse-all",
    "dashboard-alert-modal",
    "dashboard-modal-close",
    "dashboard-modal-message",
    "dashboard-modal-metadata-tbody",
    "dashboard-modal-raw-json",
]

MODAL_DEFINITION_FIELD_IDS = [
    "dashboard-modal-field-id",
    "dashboard-modal-field-timestamp",
    "dashboard-modal-field-category",
    "dashboard-modal-field-source",
    "dashboard-modal-field-client",
    "dashboard-modal-field-ingestion-source",
]


def test_dashboard_js_source_only_references_ids_present_in_the_template():
    """
    Extracts every `getElementById("...")` call dashboard.js makes and
    asserts each ID exists somewhere in dashboard.html's raw source (an
    `id="..."` attribute) -- catches a renamed element in one file that
    wasn't mirrored in the other, without needing a real DOM to click
    through.
    """
    js_path = Path(__file__).resolve().parent.parent / "src" / "static" / "js" / "dashboard.js"
    html_path = Path(__file__).resolve().parent.parent / "src" / "gateway" / "templates" / "dashboard.html"
    js_source = js_path.read_text()
    html_source = html_path.read_text()

    referenced_ids = set(re.findall(r'getElementById\("([^"]+)"\)', js_source))
    assert referenced_ids, "expected dashboard.js to reference at least one element ID"

    html_ids = set(re.findall(r'\bid="([^"]+)"', html_source))

    missing = referenced_ids - html_ids
    assert not missing, f"dashboard.js references ID(s) not present in dashboard.html: {sorted(missing)}"


@pytest.mark.parametrize("element_id", DASHBOARD_JS_ELEMENT_IDS + MODAL_DEFINITION_FIELD_IDS)
def test_expected_element_id_present_in_rendered_dashboard_page(client, store, element_id):
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=False)
    _login_session(client, principal)

    html = client.get("/dashboard").get_data(as_text=True)
    assert f'id="{element_id}"' in html


# ---------------------------------------------------------------------------
# Timeframe <select>: all 8 window enum values, default 24h
# ---------------------------------------------------------------------------

def test_dashboard_page_window_select_has_all_8_enum_values_default_24h(client, store):
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=False)
    _login_session(client, principal)

    html = client.get("/dashboard").get_data(as_text=True)

    for window in ("1h", "6h", "12h", "24h", "7d", "14d", "30d", "all"):
        assert f'value="{window}"' in html, f"missing <option value=\"{window}\">"

    assert re.search(r'<option value="24h"\s+selected>', html), "24h must be the default-selected option"


# ---------------------------------------------------------------------------
# 200 (DB up) / 503 (DB down, server-rendered banner) -- real template now
# in place, not the old placeholder string
# ---------------------------------------------------------------------------

def test_dashboard_page_200_when_db_ok_renders_real_template(client, store):
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=False)
    _login_session(client, principal)

    resp = client.get("/dashboard")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    html = resp.get_data(as_text=True)
    assert "temporarily unavailable" not in html
    assert "Alert Manager - Dashboard" in html
    assert 'id="dashboard-cards"' in html


def test_dashboard_page_503_when_db_down_renders_server_side_banner(client, store):
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=True)
    _login_session(client, principal)

    resp = client.get("/dashboard")
    assert resp.status_code == 503
    assert resp.mimetype == "text/html"
    html = resp.get_data(as_text=True)
    assert "temporarily unavailable" in html
    assert 'id="dashboard-db-banner"' in html
    # The page shell (cards/table/modal containers) still renders even
    # when the DB is down -- dashboard.js's own client-side fetch/503
    # handling is independent defense, not a replacement for this.
    assert 'id="dashboard-cards"' in html


def test_dashboard_db_up_page_has_no_db_banner_element_at_all(client, store):
    # Not just hidden -- the server-rendered banner block is Jinja-gated
    # ({% if not db_ok %}) and shouldn't be emitted at all when db_ok.
    principal = store.add_user("alice", roles=frozenset({"master_admin"}), scopes={"*": frozenset({"logs:read"})})
    _install_pool(client.application, db_down=False)
    _login_session(client, principal)

    html = client.get("/dashboard").get_data(as_text=True)
    assert 'id="dashboard-db-banner"' not in html


# ---------------------------------------------------------------------------
# Auth unaffected by the template swap
# ---------------------------------------------------------------------------

def test_dashboard_page_still_redirects_anonymous_to_login(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")
