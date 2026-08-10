"""
Tests for Task 27's real `templates/admin/{users_list,user_form,
password_result}.html` -- the templates that replace Task 23's minimal,
functionally-real stub templates (its own report: "Task 27 replaces these
files' CONTENTS wholesale; the template NAMES and the context variables
each view passes ... are the contract").

`tests/test_gateway_admin_routes.py` (Task 23's own test file, unmodified
by this task) already renders these templates end-to-end for every route
behavior it cares about (guard rails, validation, CSRF, personal-role
reconciliation, datalist union) -- that whole suite passing unmodified
against the new templates (61 passed) is itself the primary verification
that Task 23's "tests will work against real templates once built" claim
held. This file is deliberately narrower and covers what's specific to
the TEMPLATES themselves, not a re-test of that route logic:

- `{% extends "layout.html" %}` real inheritance (all three templates).
- Role-checkbox rendering: master_admin gets the red warning treatment
  (`role-option-danger` + a `badge-admin` pill), no other role does.
- Create-vs-edit username field behavior (a real `name="username"` input
  only on `mode == "new"`; edit shows read-only text, no such input at
  all -- confirmed by ABSENCE of the input, not just presence of text).
- Scope-row client datalist populated from `clients` (_distinct_clients()).
- password_result.html: `Cache-Control: no-store`, a copy-to-clipboard
  button wired to the Clipboard API, and the password value appearing
  in the rendered HTML EXACTLY ONCE.
- CSRF token present on every form in all three templates.
- XSS safety of client-name data specifically: `alerts.client` is
  attacker-influenceable (any node-authenticated client can set an
  arbitrary `client` string on a POSTed alert -- CLAUDE.md's payload
  schema treats it as a plain, node-supplied string with no additional
  sanitization beyond strip+lowercase), and `_distinct_clients()` feeds
  that value straight into the scope-row datalist's `<option value="...">`
  -- this is a different trust boundary than Task 25's dashboard concern
  (alert content, never re-interpolated into an attribute), so it gets
  its own dedicated check: a crafted client value containing `">` /
  `<script>` must render HTML-escaped, and no template may use Jinja's
  `|safe` filter anywhere (a static source grep, since a single `|safe`
  on the wrong variable would silently defeat autoescaping).

--------------------------------------------------------------------------
Post-review addition: scope-row removal renumbering (Critical, fixed)
--------------------------------------------------------------------------
A reviewer found the original "Remove" click handler deleted a
`.scope-row`'s DOM node without renumbering any row after it, breaking
`_parse_scope_pairs()`'s (routes.py) assumption that a row's position
among the OTHER rows still in the form equals the numeric suffix on its
`scope_permissions_N` checkbox names. Reproduced concretely: with 3 rows
(acme/logs:read @0, beta/alerts:write @1, gamma/logs:read @2), removing
acme (first) silently dropped beta's grant and reassigned gamma to beta's
alerts:write instead of its own logs:read; removing down to one surviving
row dropped that row's permissions entirely, which trips
`update_user()`'s "admin emptied every scope row" path and revokes the
user's whole personal scope role.

Fixed by extracting the interaction out of an inline `<script>` block into
a real, `node:test`-covered static file
(`src/static/js/admin-user-form.js`, `tests/js/test_admin_user_form.mjs`)
whose `renumberScopeRows()` now runs after every add/remove, not just
once. This Python file adds one more layer specifically for the "prove
routes.py's own parser pairs a CORRECTLY renumbered submission right, and
would corrupt an INCORRECTLY numbered one" half of that fix -- see the
"scope-row removal renumbering" section near the bottom, which calls
`_parse_scope_pairs()` directly (no HTTP round-trip needed for this one)
reproducing the reviewer's exact 3-row scenario both ways.

Same fixture pattern as tests/test_gateway_admin_routes.py (real
`create_app()`, Postgres/MQTT mocked, a FakeUserStore swapped onto
`app.extensions['user_store']`, a FakePool answering the two raw-SQL
helpers `routes.py` issues) -- the Fakes below are a trimmed copy of that
file's, kept in this file per this repo's established per-file
self-containment convention (see tests/test_gateway_dashboard_template.py,
which does the same relative to tests/test_gateway_dashboard_routes.py).
"""

import re
import sys
from pathlib import Path

import pytest
from werkzeug.datastructures import MultiDict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.admin.routes import _parse_scope_pairs  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from userstore import MASTER_ADMIN_ROLE, PrincipalData, TokenNotFoundError  # noqa: E402

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "src" / "gateway" / "templates" / "admin"


# ---------------------------------------------------------------------------
# Fakes -- trimmed copy of tests/test_gateway_admin_routes.py's own (see
# that file for the fuller version / detailed comments); only what
# rendering these three templates actually needs.
# ---------------------------------------------------------------------------

class FakeUserStore:
    def __init__(self):
        self._users = {}
        self._user_roles = {}
        self._roles = {}
        self._role_scopes = {}
        self._tokens = {}
        self._next_user_id = 1
        self._next_role_id = 1
        self._next_token_id = 1

    def seed_user(self, username, *, user_id=None, status="active", default_client=None,
                  must_change_password=False, last_login_at=None, roles=()):
        username = username.strip().lower()
        uid = user_id if user_id is not None else self._next_user_id
        self._next_user_id = max(self._next_user_id, uid + 1)
        self._users[username] = {
            "id": uid, "username": username, "status": status, "default_client": default_client,
            "must_change_password": must_change_password, "failed_attempts": 0, "locked_until": None,
            "expires_at": None, "last_login_at": last_login_at, "created_at": 0, "updated_at": 0,
        }
        self._user_roles[username] = set()
        for role in roles:
            self.seed_role(role)
            self._user_roles[username].add(role)
        return uid

    def seed_role(self, name, *, is_system=None, description=None):
        if name in self._roles:
            return self._roles[name]["id"]
        role_id = self._next_role_id
        self._next_role_id += 1
        self._roles[name] = {
            "id": role_id, "name": name, "description": description,
            "is_system": is_system if is_system is not None else (name == MASTER_ADMIN_ROLE),
        }
        return role_id

    def seed_scope(self, role_name, client, permission):
        self.seed_role(role_name)
        self._role_scopes.setdefault(role_name, set()).add((client.strip().lower(), permission))

    def seed_token(self, name, username, *, status="active", expires_at=None, last_used_at=None, created_at=0):
        username = username.strip().lower()
        user_id = self._users[username]["id"]
        tid = self._next_token_id
        self._next_token_id += 1
        self._tokens[tid] = {
            "id": tid, "name": name, "user_id": user_id, "username": username, "status": status,
            "expires_at": expires_at, "last_used_at": last_used_at, "created_at": created_at,
        }
        return tid

    def get_user(self, username):
        u = self._users.get(username.strip().lower())
        return dict(u) if u else None

    def list_users(self):
        return [dict(u) for u in sorted(self._users.values(), key=lambda u: u["username"])]

    def get_principal(self, username):
        username = username.strip().lower()
        u = self._users.get(username)
        if u is None:
            return None
        roles = self._user_roles.get(username, set())
        scopes = {}
        for role in roles:
            for client, permission in self._role_scopes.get(role, set()):
                scopes.setdefault(client, set()).add(permission)
        return PrincipalData(
            user_id=u["id"], username=username, status=u["status"], default_client=u["default_client"],
            must_change_password=u["must_change_password"], roles=frozenset(roles),
            scopes={c: frozenset(p) for c, p in scopes.items()},
        )

    def create_user(self, username, password, *, default_client=None, must_change_password=False,
                     status="active", actor=None, source_ip=None):
        username = username.strip().lower()
        uid = self._next_user_id
        self._next_user_id += 1
        self._users[username] = {
            "id": uid, "username": username, "status": status, "default_client": default_client,
            "must_change_password": must_change_password, "failed_attempts": 0, "locked_until": None,
            "expires_at": None, "last_login_at": None, "created_at": 0, "updated_at": 0,
        }
        self._user_roles[username] = set()
        return uid

    def disable_user(self, username, *, actor=None, source_ip=None):
        self._users[username.strip().lower()]["status"] = "disabled"

    def enable_user(self, username, *, actor=None, source_ip=None):
        self._users[username.strip().lower()]["status"] = "active"

    def set_password(self, username, new_password, *, must_change_password=False, actor=None, source_ip=None):
        self._users[username.strip().lower()]["must_change_password"] = must_change_password

    def list_roles(self):
        return sorted(({"id": r["id"], "name": r["name"], "description": r["description"], "is_system": r["is_system"]}
                       for r in self._roles.values()), key=lambda r: r["name"])

    def get_role(self, name):
        r = self._roles.get(name)
        return dict(r) if r else None

    def create_role(self, name, description=None, is_system=False, *, actor=None, source_ip=None):
        if name in self._roles:
            return self._roles[name]["id"]
        role_id = self._next_role_id
        self._next_role_id += 1
        self._roles[name] = {"id": role_id, "name": name, "description": description, "is_system": is_system}
        return role_id

    def grant_role(self, username, role_name, *, actor=None, source_ip=None):
        self._user_roles.setdefault(username.strip().lower(), set()).add(role_name)

    def revoke_role(self, username, role_name, *, actor=None, source_ip=None):
        self._user_roles.get(username.strip().lower(), set()).discard(role_name)

    def add_scope(self, role_name, client, permission, *, actor=None, source_ip=None):
        self._role_scopes.setdefault(role_name, set()).add((client.strip().lower(), permission))

    def remove_scope(self, role_name, client, permission, *, actor=None, source_ip=None):
        self._role_scopes.get(role_name, set()).discard((client.strip().lower(), permission))

    def list_role_scopes(self, role_id):
        role_name = next((name for name, r in self._roles.items() if r["id"] == role_id), None)
        pairs = self._role_scopes.get(role_name, set()) if role_name else set()
        return [{"client": c, "permission": p} for c, p in sorted(pairs)]

    def list_all_scope_clients(self):
        names = set()
        for pairs in self._role_scopes.values():
            names |= {c for c, _p in pairs if c not in (None, "", "*")}
        return names

    def create_token(self, name, user_id, *, expires_at=None, actor=None, source_ip=None):
        tid = self._next_token_id
        self._next_token_id += 1
        username = next((u["username"] for u in self._users.values() if u["id"] == user_id), None)
        self._tokens[tid] = {
            "id": tid, "name": name, "user_id": user_id, "username": username, "status": "active",
            "expires_at": expires_at, "last_used_at": None, "created_at": 0,
        }
        return f"amt_faketoken{tid}"

    def list_tokens(self, user_id=None):
        rows = sorted(self._tokens.values(), key=lambda t: -t["id"])
        return [dict(t) for t in rows if user_id is None or t["user_id"] == user_id]

    def revoke_token(self, token_id, *, actor=None, source_ip=None):
        if token_id not in self._tokens:
            raise TokenNotFoundError(token_id)
        self._tokens[token_id]["status"] = "revoked"


class FakeMqttPublisher:
    def is_connected(self):
        return True

    def forward_to_mqtt(self, alert_data):
        return True


class FakeCursor:
    """Answers only the one raw-SQL query admin/routes.py still issues against the pool directly: `_distinct_clients()`'s `alerts.client` half (the `role_scopes` half now reads through `UserStore.list_all_scope_clients()` -- see FakeUserStore above)."""

    def __init__(self, alert_clients):
        self.alert_clients = alert_clients
        self._result = []

    def execute(self, sql, params=None):
        if "FROM alerts" in sql:
            self._result = [{"client": c} for c in sorted(set(self.alert_clients))]
        else:
            raise AssertionError(f"FakeCursor got an unrecognized query: {sql}")

    def fetchall(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self, cursor_factory=None):
        return self._cursor

    def rollback(self):
        pass


class FakePool:
    def __init__(self, store, alert_clients=None):
        self.store = store
        self.alert_clients = alert_clients or []

    def getconn(self):
        return FakeConnection(FakeCursor(self.alert_clients))

    def putconn(self, conn):
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


@pytest.fixture(autouse=True)
def pool(app, store):
    fake_pool = FakePool(store)
    app.extensions["db_pool"] = fake_pool
    return fake_pool


@pytest.fixture
def client(app):
    return app.test_client()


def _login_session(client, user_id, username) -> None:
    with client.session_transaction() as sess:
        sess["uid"] = user_id
        sess["u"] = username


def _csrf_token(client, app) -> str:
    import hashlib
    import os

    from itsdangerous import URLSafeTimedSerializer

    session_value = hashlib.sha1(os.urandom(64)).hexdigest()
    with client.session_transaction() as sess:
        sess["csrf_token"] = session_value
    serializer = URLSafeTimedSerializer(app.secret_key, salt="wtf-csrf-token")
    return serializer.dumps(session_value)


def _post(client, url, data=None):
    payload = {"csrf_token": _csrf_token(client, client.application)}
    payload.update(data or {})
    return client.post(url, data=payload)


def _admin(store, client, username="root", user_id=1):
    uid = store.seed_user(username, user_id=user_id, roles={MASTER_ADMIN_ROLE})
    _login_session(client, uid, username)
    return uid


# ---------------------------------------------------------------------------
# {% extends "layout.html" %} -- real inheritance, all three templates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["users_list.html", "user_form.html", "password_result.html"])
def test_template_source_extends_layout_html(filename):
    source = (TEMPLATES_DIR / filename).read_text()
    assert re.search(r'{%\s*extends\s+["\']layout\.html["\']\s*%}', source), (
        f"{filename} must extend layout.html via Jinja's {{% extends %}} tag"
    )


def test_users_list_pulls_in_layout_chrome(client, store):
    _admin(store, client)
    html = client.get("/admin/users").get_data(as_text=True)
    assert 'class="navbar"' in html
    assert re.search(r'<meta name="csrf-token" content="[^"]+">', html)
    assert "/static/js/common.js" in html
    assert re.search(r"Alert Manager v\S+", html)
    assert 'href="/admin/users"' in html  # nav "Admin" link, active on this page


def test_user_form_pulls_in_layout_chrome(client, store):
    _admin(store, client)
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert 'class="navbar"' in html
    assert "/static/js/common.js" in html


def test_password_result_pulls_in_layout_chrome(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    html = resp.get_data(as_text=True)
    assert 'class="navbar"' in html
    assert "/static/js/common.js" in html


# ---------------------------------------------------------------------------
# Role checkboxes: master_admin gets the red warning treatment, nothing
# else does
# ---------------------------------------------------------------------------

def test_master_admin_role_checkbox_gets_danger_class(client, store):
    _admin(store, client)
    store.seed_role("acme-viewer")
    html = client.get("/admin/users/new").get_data(as_text=True)

    # The master_admin <label> carries role-option-danger; find its whole
    # <label ...>...</label> block and check the class + warning badge are
    # both inside it, tied to the master_admin checkbox specifically (not
    # just present somewhere on the page).
    match = re.search(
        r'<label class="([^"]*)">\s*<input type="checkbox" name="roles" value="master_admin"[^>]*>.*?</label>',
        html, re.DOTALL,
    )
    assert match, "expected a <label> wrapping the master_admin role checkbox"
    assert "role-option-danger" in match.group(1)
    assert "badge-admin" in match.group(0)


def test_non_master_admin_role_checkbox_has_no_danger_class(client, store):
    _admin(store, client)
    store.seed_role("acme-viewer")
    html = client.get("/admin/users/new").get_data(as_text=True)

    match = re.search(
        r'<label class="([^"]*)">\s*<input type="checkbox" name="roles" value="acme-viewer"[^>]*>.*?</label>',
        html, re.DOTALL,
    )
    assert match, "expected a <label> wrapping the acme-viewer role checkbox"
    assert "role-option-danger" not in match.group(1)
    assert "badge-admin" not in match.group(0)


def test_master_admin_role_tag_on_list_page_gets_admin_badge(client, store):
    # list_users()'s own context now carries master_admin_role (a small
    # Task 27 addition to routes.py -- see task-27-report.md) so the Roles
    # column can apply the same red treatment to the tag, not just the
    # checkbox on the form.
    _admin(store, client, username="root", user_id=1)
    store.seed_user("bob", user_id=2, roles={"acme-viewer"})
    store.seed_role("acme-viewer")

    html = client.get("/admin/users").get_data(as_text=True)
    assert re.search(r'<span class="badge badge-admin">\s*master_admin\s*</span>', html)
    assert re.search(r'<span class="badge badge-user">\s*acme-viewer\s*</span>', html)


# ---------------------------------------------------------------------------
# Create-vs-edit username field behavior
# ---------------------------------------------------------------------------

def test_new_form_has_editable_username_input(client, store):
    _admin(store, client)
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert re.search(r'<input type="text"[^>]*name="username"', html)


def test_edit_form_has_no_username_input_at_all(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    # Not just "not editable" text -- there must be no submittable
    # <input name="username"> field of ANY type for an admin to tamper
    # with. Regex (not a bare substring check) so this doesn't false-
    # positive on this template's own header comment, which discusses
    # the username field in prose.
    assert not re.search(r'<input[^>]*\bname="username"', html)
    assert "bob" in html  # still shown, just as read-only text


# ---------------------------------------------------------------------------
# Service account / Issue token fields (new-mode only) + Tokens fieldset
# (edit-mode only)
# ---------------------------------------------------------------------------

def test_new_form_has_service_account_and_issue_token_fields(client, store):
    _admin(store, client)
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert 'name="service_account"' in html
    assert 'name="issue_token"' in html
    assert re.search(r'name="token_name"[^>]*value="default"', html)


def test_edit_form_has_no_new_mode_service_account_or_issue_token_checkbox(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert 'id="user-form-service-account"' not in html
    assert 'id="user-form-issue-token"' not in html


def test_edit_form_tokens_fieldset_lists_tokens(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_token("prod-key", "bob", status="active")
    store.seed_token("old-key", "bob", status="revoked")

    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert "<legend>Tokens</legend>" in html
    assert "prod-key" in html
    assert "old-key" in html
    assert 'status-pill status-active">active' in html
    assert 'status-pill status-disabled">revoked' in html


def test_edit_form_revoke_button_only_for_active_tokens(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_token("prod-key", "bob", status="active")
    store.seed_token("old-key", "bob", status="revoked")

    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    revoke_actions = re.findall(r'action="[^"]*/tokens/\d+/revoke"', html)
    assert len(revoke_actions) == 1


def test_edit_form_no_tokens_shows_placeholder_row(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert "No tokens yet." in html


def test_edit_form_issue_new_token_mini_form_present(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert 'action="/admin/users/bob/tokens/new"' in html
    assert 'name="token_name"' in html
    assert "Issue new token" in html


def test_edit_form_token_forms_have_csrf_token(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_token("prod-key", "bob", status="active")

    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    token_forms = re.findall(r"<form\b[^>]*tokens[^>]*>.*?</form>", html, re.DOTALL)
    assert len(token_forms) >= 2  # revoke + issue-new-token
    for form in token_forms:
        assert re.search(r'name="csrf_token" value="[^"]+"', form)


# ---------------------------------------------------------------------------
# Scope-row client datalist populated from `clients`
# ---------------------------------------------------------------------------

def test_scope_datalist_populated_from_clients_context(client, store, pool):
    _admin(store, client)
    pool.alert_clients = ["acme", "revysmc"]
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert re.search(r'<datalist id="clients-datalist">', html)
    assert '<option value="acme">' in html
    assert '<option value="revysmc">' in html


def test_scope_datalist_empty_when_no_clients(client, store, pool):
    _admin(store, client)
    pool.alert_clients = []
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert re.search(r'<datalist id="clients-datalist">\s*</datalist>', html)


def test_add_scope_row_script_and_template_present(client, store):
    # Static structural check for the "add another scope row" interaction
    # -- the actual click/renumber behavior needs a real DOM/browser
    # (documented gap, same class as Task 25's dashboard.js) for the
    # innerHTML-cloning "add" path specifically; the pure renumbering
    # logic itself (the thing the critical review finding was about) DOES
    # have real node:test coverage -- see tests/js/test_admin_user_form.mjs
    # and src/static/js/admin-user-form.js. This test just pins the public
    # element-ID contract that file's init() wires against, and that the
    # page actually loads that file (post-review: moved out of an inline
    # <script> block specifically so it could be node:test-covered).
    _admin(store, client)
    html = client.get("/admin/users/new").get_data(as_text=True)
    assert 'id="scope-rows-container"' in html
    assert 'id="add-scope-row-btn"' in html
    assert '<template id="scope-row-template">' in html
    assert "__ROW_INDEX__" in html
    assert '<script src="/static/js/admin-user-form.js"></script>' in html


def test_admin_user_form_js_actually_servable(client, store):
    # Not just "the <script> tag is present" (above) -- confirms the file
    # it points at is real and actually served, the same regression class
    # Task 24's own test suite checks for base.css/common.js.
    _admin(store, client)
    resp = client.get("/static/js/admin-user-form.js")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "renumberScopeRows" in body


# ---------------------------------------------------------------------------
# password_result.html
# ---------------------------------------------------------------------------

def test_password_page_has_no_store_cache_control(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    assert resp.headers.get("Cache-Control") == "no-store"


def test_password_page_has_copy_button_wired_to_clipboard_api(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    html = resp.get_data(as_text=True)
    assert 'id="copy-password-btn"' in html
    assert "navigator.clipboard" in html
    assert "writeText" in html


def test_password_value_appears_exactly_once_in_rendered_page(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    html = resp.get_data(as_text=True)

    match = re.search(r'<code id="generated-password"[^>]*>([^<]+)</code>', html)
    assert match, "expected #generated-password to contain the password text"
    password = match.group(1).strip()
    assert len(password) > 10  # secrets.token_urlsafe(12) output, sanity check

    assert html.count(password) == 1


def test_password_page_reset_also_appears_exactly_once(client, store):
    _admin(store, client)
    store.seed_user("target", user_id=55)
    resp = _post(client, "/admin/users/target/reset-password")
    html = resp.get_data(as_text=True)
    match = re.search(r'<code id="generated-password"[^>]*>([^<]+)</code>', html)
    assert match
    assert html.count(match.group(1).strip()) == 1


# ---------------------------------------------------------------------------
# password_result.html: optional Bearer token reveal card
# ---------------------------------------------------------------------------

def test_password_result_shows_token_card_when_token_present(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "issue_token": "on"})
    html = resp.get_data(as_text=True)
    assert 'id="generated-token"' in html
    assert 'id="copy-token-btn"' in html
    assert "navigator.clipboard" in html
    assert "writeText" in html


def test_password_result_hides_token_card_when_no_token(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    html = resp.get_data(as_text=True)
    assert 'id="generated-token"' not in html


def test_reset_password_still_single_card_no_token_card(client, store):
    # Regression guard: reset_password() always passes token=None -- its
    # response shape must stay exactly what it was before tokens existed.
    _admin(store, client)
    store.seed_user("target", user_id=55)
    resp = _post(client, "/admin/users/target/reset-password")
    html = resp.get_data(as_text=True)
    assert 'id="generated-token"' not in html
    assert 'id="generated-password"' in html


def test_issue_token_route_shows_only_token_card_no_password(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/new", {"token_name": "svc"})
    html = resp.get_data(as_text=True)
    assert 'id="generated-token"' in html
    assert 'id="generated-password"' not in html


def test_token_value_appears_exactly_once_on_create(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "issue_token": "on"})
    html = resp.get_data(as_text=True)
    match = re.search(r'<code id="generated-token"[^>]*>([^<]+)</code>', html)
    assert match
    token = match.group(1).strip()
    assert html.count(token) == 1


def test_token_value_appears_exactly_once_on_issue_token_route(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/new", {"token_name": "svc"})
    html = resp.get_data(as_text=True)
    match = re.search(r'<code id="generated-token"[^>]*>([^<]+)</code>', html)
    assert match
    token = match.group(1).strip()
    assert html.count(token) == 1


# ---------------------------------------------------------------------------
# CSRF token present on every form
# ---------------------------------------------------------------------------

def test_users_list_every_form_has_csrf_token(client, store):
    _admin(store, client, username="root", user_id=1)
    store.seed_user("other", user_id=2)
    html = client.get("/admin/users").get_data(as_text=True)

    forms = re.findall(r"<form\b.*?</form>", html, re.DOTALL)
    assert len(forms) >= 2  # at least disable + reset-password for "other"
    for form in forms:
        assert re.search(r'name="csrf_token" value="[^"]+"', form), f"form missing CSRF token: {form[:120]!r}"


@pytest.mark.parametrize("url", ["/admin/users/new", "/admin/users/bob/edit"])
def test_user_form_has_csrf_token(client, store, url):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    html = client.get(url).get_data(as_text=True)
    assert re.search(r'name="csrf_token" value="[^"]+"', html)


# ---------------------------------------------------------------------------
# XSS safety
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", ["users_list.html", "user_form.html", "password_result.html"])
def test_template_never_uses_safe_filter(filename):
    # A single |safe on the wrong variable would silently defeat Jinja's
    # default autoescaping for exactly the attacker-influenceable values
    # this task is most worried about (client names). Static source check,
    # not just "the rendered output looked fine for this one test input".
    source = (TEMPLATES_DIR / filename).read_text()
    assert "|safe" not in source
    assert "| safe" not in source
    assert "{% autoescape false %}" not in source


def test_scope_datalist_html_escapes_malicious_client_name(client, store, pool):
    # alerts.client is attacker-influenceable (any node-authenticated
    # client can set an arbitrary "client" string on a POSTed alert --
    # CLAUDE.md's payload schema only strips/lowercases it, no other
    # sanitization) and _distinct_clients() feeds that value straight into
    # this datalist's <option value="..."> -- a different trust boundary
    # than Task 25's dashboard concern (alert CONTENT, never re-embedded
    # into an HTML attribute). Confirms Jinja's default autoescaping
    # neutralizes a breakout attempt rather than any bespoke sanitization
    # this template would otherwise need to hand-roll.
    _admin(store, client)
    malicious = '"><script>alert(1)</script>'
    pool.alert_clients = [malicious]

    html = client.get("/admin/users/new").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_scope_row_client_value_html_escapes_on_edit_prepopulate(client, store, pool):
    # Same trust boundary, different render path: an existing scope row's
    # client value (role_scopes.client, prepopulated on GET edit from
    # _role_scope_rows()) lands in a text <input value="...">, not just the
    # datalist's <option>.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", '"><script>alert(1)</script>', "logs:read")
    store._user_roles["bob"].add("user:bob")

    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_username_html_escapes_on_edit_page(client, store):
    # Usernames are regex-constrained server-side (^[a-z0-9_-]{3,32}$) so
    # this can't happen through the real create route -- seeded directly
    # here as defense-in-depth verification that the template itself
    # doesn't assume that invariant (e.g. via |safe) rather than relying
    # on Jinja's autoescaping like everything else on this page.
    _admin(store, client)
    store._users["bob<script>"] = {
        "id": 2, "username": "bob<script>", "status": "active", "default_client": None,
        "must_change_password": False, "failed_attempts": 0, "locked_until": None,
        "expires_at": None, "last_login_at": None, "created_at": 0, "updated_at": 0,
    }
    store._user_roles["bob<script>"] = set()

    html = client.get("/admin/users/bob%3Cscript%3E/edit").get_data(as_text=True)
    assert "bob<script>" not in html
    assert "bob&lt;script&gt;" in html


def test_token_name_html_escapes_in_tokens_table(client, store):
    # Token names are operator-chosen free text (auth_tokens.name has no
    # charset restriction beyond VARCHAR(255) NOT NULL) -- same trust
    # boundary discipline as client names above.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_token('"><script>alert(1)</script>', "bob")

    html = client.get("/admin/users/bob/edit").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_token_name_html_escapes_on_reveal_page(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(
        client, "/admin/users/bob/tokens/new",
        {"token_name": '"><script>alert(1)</script>'},
    )
    html = resp.get_data(as_text=True)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# Post-review: scope-row removal renumbering, proven end-to-end through
# routes.py's REAL _parse_scope_pairs() -- no HTTP round-trip needed for
# this half of the fix, since the bug/fix both live entirely in what form
# field NAMES a browser would submit, which _parse_scope_pairs() consumes
# directly. Reproduces the reviewer's exact 3-row scenario both ways: a
# submission shaped like the ORIGINAL (unrenumbered) bug corrupts the
# pairing exactly as reported; a submission shaped like the FIXED
# JS's output (post-renumber, via admin-user-form.js's
# renumberScopeRows() -- see tests/js/test_admin_user_form.mjs for that
# half) parses correctly. Together with the JS test, this closes the loop:
# the JS test proves renumbering itself is correct; this proves that a
# correctly-renumbered submission is what the backend actually needs.
# ---------------------------------------------------------------------------

def test_parse_scope_pairs_characterizes_the_original_removal_bug(client, store):
    # Reviewer's exact reproduction, reconstructed as the raw form data an
    # UNFIXED browser would have submitted after removing the FIRST of 3
    # rows (acme/logs:read @0, beta/alerts:write @1, gamma/logs:read @2)
    # without renumbering survivors: scope_client submits in the new DOM
    # order ["beta", "gamma"] (positions 0/1), but the checkbox fields
    # were never renamed, so they're STILL "scope_permissions_1"/
    # "scope_permissions_2".
    form = MultiDict(
        [
            ("scope_client", "beta"),
            ("scope_client", "gamma"),
            ("scope_permissions_1", "alerts:write"),  # beta's field, stale name
            ("scope_permissions_2", "logs:read"),  # gamma's field, stale name
        ]
    )

    pairs = _parse_scope_pairs(form)

    # This IS the corruption the reviewer reported: beta (position 0)
    # looks for "scope_permissions_0", which doesn't exist -- its grant
    # silently vanishes. gamma (position 1) looks for
    # "scope_permissions_1", which exists but is BETA's old field --
    # gamma silently inherits alerts:write instead of its own logs:read.
    assert pairs == [("gamma", "alerts:write")]
    assert ("beta", "alerts:write") not in pairs  # beta's grant: gone entirely
    assert ("gamma", "logs:read") not in pairs  # gamma's real grant: also gone


def test_parse_scope_pairs_correct_after_renumbered_removal_submission(client, store):
    # The FIX's contract: the same removal (acme, first of 3, removed),
    # but the form now reflects what admin-user-form.js's
    # renumberScopeRows() actually produces post-fix -- beta and gamma's
    # checkbox fields renamed to "_0"/"_1" to match their new DOM
    # positions (proven correct in isolation by
    # tests/js/test_admin_user_form.mjs's own "reviewer's exact scenario"
    # test).
    form = MultiDict(
        [
            ("scope_client", "beta"),
            ("scope_client", "gamma"),
            ("scope_permissions_0", "alerts:write"),  # beta's field, correctly renumbered
            ("scope_permissions_1", "logs:read"),  # gamma's field, correctly renumbered
        ]
    )

    pairs = _parse_scope_pairs(form)

    assert pairs == [("beta", "alerts:write"), ("gamma", "logs:read")]


def test_parse_scope_pairs_correct_after_removing_down_to_one_row(client, store):
    # The "worse" case the reviewer flagged: only gamma survives (acme and
    # beta both removed). Post-fix, gamma's field is renumbered to "_0".
    form = MultiDict(
        [
            ("scope_client", "gamma"),
            ("scope_permissions_0", "logs:read"),
        ]
    )

    pairs = _parse_scope_pairs(form)

    assert pairs == [("gamma", "logs:read")]
    # And critically, this is NOT an empty scope set -- the exact
    # condition that would otherwise trip update_user()'s "admin emptied
    # every scope row" path and revoke the user's whole personal role.
    assert len(pairs) == 1


def test_edit_removing_first_of_three_scope_rows_preserves_survivors_end_to_end(client, store):
    # Full route-level proof (not just the parser in isolation): an edit
    # POST shaped like the fixed JS's post-renumber submission for the
    # reviewer's exact 3-row/remove-first scenario actually reconciles
    # beta's and gamma's scopes correctly through the real admin route,
    # including add_scope/remove_scope calls against the FakeUserStore.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store.seed_scope("user:bob", "beta", "alerts:write")
    store.seed_scope("user:bob", "gamma", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = _post(
        client,
        "/admin/users/bob/edit",
        {
            "roles": [],
            "scope_client": ["beta", "gamma"],  # acme's row removed
            "scope_permissions_0": ["alerts:write"],  # beta, correctly renumbered
            "scope_permissions_1": ["logs:read"],  # gamma, correctly renumbered
        },
    )
    assert resp.status_code == 302

    scopes = store.get_principal("bob").scopes
    assert scopes.get("beta") == frozenset({"alerts:write"})
    assert scopes.get("gamma") == frozenset({"logs:read"})
    assert "acme" not in scopes  # removed row's grant actually revoked
