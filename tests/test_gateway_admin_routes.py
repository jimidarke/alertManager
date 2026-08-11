"""
Tests for src/gateway/admin/routes.py (Task 23): the `admin` blueprint
(`GET /admin/users`, `GET/POST /admin/users/new`,
`GET/POST /admin/users/<username>/edit`,
`POST .../disable|enable|reset-password`, `POST .../tokens/new`,
`POST .../tokens/<id>/revoke`, `GET /admin/clients`,
`GET/POST /admin/clients/new`, `POST /admin/clients/<name>/disable|enable`),
wired through the real `create_app()` (so ProxyFix/CSRFProtect/blueprint
registration are exactly what production gets), with Postgres/MQTT mocked
exactly as tests/test_gateway_app.py establishes and a FakeUserStore
swapped onto app.extensions afterward. `_role_scope_rows()` (used to
reconcile the personal per-user role's scope set on edit) reads through
`UserStore.list_role_scopes()` -- i.e. through the SAME FakeUserStore
instance, no separate fake needed -- per the final whole-branch review's
"UserStore single owner" fix. No live Postgres is available in this
environment (and per project rule, docker/docker-compose is never invoked
from automation).

Covers, in priority order (per this task's brief):
- THE guard-rail matrix (the highest bar here, same class of risk Task 22's
  review flagged): self-disable rejected via exact user_id comparison;
  self-master_admin-removal rejected via a fresh UserStore.get_principal()
  role-membership check; disabling/de-admin-ing SOMEONE ELSE always
  succeeds normally, including de-admin-ing another master_admin.
- require_master_admin enforcement: anonymous -> 302 to /login (browser
  page, not /api/*); session-authenticated but non-master-admin -> 403.
- Username regex validation on create (valid/invalid patterns), and that
  edit does NOT re-validate/reject based on it (username isn't editable at
  all -- the form never lets it change).
- Passwords: always secrets.token_urlsafe(12)-generated, a user-supplied
  "password" form field has zero effect, a fresh value every request,
  Cache-Control: no-store on the one-time-reveal response.
- CSRF requirement on every POST.
- No delete route exists at all (users OR clients).
- The personal-role scope reconciliation (add newly-checked scopes, remove
  newly-unchecked ones) and the clients registry (sql/migrations/
  0004_clients.sql): the /admin/clients CRUD routes, and create_user()/
  update_user() rejecting a scope-row client or default_client that isn't
  a registered, active client.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
from userstore import (  # noqa: E402
    ALLOWED_PERMISSIONS,
    MASTER_ADMIN_ROLE,
    ClientNotFoundError,
    PrincipalData,
    RoleNotFoundError,
    TokenNotFoundError,
    UserNotFoundError,
)


# ---------------------------------------------------------------------------
# Fakes: FakeUserStore mirrors UserStore's account/role/scope surface
# in-memory; FakePool answers the two raw-SQL helpers by reading straight
# out of the SAME FakeUserStore instance's internal state, so a write made
# earlier in a request (e.g. update_user's store.create_role() +
# store.add_scope() calls) is immediately visible to a later
# _role_scope_rows() read in that same request -- exactly like a real DB
# transaction would behave, without needing one.
# ---------------------------------------------------------------------------

class FakeUserStore:
    def __init__(self):
        self._users = {}  # username -> dict (matches UserStore._USER_COLUMNS shape)
        self._user_roles = {}  # username -> set(role_name)
        self._roles = {}  # role_name -> {"id", "name", "description", "is_system"}
        self._role_scopes = {}  # role_name -> set((client, permission))
        self._tokens = {}  # token_id -> dict (matches UserStore.list_tokens()'s row shape)
        self._clients = {}  # name -> {"id", "name", "description", "status"}
        self._next_user_id = 1
        self._next_role_id = 1
        self._next_token_id = 1
        self._next_client_id = 1
        self.calls = []  # [(method_name, args, kwargs), ...] -- exact-call-sequence assertions

    def _log(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))

    def calls_named(self, name):
        return [(a, k) for (n, a, k) in self.calls if n == name]

    # -- test-only seeding helpers --------------------------------------

    def seed_user(
        self, username, *, user_id=None, status="active", default_client=None,
        must_change_password=False, last_login_at=None, roles=(),
    ):
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

    def seed_client(self, name, *, status="active", description=None):
        name = name.strip().lower()
        if name in self._clients:
            return self._clients[name]["id"]
        cid = self._next_client_id
        self._next_client_id += 1
        self._clients[name] = {"id": cid, "name": name, "description": description, "status": status}
        return cid

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

    # -- UserStore-mirroring interface -----------------------------------

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
        self._log("create_user", username, password, default_client=default_client,
                   must_change_password=must_change_password, status=status, actor=actor, source_ip=source_ip)
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
        username = username.strip().lower()
        self._log("disable_user", username, actor=actor, source_ip=source_ip)
        if username not in self._users:
            raise UserNotFoundError(username)
        self._users[username]["status"] = "disabled"

    def enable_user(self, username, *, actor=None, source_ip=None):
        username = username.strip().lower()
        self._log("enable_user", username, actor=actor, source_ip=source_ip)
        if username not in self._users:
            raise UserNotFoundError(username)
        self._users[username]["status"] = "active"

    def set_password(self, username, new_password, *, must_change_password=False, actor=None, source_ip=None):
        username = username.strip().lower()
        self._log("set_password", username, new_password, must_change_password=must_change_password,
                   actor=actor, source_ip=source_ip)
        if username not in self._users:
            raise UserNotFoundError(username)
        self._users[username]["must_change_password"] = must_change_password

    def list_roles(self):
        return sorted(({"id": r["id"], "name": r["name"], "description": r["description"], "is_system": r["is_system"]}
                       for r in self._roles.values()), key=lambda r: r["name"])

    def get_role(self, name):
        r = self._roles.get(name)
        return dict(r) if r else None

    def create_role(self, name, description=None, is_system=False, *, actor=None, source_ip=None):
        self._log("create_role", name, description=description, is_system=is_system, actor=actor, source_ip=source_ip)
        if name in self._roles:
            return self._roles[name]["id"]
        role_id = self._next_role_id
        self._next_role_id += 1
        self._roles[name] = {"id": role_id, "name": name, "description": description, "is_system": is_system}
        return role_id

    def grant_role(self, username, role_name, *, actor=None, source_ip=None):
        username = username.strip().lower()
        self._log("grant_role", username, role_name, actor=actor, source_ip=source_ip)
        if username not in self._users:
            raise UserNotFoundError(username)
        if role_name not in self._roles:
            raise RoleNotFoundError(role_name)
        self._user_roles.setdefault(username, set()).add(role_name)

    def revoke_role(self, username, role_name, *, actor=None, source_ip=None):
        username = username.strip().lower()
        self._log("revoke_role", username, role_name, actor=actor, source_ip=source_ip)
        if username not in self._users:
            raise UserNotFoundError(username)
        if role_name not in self._roles:
            raise RoleNotFoundError(role_name)
        self._user_roles.get(username, set()).discard(role_name)

    def add_scope(self, role_name, client, permission, *, actor=None, source_ip=None):
        self._log("add_scope", role_name, client, permission, actor=actor, source_ip=source_ip)
        if permission not in ALLOWED_PERMISSIONS:
            raise ValueError(f"invalid permission {permission!r}")
        if role_name not in self._roles:
            raise RoleNotFoundError(role_name)
        self._role_scopes.setdefault(role_name, set()).add((client.strip().lower(), permission))

    def remove_scope(self, role_name, client, permission, *, actor=None, source_ip=None):
        self._log("remove_scope", role_name, client, permission, actor=actor, source_ip=source_ip)
        if role_name not in self._roles:
            raise RoleNotFoundError(role_name)
        self._role_scopes.get(role_name, set()).discard((client.strip().lower(), permission))

    def list_role_scopes(self, role_id):
        self._log("list_role_scopes", role_id)
        role_name = next((name for name, r in self._roles.items() if r["id"] == role_id), None)
        pairs = self._role_scopes.get(role_name, set()) if role_name else set()
        return [{"client": c, "permission": p} for c, p in sorted(pairs)]

    def list_clients(self, *, active_only=False):
        self._log("list_clients", active_only=active_only)
        rows = sorted(self._clients.values(), key=lambda c: c["name"])
        return [dict(c) for c in rows if not active_only or c["status"] == "active"]

    def get_client(self, name):
        c = self._clients.get(name.strip().lower())
        return dict(c) if c else None

    def create_client(self, name, description=None, *, actor=None, source_ip=None):
        name = name.strip().lower()
        self._log("create_client", name, description=description, actor=actor, source_ip=source_ip)
        cid = self._next_client_id
        self._next_client_id += 1
        self._clients[name] = {"id": cid, "name": name, "description": description, "status": "active"}
        return cid

    def disable_client(self, name, *, actor=None, source_ip=None):
        name = name.strip().lower()
        self._log("disable_client", name, actor=actor, source_ip=source_ip)
        if name not in self._clients:
            raise ClientNotFoundError(name)
        self._clients[name]["status"] = "disabled"

    def enable_client(self, name, *, actor=None, source_ip=None):
        name = name.strip().lower()
        self._log("enable_client", name, actor=actor, source_ip=source_ip)
        if name not in self._clients:
            raise ClientNotFoundError(name)
        self._clients[name]["status"] = "active"

    def create_token(self, name, user_id, *, expires_at=None, actor=None, source_ip=None):
        self._log("create_token", name, user_id, expires_at=expires_at, actor=actor, source_ip=source_ip)
        tid = self._next_token_id
        self._next_token_id += 1
        username = next((u["username"] for u in self._users.values() if u["id"] == user_id), None)
        self._tokens[tid] = {
            "id": tid, "name": name, "user_id": user_id, "username": username, "status": "active",
            "expires_at": expires_at, "last_used_at": None, "created_at": 0,
        }
        return f"amt_faketoken{tid}"

    def list_tokens(self, user_id=None):
        self._log("list_tokens", user_id=user_id)
        rows = sorted(self._tokens.values(), key=lambda t: -t["id"])
        return [dict(t) for t in rows if user_id is None or t["user_id"] == user_id]

    def revoke_token(self, token_id, *, actor=None, source_ip=None):
        self._log("revoke_token", token_id, actor=actor, source_ip=source_ip)
        if token_id not in self._tokens:
            raise TokenNotFoundError(token_id)
        self._tokens[token_id]["status"] = "revoked"

    # verify_login/verify_token are unused by this blueprint but required
    # for _resolve_session's own get_principal-based checks to work; not
    # separately defined -- session resolution only ever calls get_principal.


class FakeMqttPublisher:
    def is_connected(self):
        return True

    def forward_to_mqtt(self, alert_data):
        return True


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
# require_master_admin enforcement
# ---------------------------------------------------------------------------

def test_list_users_redirects_anonymous_browser_request(client):
    resp = client.get("/admin/users")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_list_users_403_for_session_authenticated_non_master_admin(client, store):
    uid = store.seed_user("bob", roles=set())
    _login_session(client, uid, "bob")
    resp = client.get("/admin/users")
    assert resp.status_code == 403


def test_disable_403_for_non_master_admin(client, store):
    store.seed_user("victim")
    uid = store.seed_user("bob", roles=set())
    _login_session(client, uid, "bob")
    resp = _post(client, "/admin/users/victim/disable")
    assert resp.status_code == 403
    assert store.get_user("victim")["status"] == "active"


def test_list_users_200_for_master_admin(client, store):
    _admin(store, client)
    resp = client.get("/admin/users")
    assert resp.status_code == 200


def test_ignores_basic_auth_headers_session_only(client, store):
    store.seed_user("alice", roles={MASTER_ADMIN_ROLE})
    import base64
    encoded = base64.b64encode(b"alice:hunter2").decode()
    resp = client.get("/admin/users", headers={"Authorization": f"Basic {encoded}"})
    assert resp.status_code in (302, 401)
    assert resp.status_code != 200


# ---------------------------------------------------------------------------
# GET /admin/users -- list content
# ---------------------------------------------------------------------------

def test_list_shows_username_roles_status(client, store):
    _admin(store, client)
    store.seed_user("viewer", roles={"acme-viewer"})
    store.seed_role("acme-viewer")

    resp = client.get("/admin/users")
    body = resp.get_data(as_text=True)
    assert "root" in body
    assert "viewer" in body
    assert "acme-viewer" in body


def test_list_never_shows_personal_role_name_as_a_role(client, store):
    # Reviewer finding: list_users() must filter PERSONAL_ROLE_PREFIX names
    # out of the Roles column the same way _selectable_roles() filters the
    # checkbox list -- an admin must not mistake a user's ad-hoc scope
    # bucket ("user:bob") for a real, admin-assignable role.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = client.get("/admin/users")
    body = resp.get_data(as_text=True)
    assert "user:bob" not in body


def test_list_still_shows_personal_role_scopes_in_scopes_column(client, store):
    # The filter above must not silently hide the underlying grant --
    # scopes held only via the personal role still surface in the Scopes
    # column, exactly like a scope granted via a named role would.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = client.get("/admin/users")
    body = resp.get_data(as_text=True)
    assert "acme" in body
    assert "logs:read" in body


def test_list_shows_real_roles_alongside_filtered_personal_role(client, store):
    # Both facts at once: a user holding BOTH a real, admin-assignable role
    # AND a personal scope bucket shows the real role but not the synthetic
    # one.
    _admin(store, client)
    store.seed_user("bob", user_id=2, roles={"acme-viewer"})
    store.seed_role("acme-viewer")
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = client.get("/admin/users")
    body = resp.get_data(as_text=True)
    assert "acme-viewer" in body
    assert "user:bob" not in body


# ---------------------------------------------------------------------------
# No delete route
# ---------------------------------------------------------------------------

def test_no_delete_route_exists(client, store):
    _admin(store, client)
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert not any("delete" in rule for rule in rules if "/admin/users" in rule)
    resp = _post(client, "/admin/users/root/delete")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Username validation on create (create-only)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("username", ["abc", "a1_2-b", "x" * 32, "user_name-1"])
def test_create_accepts_valid_usernames(client, store, username):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": username})
    assert resp.status_code == 200  # renders password_result.html
    assert store.get_user(username) is not None


@pytest.mark.parametrize(
    "username",
    ["ab", "has space", "x" * 33, "has$symbol", ""],
)
def test_create_rejects_invalid_usernames(client, store, username):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": username})
    assert resp.status_code == 400
    assert store.get_user(username.strip().lower()) is None
    assert not store.calls_named("create_user")


def test_create_normalizes_username_before_validating_so_mixed_case_is_accepted(client, store):
    # Matches CLAUDE.md's project-wide "String Normalization" convention
    # (trim + lowercase before validation) and UserStore.create_user()'s
    # own internal normalization -- the regex applies to the NORMALIZED
    # form, so "AB1"/"MixedCase" are accepted and stored lowercase, exactly
    # like alert level/category normalization ("ERROR" -> "critical").
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "MixedCase"})
    assert resp.status_code == 200
    assert store.get_user("mixedcase") is not None
    stored_username = store.calls_named("create_user")[0][0][0]
    assert stored_username == "mixedcase"  # the value actually passed to UserStore.create_user() -- lowercase


def test_create_rejects_duplicate_username(client, store):
    _admin(store, client)
    store.seed_user("taken")
    resp = _post(client, "/admin/users/new", {"username": "taken"})
    assert resp.status_code == 400
    assert len(store.calls_named("create_user")) == 0


def test_edit_does_not_validate_username_regex_since_its_not_editable(client, store):
    # Username isn't a submittable field on the edit form at all -- posting
    # extra/garbage "username" form data must have zero effect (edit never
    # reads it), and the regex-validation 400 path is create-only.
    _admin(store, client)
    store.seed_user("bob", user_id=99)
    resp = _post(client, "/admin/users/bob/edit", {"username": "!!!not-valid!!!", "roles": []})
    assert resp.status_code == 302
    assert store.get_user("bob") is not None
    assert store.get_user("!!!not-valid!!!") is None


# ---------------------------------------------------------------------------
# Passwords: always server-generated, never user-supplied, fresh every time
# ---------------------------------------------------------------------------

def test_create_password_is_never_user_supplied(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "password": "hunter2-i-chose-this"})
    assert resp.status_code == 200
    calls = store.calls_named("create_user")
    assert len(calls) == 1
    (args, kwargs) = calls[0]
    generated_password = args[1]
    assert generated_password != "hunter2-i-chose-this"
    assert "hunter2-i-chose-this" not in resp.get_data(as_text=True)
    assert generated_password in resp.get_data(as_text=True)


def test_create_password_is_fresh_every_request(client, store):
    _admin(store, client)
    _post(client, "/admin/users/new", {"username": "userone"})
    _post(client, "/admin/users/new", {"username": "usertwo"})
    calls = store.calls_named("create_user")
    passwords = [args[1] for args, kwargs in calls]
    assert passwords[0] != passwords[1]


def test_create_response_has_no_store_cache_control(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    assert resp.headers.get("Cache-Control") == "no-store"


def test_reset_password_generates_fresh_password_never_user_supplied(client, store):
    _admin(store, client)
    store.seed_user("target", user_id=55)
    resp = _post(client, "/admin/users/target/reset-password", {"password": "i-want-this-one"})
    assert resp.status_code == 200
    calls = store.calls_named("set_password")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[1] != "i-want-this-one"
    assert kwargs["must_change_password"] is True
    assert resp.headers.get("Cache-Control") == "no-store"


def test_reset_password_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/reset-password")
    assert resp.status_code == 404


def test_reset_own_password_allowed_no_guard_rail(client, store):
    _admin(store, client, username="root")
    resp = _post(client, "/admin/users/root/reset-password")
    assert resp.status_code == 200
    assert len(store.calls_named("set_password")) == 1


# ---------------------------------------------------------------------------
# CSRF requirement
# ---------------------------------------------------------------------------

def test_create_post_without_csrf_token_is_400(client, store):
    _admin(store, client)
    resp = client.post("/admin/users/new", data={"username": "newbie"})
    assert resp.status_code == 400
    assert store.get_user("newbie") is None


def test_disable_post_without_csrf_token_is_400(client, store):
    _admin(store, client)
    store.seed_user("victim", user_id=42)
    resp = client.post("/admin/users/victim/disable", data={})
    assert resp.status_code == 400
    assert store.get_user("victim")["status"] == "active"


def test_edit_post_without_csrf_token_is_400(client, store):
    _admin(store, client)
    store.seed_user("victim", user_id=42)
    resp = client.post("/admin/users/victim/edit", data={"roles": []})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# THE guard-rail matrix -- self-disable
# ---------------------------------------------------------------------------

def test_self_disable_rejected(client, store):
    _admin(store, client, username="root", user_id=1)
    resp = _post(client, "/admin/users/root/disable")
    assert resp.status_code == 400
    assert store.get_user("root")["status"] == "active"
    assert not store.calls_named("disable_user")


def test_disable_someone_else_succeeds(client, store):
    _admin(store, client, username="root", user_id=1)
    store.seed_user("other", user_id=2)
    resp = _post(client, "/admin/users/other/disable")
    assert resp.status_code == 302
    assert store.get_user("other")["status"] == "disabled"


def test_disable_another_master_admin_succeeds(client, store):
    # Guard rail is about YOUR OWN session, not "protect all master_admins".
    _admin(store, client, username="root", user_id=1)
    store.seed_user("root2", user_id=2, roles={MASTER_ADMIN_ROLE})
    resp = _post(client, "/admin/users/root2/disable")
    assert resp.status_code == 302
    assert store.get_user("root2")["status"] == "disabled"


def test_disable_uses_numeric_id_not_username_string_match(client, store):
    # A DIFFERENT account happening to have some superficially-similar
    # username must not accidentally trip the self-guard -- only an exact
    # user_id match against g.principal.user_id does.
    _admin(store, client, username="root", user_id=1)
    store.seed_user("root-imposter", user_id=2)
    resp = _post(client, "/admin/users/root-imposter/disable")
    assert resp.status_code == 302
    assert store.get_user("root-imposter")["status"] == "disabled"


def test_disable_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/disable")
    assert resp.status_code == 404


def test_enable_no_guard_rail_code_exists_for_target(client, store):
    # Enabling is never a lockout risk, so unlike disable/edit there is no
    # is-this-my-own-account check in enable_user() at all -- any
    # authenticated master_admin can enable any disabled target, no
    # special-casing. (A TRUE self-enable-while-disabled scenario can't
    # happen through session auth in the first place:
    # gateway.principal._resolve_session re-checks status == "active" on
    # every request, so a disabled account's own session already fails to
    # resolve before this route's body ever runs -- there is nothing for a
    # self-guard to protect against here.)
    _admin(store, client, username="root", user_id=1)
    store.seed_user("other", user_id=2, status="disabled")
    resp = _post(client, "/admin/users/other/enable")
    assert resp.status_code == 302
    assert store.get_user("other")["status"] == "active"


def test_disabled_own_account_cannot_authenticate_to_self_enable(client, store):
    # Directly demonstrates the claim above: flip root's OWN status to
    # disabled (as if a second admin had done it moments ago) and confirm
    # root's EXISTING session can no longer reach ANY admin route at all --
    # session resolution itself now fails, redirecting to /login rather
    # than reaching enable_user's body.
    _admin(store, client, username="root", user_id=1)
    store._users["root"]["status"] = "disabled"
    resp = _post(client, "/admin/users/root/enable")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")
    assert store.get_user("root")["status"] == "disabled"  # enable_user's body never ran


def test_enable_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/enable")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# THE guard-rail matrix -- self de-admin (master_admin role removal)
# ---------------------------------------------------------------------------

def test_self_master_admin_removal_rejected(client, store):
    _admin(store, client, username="root", user_id=1)
    resp = _post(client, "/admin/users/root/edit", {"roles": []})
    assert resp.status_code == 400
    assert MASTER_ADMIN_ROLE in store.get_principal("root").roles
    assert not store.calls_named("revoke_role")


def test_self_edit_keeping_master_admin_checked_succeeds(client, store):
    _admin(store, client, username="root", user_id=1)
    resp = _post(client, "/admin/users/root/edit", {"roles": [MASTER_ADMIN_ROLE]})
    assert resp.status_code == 302
    assert MASTER_ADMIN_ROLE in store.get_principal("root").roles


def test_deadmin_someone_else_succeeds(client, store):
    _admin(store, client, username="root", user_id=1)
    store.seed_user("root2", user_id=2, roles={MASTER_ADMIN_ROLE})
    resp = _post(client, "/admin/users/root2/edit", {"roles": []})
    assert resp.status_code == 302
    assert MASTER_ADMIN_ROLE not in store.get_principal("root2").roles
    assert ("root2", MASTER_ADMIN_ROLE) in [(a[0], a[1]) for a, k in store.calls_named("revoke_role")]


def test_deadmin_uses_real_role_membership_not_scope_inference(client, store):
    # Mirrors Task 22's exact review lesson: a principal can hold a
    # client='*' WILDCARD SCOPE without holding the master_admin ROLE.
    # Editing that principal and unchecking every role checkbox (there
    # were none to begin with) must NOT trip the self-guard, because the
    # guard checks MASTER_ADMIN_ROLE in current.roles -- never
    # visible_clients()/has_permission()/any scope-derived proxy.
    _admin(store, client, username="root", user_id=1)
    wildcard_uid = store.seed_user("wildcard-reader", user_id=2, roles=set())
    store.seed_scope("wildcard-reader-role", "*", "logs:read")
    store._user_roles["wildcard-reader"].add("wildcard-reader-role")
    _login_session(client, wildcard_uid, "wildcard-reader")
    # Log back in as root (master_admin) to perform the edit -- session
    # must belong to the ADMIN doing the editing, not the target.
    _admin(store, client, username="root", user_id=1)

    resp = _post(client, "/admin/users/wildcard-reader/edit", {"roles": []})
    assert resp.status_code == 302  # not the self-guard's 400 -- this is not root's own account
    assert store.get_principal("wildcard-reader").roles == frozenset()


def test_target_not_self_even_with_matching_username_string_but_different_id_is_impossible_but_id_is_authoritative(
    client, store
):
    # Directly proves identity is resolved by user_id, not string identity:
    # construct the exact "self" case and confirm the guard fires only
    # because the ids match, then flip to a non-matching id and confirm it
    # doesn't (covered by test_disable_uses_numeric_id_not_username_string_match
    # above for disable; this is the edit-route equivalent).
    _admin(store, client, username="root", user_id=1)
    resp_self = _post(client, "/admin/users/root/edit", {"roles": []})
    assert resp_self.status_code == 400

    store.seed_user("someone-else", user_id=999)
    resp_other = _post(client, "/admin/users/someone-else/edit", {"roles": []})
    assert resp_other.status_code == 302


def test_edit_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/edit", {"roles": []})
    assert resp.status_code == 404


def test_edit_unknown_role_rejected(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/edit", {"roles": ["totally-made-up-role"]})
    assert resp.status_code == 400
    assert store.get_principal("bob").roles == frozenset()


def test_create_unknown_role_rejected(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "roles": ["nonexistent-role"]})
    assert resp.status_code == 400
    assert store.get_user("newbie") is None


def test_create_invalid_permission_rejected(client, store):
    _admin(store, client)
    resp = _post(
        client, "/admin/users/new",
        {"username": "newbie", "scope_client": ["acme"], "scope_permissions_0": ["not-a-real-permission"]},
    )
    assert resp.status_code == 400
    assert store.get_user("newbie") is None


def test_edit_invalid_permission_rejected(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(
        client, "/admin/users/bob/edit",
        {"roles": [], "scope_client": ["acme"], "scope_permissions_0": ["not-a-real-permission"]},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Personal-role scope reconciliation (module docstring section 4)
# ---------------------------------------------------------------------------

def test_create_with_scope_row_creates_personal_role_and_grants_it(client, store):
    _admin(store, client)
    store.seed_client("acme")
    resp = _post(
        client, "/admin/users/new",
        {"username": "scoped", "scope_client": ["acme"], "scope_permissions_0": ["logs:read", "alerts:write"]},
    )
    assert resp.status_code == 200
    personal_role = "user:scoped"
    assert store.get_role(personal_role) is not None
    assert MASTER_ADMIN_ROLE not in store.get_principal("scoped").roles
    assert "scoped" not in [r["name"] for r in store.list_roles() if not r["name"].startswith("user:")]
    scopes = store.get_principal("scoped").scopes
    assert scopes["acme"] == frozenset({"logs:read", "alerts:write"})


def test_create_without_scope_rows_never_creates_a_personal_role(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "plain"})
    assert resp.status_code == 200
    assert store.get_role("user:plain") is None
    assert len(store.calls_named("create_role")) == 0


def test_edit_adds_new_scope_and_removes_unchecked_scope(client, store):
    _admin(store, client)
    store.seed_client("acme")
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = _post(
        client, "/admin/users/bob/edit",
        {"roles": [], "scope_client": ["acme"], "scope_permissions_0": ["alerts:write"]},
    )
    assert resp.status_code == 302

    scopes = store.get_principal("bob").scopes
    assert scopes.get("acme", frozenset()) == frozenset({"alerts:write"})
    assert ("user:bob", "acme", "logs:read") in [
        (a[0], a[1], a[2]) for a, k in store.calls_named("remove_scope")
    ]
    assert ("user:bob", "acme", "alerts:write") in [
        (a[0], a[1], a[2]) for a, k in store.calls_named("add_scope")
    ]


def test_edit_unchanged_scopes_are_not_touched(client, store):
    _admin(store, client)
    store.seed_client("acme")
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = _post(
        client, "/admin/users/bob/edit",
        {"roles": [], "scope_client": ["acme"], "scope_permissions_0": ["logs:read"]},
    )
    assert resp.status_code == 302
    assert len(store.calls_named("add_scope")) == 0
    assert len(store.calls_named("remove_scope")) == 0


def test_edit_removing_all_scope_rows_revokes_the_now_empty_personal_role(client, store):
    # Reviewer's minor/optional finding: an edit that empties out every
    # scope row must not leave the personal role dangling, granted-but-
    # empty, forever -- the now-pointless grant is revoked.
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = _post(client, "/admin/users/bob/edit", {"roles": []})  # no scope_client rows submitted at all
    assert resp.status_code == 302

    assert "user:bob" not in store.get_principal("bob").roles
    revoke_calls = [(a[0], a[1]) for a, k in store.calls_named("revoke_role")]
    assert ("bob", "user:bob") in revoke_calls
    # The role itself isn't deleted (UserStore has no route-exposed delete
    # here) -- only ungranted.
    assert store.get_role("user:bob") is not None


def test_edit_revoking_empty_personal_role_is_a_noop_when_it_never_existed(client, store):
    _admin(store, client)
    store.seed_user("plain", user_id=2)

    resp = _post(client, "/admin/users/plain/edit", {"roles": []})
    assert resp.status_code == 302
    assert len(store.calls_named("revoke_role")) == 0


def test_get_edit_form_prepopulates_existing_scopes(client, store):
    _admin(store, client)
    store.seed_client("acme")
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = client.get("/admin/users/bob/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "acme" in body


def test_personal_role_never_shown_as_selectable_role_checkbox(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")

    resp = client.get("/admin/users/new")
    body = resp.get_data(as_text=True)
    assert 'value="user:bob"' not in body


# ---------------------------------------------------------------------------
# Clients registry (sql/migrations/0004_clients.sql) feeding the user form's
# Default client / Scope client <select> options -- supersedes the former
# best-effort "distinct clients" datalist now that there's a real registry.
# ---------------------------------------------------------------------------

def test_user_form_offers_registered_active_clients_as_select_options(client, store):
    _admin(store, client)
    store.seed_client("acme")
    store.seed_client("revysmc")

    resp = client.get("/admin/users/new")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert 'value="acme"' in body
    assert 'value="revysmc"' in body


def test_user_form_excludes_disabled_clients(client, store):
    _admin(store, client)
    store.seed_client("acme")
    store.seed_client("retired-client", status="disabled")

    resp = client.get("/admin/users/new")
    body = resp.get_data(as_text=True)
    assert 'value="acme"' in body
    assert 'value="retired-client"' not in body


def test_user_form_reads_clients_via_userstore_list_clients(client, store):
    _admin(store, client)
    store.seed_client("acme")

    resp = client.get("/admin/users/new")
    assert resp.status_code == 200
    assert len(store.calls_named("list_clients")) >= 1


def test_create_rejects_scope_row_with_unregistered_client(client, store):
    _admin(store, client)
    resp = _post(
        client, "/admin/users/new",
        {"username": "newbie", "scope_client": ["ghost-client"], "scope_permissions_0": ["logs:read"]},
    )
    assert resp.status_code == 400
    assert store.get_user("newbie") is None


def test_create_rejects_default_client_not_registered(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "default_client": "ghost-client"})
    assert resp.status_code == 400
    assert store.get_user("newbie") is None


def test_create_accepts_wildcard_scope_client_without_registry_check(client, store):
    # '*' is never a `clients` table row -- it stays exempt from the
    # registry check, exactly like the pre-existing permission/authz model.
    _admin(store, client)
    resp = _post(
        client, "/admin/users/new",
        {"username": "adminish", "scope_client": ["*"], "scope_permissions_0": ["*"]},
    )
    assert resp.status_code == 200
    assert store.get_principal("adminish").scopes["*"] == frozenset({"*"})


def test_edit_rejects_scope_row_with_unregistered_client(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(
        client, "/admin/users/bob/edit",
        {"roles": [], "scope_client": ["ghost-client"], "scope_permissions_0": ["logs:read"]},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# _role_scope_rows(): reads via UserStore, not raw SQL (final whole-branch
# review's "UserStore single owner" fix -- see module docstring above)
# ---------------------------------------------------------------------------

def test_edit_form_reads_scopes_via_userstore_list_role_scopes(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_scope("user:bob", "acme", "logs:read")
    store._user_roles["bob"].add("user:bob")
    role_id = store.get_role("user:bob")["id"]

    resp = client.get("/admin/users/bob/edit")
    assert resp.status_code == 200
    assert (role_id,) in [a for a, k in store.calls_named("list_role_scopes")]


# ---------------------------------------------------------------------------
# UserStore call-pattern discipline: handlers call UserStore directly, no
# duplicated business logic (e.g. permission validity is UserStore's own
# ALLOWED_PERMISSIONS -- this file doesn't hardcode a second list).
# ---------------------------------------------------------------------------

def test_create_calls_grant_role_for_each_selected_role_and_nothing_else_role_related(client, store):
    _admin(store, client)
    store.seed_role("acme-viewer")
    resp = _post(client, "/admin/users/new", {"username": "newbie", "roles": ["acme-viewer"]})
    assert resp.status_code == 200
    grant_calls = [(a[0], a[1]) for a, k in store.calls_named("grant_role")]
    assert ("newbie", "acme-viewer") in grant_calls


# ---------------------------------------------------------------------------
# Bearer tokens: issued at creation time, and via the two new
# /tokens/new + /tokens/<id>/revoke routes on an existing account.
# ---------------------------------------------------------------------------

def test_create_with_issue_token_creates_token_and_shows_it(client, store):
    _admin(store, client)
    resp = _post(
        client, "/admin/users/new",
        {"username": "newbie", "issue_token": "on", "token_name": "ci-runner"},
    )
    assert resp.status_code == 200
    calls = store.calls_named("create_token")
    assert len(calls) == 1
    (args, kwargs) = calls[0]
    token_name, user_id = args
    assert token_name == "ci-runner"
    assert user_id == store.get_user("newbie")["id"]

    body = resp.get_data(as_text=True)
    password = store.calls_named("create_user")[0][0][1]
    generated_token = "amt_faketoken" + str(store.list_tokens(user_id=user_id)[0]["id"])
    assert generated_token in body
    assert generated_token != password


def test_create_without_issue_token_never_creates_a_token(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie"})
    assert resp.status_code == 200
    assert not store.calls_named("create_token")
    assert 'id="generated-token"' not in resp.get_data(as_text=True)


def test_create_issue_token_blank_name_defaults_to_default(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "issue_token": "on", "token_name": ""})
    assert resp.status_code == 200
    (args, kwargs) = store.calls_named("create_token")[0]
    assert args[0] == "default"


def test_create_service_account_checkbox_has_no_serverside_effect(client, store):
    # "Service account" is a purely client-side (JS) convenience toggle --
    # nothing in create_user() reads it at all.
    _admin(store, client)
    resp = _post(client, "/admin/users/new", {"username": "newbie", "service_account": "on"})
    assert resp.status_code == 200
    assert store.get_user("newbie") is not None
    assert not store.calls_named("create_token")


def test_edit_get_passes_tokens_to_template(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    store.seed_token("prod-key", "bob")

    resp = client.get("/admin/users/bob/edit")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "prod-key" in body
    assert (2,) in [a for a, k in store.calls_named("list_tokens")] or {"user_id": 2} in [
        k for a, k in store.calls_named("list_tokens")
    ]


def test_issue_token_route_creates_token_for_existing_user(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/new", {"token_name": "svc"})
    assert resp.status_code == 200
    (args, kwargs) = store.calls_named("create_token")[0]
    assert args == ("svc", 2)
    body = resp.get_data(as_text=True)
    assert 'id="generated-token"' in body
    assert 'id="generated-password"' not in body
    assert resp.headers.get("Cache-Control") == "no-store"


def test_issue_token_route_defaults_blank_name(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/new", {"token_name": ""})
    assert resp.status_code == 200
    (args, kwargs) = store.calls_named("create_token")[0]
    assert args[0] == "default"


def test_issue_token_route_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/tokens/new", {"token_name": "svc"})
    assert resp.status_code == 404
    assert not store.calls_named("create_token")


def test_issue_token_route_requires_csrf(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = client.post("/admin/users/bob/tokens/new", data={"token_name": "svc"})
    assert resp.status_code == 400
    assert not store.calls_named("create_token")


def test_revoke_token_route_happy_path(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    token_id = store.seed_token("prod-key", "bob")
    resp = _post(client, f"/admin/users/bob/tokens/{token_id}/revoke")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/admin/users/bob/edit")
    assert store.list_tokens(user_id=2)[0]["status"] == "revoked"
    assert (token_id,) in [a for a, k in store.calls_named("revoke_token")]


def test_revoke_token_route_wrong_owner_rejected(client, store):
    _admin(store, client)
    store.seed_user("alice", user_id=2)
    store.seed_user("bob", user_id=3)
    alice_token_id = store.seed_token("alice-key", "alice")

    resp = _post(client, f"/admin/users/bob/tokens/{alice_token_id}/revoke")
    assert resp.status_code == 404
    assert not store.calls_named("revoke_token")
    assert store.list_tokens(user_id=2)[0]["status"] == "active"


def test_revoke_token_route_404_for_unknown_user(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/users/ghost/tokens/1/revoke")
    assert resp.status_code == 404
    assert not store.calls_named("revoke_token")


def test_revoke_token_route_404_for_unknown_token_id(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/99999/revoke")
    assert resp.status_code == 404
    assert not store.calls_named("revoke_token")


def test_revoke_token_route_404_for_non_numeric_token_id(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    resp = _post(client, "/admin/users/bob/tokens/not-a-number/revoke")
    assert resp.status_code == 404


def test_revoke_token_route_requires_csrf(client, store):
    _admin(store, client)
    store.seed_user("bob", user_id=2)
    token_id = store.seed_token("prod-key", "bob")
    resp = client.post(f"/admin/users/bob/tokens/{token_id}/revoke", data={})
    assert resp.status_code == 400
    assert not store.calls_named("revoke_token")


def test_no_token_delete_route_exists(client, store):
    _admin(store, client)
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert not any("/tokens/" in rule and "delete" in rule for rule in rules)


# ---------------------------------------------------------------------------
# /admin/clients -- the clients registry admin UI (sql/migrations/
# 0004_clients.sql): GET list, GET/POST new, POST disable/enable. Same
# master_admin/CSRF/400-re-render conventions as every route above; no
# self-lockout-style guard rail (a client has no "self" concept).
# ---------------------------------------------------------------------------

def test_list_clients_redirects_anonymous_browser_request(client):
    resp = client.get("/admin/clients")
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_list_clients_403_for_non_master_admin(client, store):
    uid = store.seed_user("bob", roles=set())
    _login_session(client, uid, "bob")
    resp = client.get("/admin/clients")
    assert resp.status_code == 403


def test_list_clients_200_for_master_admin(client, store):
    _admin(store, client)
    resp = client.get("/admin/clients")
    assert resp.status_code == 200


def test_list_clients_shows_name_description_status(client, store):
    _admin(store, client)
    store.seed_client("acme", description="Acme Corp")
    store.seed_client("retired-client", status="disabled")

    resp = client.get("/admin/clients")
    body = resp.get_data(as_text=True)
    assert "acme" in body
    assert "Acme Corp" in body
    assert "retired-client" in body


def test_create_client_valid_name_succeeds(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/new", {"name": "acme", "description": "Acme Corp"})
    assert resp.status_code == 302
    row = store.get_client("acme")
    assert row is not None
    assert row["description"] == "Acme Corp"
    assert row["status"] == "active"


def test_create_client_normalizes_name(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/new", {"name": "  ACME  "})
    assert resp.status_code == 302
    assert store.get_client("acme") is not None


def test_create_client_rejects_invalid_name(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/new", {"name": "has space"})
    assert resp.status_code == 400
    assert store.get_client("has space") is None


def test_create_client_rejects_duplicate_name(client, store):
    _admin(store, client)
    store.seed_client("acme")
    resp = _post(client, "/admin/clients/new", {"name": "acme"})
    assert resp.status_code == 400


def test_create_client_rejects_literal_wildcard_name(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/new", {"name": "*"})
    assert resp.status_code == 400
    assert store.get_client("*") is None


def test_create_client_post_without_csrf_token_is_400(client, store):
    _admin(store, client)
    resp = client.post("/admin/clients/new", data={"name": "acme"})
    assert resp.status_code == 400
    assert store.get_client("acme") is None


def test_disable_client_succeeds(client, store):
    _admin(store, client)
    store.seed_client("acme")
    resp = _post(client, "/admin/clients/acme/disable")
    assert resp.status_code == 302
    assert store.get_client("acme")["status"] == "disabled"


def test_disable_client_404_for_unknown_client(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/ghost/disable")
    assert resp.status_code == 404


def test_enable_client_succeeds(client, store):
    _admin(store, client)
    store.seed_client("acme", status="disabled")
    resp = _post(client, "/admin/clients/acme/enable")
    assert resp.status_code == 302
    assert store.get_client("acme")["status"] == "active"


def test_enable_client_404_for_unknown_client(client, store):
    _admin(store, client)
    resp = _post(client, "/admin/clients/ghost/enable")
    assert resp.status_code == 404


def test_disable_client_post_without_csrf_token_is_400(client, store):
    _admin(store, client)
    store.seed_client("acme")
    resp = client.post("/admin/clients/acme/disable", data={})
    assert resp.status_code == 400
    assert store.get_client("acme")["status"] == "active"


def test_no_delete_route_exists_for_clients(client, store):
    _admin(store, client)
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert not any("delete" in rule for rule in rules if "/admin/clients" in rule)
    resp = _post(client, "/admin/clients/acme/delete")
    assert resp.status_code == 404
