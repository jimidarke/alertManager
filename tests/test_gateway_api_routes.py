"""
Tests for src/gateway/api/routes.py (Task 20): the `api` blueprint
(`GET /health`, `POST /alert`, `POST /test`, `GET /docs`,
`GET /docs/<filename>`), wired through the real `create_app()` (so
ProxyFix/CSRFProtect/CredentialCache/blueprint registration are exactly
what production gets), with Postgres/MQTT mocked exactly as
tests/test_gateway_app.py establishes and a FakeUserStore/FakeMqttPublisher
swapped onto app.extensions afterward -- no live Postgres/MQTT is available
in this environment (and per project rule, docker/docker-compose is never
invoked from automation).

Covers:
- Payload validation parity with the old http_gateway.py `/alert` (missing
  Content-Type, invalid JSON, empty payload, missing required fields, a
  fully valid request).
- The breaking `POST /alert` authorization change and its full matrix:
  scope match/mismatch, default_client inheritance, and the clientless
  (`''`) + master_admin edge case (including the "explicit clientless scope
  without master_admin" case the rule exists specifically to close).
- CredentialCache composition for `/alert`: fresh hit (no re-verify), miss,
  stale-recovers, stale-serves-on-DB-exception, beyond-grace -> 503,
  different-credential-for-same-key -> miss (not a stale reuse).
- `/health` fail-open on a DB blip, fail-closed (503) on MQTT disconnect.
- MQTT forwarding via a fake `MqttPublisher` (success and failure paths).
- CSRF exemption: a POST with no csrf_token at all succeeds.
- `/docs` (auth required) and `/docs/<f>` (public) serving + traversal
  rejection, ported from http_gateway.py.
"""

import base64
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.auth.cache import CredentialCache  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402
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
        self._tokens = {}  # token -> PrincipalData
        self.login_calls = []
        self.token_calls = []
        self.login_exception = None
        self.token_exception = None
        # None = "open" registry: client_exists() returns True for any
        # name -- the default for every test above that predates the
        # clients-registry feature and never calls add_client(). Calling
        # add_client() switches this store to "closed" registry behavior
        # (only registered names/statuses answer True) for tests that
        # actually exercise unknown-client rejection.
        self._clients = None

    def add_user(self, username, password, **kwargs) -> PrincipalData:
        data = _principal_data(username=username, **kwargs)
        self._users[username] = {"password": password, "data": data}
        return data

    def add_token(self, token, **kwargs) -> PrincipalData:
        data = _principal_data(**kwargs)
        self._tokens[token] = data
        return data

    def verify_login(self, username, password, *, source_ip=None):
        self.login_calls.append((username, password, source_ip))
        if self.login_exception is not None:
            raise self.login_exception
        entry = self._users.get(username)
        if entry is None or entry["password"] != password or entry["data"].status != "active":
            return None
        return entry["data"]

    def verify_token(self, token):
        self.token_calls.append(token)
        if self.token_exception is not None:
            raise self.token_exception
        return self._tokens.get(token)

    def add_client(self, name, status="active"):
        if self._clients is None:
            self._clients = {}
        self._clients[name] = status

    def client_exists(self, name, *, active_only=True):
        if self._clients is None:
            return True
        status = self._clients.get(name)
        if status is None:
            return False
        return not active_only or status == "active"


class FakeMqttPublisher:
    """Controllable stand-in for gateway.mqtt.MqttPublisher -- no real MQTT client."""

    def __init__(self, connected=True, forward_result=True):
        self._connected = connected
        self.forward_result = forward_result
        self.published = []

    def is_connected(self):
        return self._connected

    def forward_to_mqtt(self, alert_data):
        self.published.append(alert_data)
        return self.forward_result


class FakeClock:
    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


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
def mqtt(app) -> FakeMqttPublisher:
    fake = FakeMqttPublisher()
    app.extensions["mqtt_publisher"] = fake
    return fake


@pytest.fixture
def cache_clock():
    return FakeClock()


@pytest.fixture
def cache(app, cache_clock) -> CredentialCache:
    fresh = CredentialCache(clock=cache_clock)
    app.extensions["credential_cache"] = fresh
    return fresh


@pytest.fixture
def client(app):
    return app.test_client()


def _basic(username, password):
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


VALID_ALERT = {
    "timestamp": 1732982400,
    "level": "info",
    "category": "application",
    "title": "Status",
    "message": "OK",
    "source": "my-service",
}


# ---------------------------------------------------------------------------
# POST /alert -- payload validation parity with http_gateway.py
# ---------------------------------------------------------------------------

def test_alert_rejects_non_json_content_type(client, store):
    # default_client="revysmc" (matching the node's own scope) so this test
    # isolates the Content-Type check from the authorization step: with a
    # non-JSON body, `request.get_json(silent=True)` (used by the
    # authorization decorator's body-based client resolution, per
    # principal.py's own documented pattern) can't read a `client` field at
    # all and falls back to `principal.default_client` -- see this module's
    # own docstring / task-20-report.md for why a malformed request from a
    # principal with NO matching default_client scope instead gets 403
    # (authorization runs before the view's own body validation, an
    # unavoidable consequence of decorator-based, body-based authorization).
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post(
        "/alert", data="not json", content_type="text/plain", headers=_basic("node1", "secret")
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Content-Type must be application/json"


def test_alert_malformed_body_from_principal_without_a_matching_fallback_scope_is_403_not_400(client, store):
    # The flip side of the test above, made explicit: authorization
    # (`_require_alert_permission`) necessarily runs before this view's own
    # Content-Type/JSON validation, since it must read `client` from the
    # body to know what to authorize -- a non-JSON body means
    # `get_json(silent=True)` yields nothing, the effective client falls
    # back to `default_client` (None here) then `''`, and this node has no
    # scope for `''`. Documented as a deliberate, unavoidable consequence of
    # the specified design, not a defect: the request is still rejected
    # either way, just with a different status/message than a plain 400.
    store.add_user("node1", "secret", default_client=None, scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post(
        "/alert", data="not json", content_type="text/plain", headers=_basic("node1", "secret")
    )
    assert resp.status_code == 403


def test_alert_rejects_malformed_json(client, store):
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post(
        "/alert", data="{not valid json", content_type="application/json", headers=_basic("node1", "secret")
    )
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid JSON format"


def test_alert_rejects_empty_json_object(client, store):
    # Matches http_gateway.py's exact (if slightly surprising) precedence:
    # `{}` is falsy in Python, so the "empty payload" check fires before
    # the required-fields check ever runs -- ported verbatim, not "fixed".
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post("/alert", json={}, headers=_basic("node1", "secret"))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Empty JSON payload"


def test_alert_rejects_missing_required_fields(client, store):
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post("/alert", json={"timestamp": 1234567890, "level": "info"}, headers=_basic("node1", "secret"))
    assert resp.status_code == 400
    body = resp.get_json()
    assert "Missing required fields" in body["error"]
    assert body["required_fields"] == ["timestamp", "level", "category", "title", "message", "source"]


def test_alert_accepts_a_fully_valid_payload(client, store, mqtt):
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "success"
    assert "alert_id" in resp.get_json()


def test_alert_500_when_mqtt_forward_fails(client, store, mqtt):
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    mqtt.forward_result = False
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 500
    assert resp.get_json()["status"] == "error"


# ---------------------------------------------------------------------------
# POST /alert -- authentication failures (401, JSON via the after_request hook)
# ---------------------------------------------------------------------------

def test_alert_401_json_when_no_credentials(client, store):
    resp = client.post("/alert", json=VALID_ALERT)
    assert resp.status_code == 401
    assert resp.headers["WWW-Authenticate"] == 'Basic realm="Alert Gateway"'
    assert resp.get_json() == {
        "status": "error",
        "error": "authentication required",
        "timestamp": resp.get_json()["timestamp"],
    }


def test_alert_401_json_when_bad_credentials(client, store):
    store.add_user("node1", "secret")
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "wrong-password"))
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "authentication required"


# ---------------------------------------------------------------------------
# POST /alert -- the breaking authorization change
# ---------------------------------------------------------------------------

def test_alert_success_when_payload_client_matches_scope(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


def test_alert_normalizes_payload_client_before_authorizing(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="  RevySMC  ")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


def test_alert_403_when_payload_client_not_in_scope(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="some-other-client")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
    assert mqtt.published == []


def test_alert_inherits_default_client_when_payload_omits_client(client, store, mqtt):
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


def test_alert_default_client_used_only_when_payload_client_is_empty(client, store, mqtt):
    # payload client == "" (falsy) -> falls back to default_client, exactly
    # like http_gateway.py's own inheritance rule.
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


def test_alert_whitespace_only_payload_client_falls_back_to_default_client(client, store, mqtt):
    # Regression test (review finding): a whitespace-only payload `client`
    # (e.g. "   ") is a truthy Python string, so a naive `or` fallback
    # chain would treat it as "present" and never inherit default_client --
    # unlike http_gateway.py's own check (`alert_data.get('client', '')
    # .strip() == ''`), which treats missing/empty/whitespace-only alike.
    # _alert_effective_client() must pre-strip the raw payload value before
    # the fallback decision, not just normalize afterward.
    store.add_user("node1", "secret", default_client="revysmc", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="   ")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


def test_alert_403_when_default_client_has_no_matching_scope(client, store, mqtt):
    # Matrix case: no payload client (falls back to default_client), but
    # the principal's only scope is for a DIFFERENT client than its own
    # default_client -- must still be denied, not silently authorized by
    # virtue of merely having *some* scope somewhere.
    store.add_user(
        "node1", "secret", default_client="somewhere-else", scopes={"revysmc": frozenset({"alerts:write"})}
    )
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 403
    assert mqtt.published == []


def test_alert_clientless_403_for_regular_user_without_wildcard_scope(client, store, mqtt):
    # Closes "any node can spoof any client": no client in payload, no
    # default_client, and the node's only scope is for a named client --
    # effective client is '' and has_permission('alerts:write', '') is False.
    store.add_user("node1", "secret", default_client=None, scopes={"revysmc": frozenset({"alerts:write"})})
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 403
    assert mqtt.published == []


def test_alert_clientless_403_even_with_explicit_empty_client_scope_if_not_master_admin(client, store, mqtt):
    # The specific rule this task's brief calls out: has_permission() alone
    # is not sufficient for the clientless case. A non-master_admin role
    # could (e.g. via misconfiguration) hold an explicit client=='' scope;
    # that must still be rejected unless the principal is master_admin.
    store.add_user(
        "node1", "secret", default_client=None, roles=frozenset(), scopes={"": frozenset({"alerts:write"})}
    )
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("node1", "secret"))
    assert resp.status_code == 403
    assert mqtt.published == []


def test_alert_clientless_succeeds_for_master_admin(client, store, mqtt):
    store.add_user(
        "root", "secret", default_client=None, roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})}
    )
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("root", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == ""
    assert mqtt.published[0]["_gateway"]["user_type"] == "master_admin"
    assert mqtt.published[0]["_gateway"]["authenticated_user"] == "root"


def test_alert_master_admin_can_still_use_an_explicit_client(client, store, mqtt):
    store.add_user(
        "root", "secret", default_client=None, roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})}
    )
    payload = dict(VALID_ALERT, client="anything")
    resp = client.post("/alert", json=payload, headers=_basic("root", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "anything"


def test_alert_success_via_bearer_token(client, store, mqtt):
    store.add_token("amt_sometoken", username="node2", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")
    resp = client.post("/alert", json=payload, headers=_bearer("amt_sometoken"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


# ---------------------------------------------------------------------------
# POST /alert -- the clients registry (sql/migrations/0004_clients.sql):
# an authorized-but-unregistered client is now rejected with 400, not
# silently accepted. store.add_client(...) below switches FakeUserStore
# from its default "open" registry (client_exists() always True, matching
# every test above written before this feature) into "closed" registry
# behavior for these tests specifically.
# ---------------------------------------------------------------------------

def test_alert_400_unknown_client_not_in_registry(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"*": frozenset({"alerts:write"})})
    store.add_client("revysmc")  # some OTHER client is registered; "acme" is not
    payload = dict(VALID_ALERT, client="acme")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["error"] == "Unknown client"
    assert body["client"] == "acme"
    assert mqtt.published == []


def test_alert_400_disabled_client_rejected(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"*": frozenset({"alerts:write"})})
    store.add_client("acme", status="disabled")
    payload = dict(VALID_ALERT, client="acme")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Unknown client"
    assert mqtt.published == []


def test_alert_403_before_400_when_both_unauthorized_and_unregistered(client, store, mqtt):
    # Existence is checked AFTER authorization: a principal with no
    # matching scope for "acme" gets the existing 403, never reaching (or
    # revealing anything about) the registry check -- even though "acme"
    # also happens not to be registered.
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    store.add_client("revysmc")  # registry is "closed" now; "acme" is not in it either
    payload = dict(VALID_ALERT, client="acme")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "forbidden"
    assert mqtt.published == []


def test_alert_clientless_never_checked_against_registry(client, store, mqtt):
    # Clientless ('') is a distinct, already-authorized valid state (master
    # admin only) -- never checked against the registry, even when the
    # registry is closed and empty.
    store.add_user(
        "root", "secret", default_client=None, roles=frozenset({"master_admin"}), scopes={"*": frozenset({"*"})}
    )
    store.add_client("some-other-client")  # registry is "closed"; nothing named '' could ever be in it
    resp = client.post("/alert", json=VALID_ALERT, headers=_basic("root", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == ""


def test_alert_success_when_client_registered_and_active(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    store.add_client("revysmc")
    payload = dict(VALID_ALERT, client="revysmc")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert mqtt.published[0]["client"] == "revysmc"


# ---------------------------------------------------------------------------
# CSRF exemption (Task 19's csrf instance, exempted in gateway/api/__init__.py)
# ---------------------------------------------------------------------------

def test_alert_succeeds_with_no_csrf_token_at_all(client, store, mqtt):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    # A CSRF rejection would be 400 with Flask-WTF's own error page/message;
    # a clean 200 proves this blueprint's POST routes were never subject to
    # CSRF checking in the first place.
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# CredentialCache composition -- fresh/miss/stale/unavailable, for /alert specifically
# ---------------------------------------------------------------------------

def test_alert_cache_fresh_hit_does_not_call_verify_login_again(client, store, mqtt, cache, cache_clock):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")

    resp1 = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp1.status_code == 200
    assert len(store.login_calls) == 1

    cache_clock.advance(30)  # well within FRESH_TTL (60s)
    resp2 = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp2.status_code == 200
    assert len(store.login_calls) == 1  # served from cache -- verify_login NOT called again


def test_alert_cache_stale_within_grace_reverifies_and_refreshes(client, store, mqtt, cache, cache_clock):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")

    client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert len(store.login_calls) == 1

    cache_clock.advance(61)  # past FRESH_TTL, within STALE_GRACE (300s)
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert resp.status_code == 200
    assert len(store.login_calls) == 2  # stale -> live check re-attempted


def test_alert_cache_stale_serves_cached_result_on_db_exception(client, store, mqtt, cache, cache_clock):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")

    client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert len(store.login_calls) == 1

    cache_clock.advance(61)  # stale, within grace
    store.login_exception = RuntimeError("db unreachable")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))

    assert resp.status_code == 200  # served from the stale cached entry
    assert mqtt.published[-1]["client"] == "revysmc"


def test_alert_cache_beyond_grace_with_db_exception_returns_503(client, store, mqtt, cache, cache_clock):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")

    client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert len(store.login_calls) == 1

    cache_clock.advance(301)  # beyond STALE_GRACE
    store.login_exception = RuntimeError("db unreachable")
    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))

    assert resp.status_code == 503
    assert resp.get_json()["status"] == "error"
    assert len(mqtt.published) == 1  # only the first (successful) request published -- not this one


def test_alert_cache_miss_with_no_prior_entry_and_db_exception_returns_503(client, store, mqtt, cache):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    store.login_exception = RuntimeError("db unreachable")
    payload = dict(VALID_ALERT, client="revysmc")

    resp = client.post("/alert", json=payload, headers=_basic("node1", "secret"))

    assert resp.status_code == 503
    assert mqtt.published == []


def test_alert_cache_different_password_for_same_username_is_a_miss_not_a_stale_reuse(client, store, mqtt, cache):
    store.add_user("node1", "secret", scopes={"revysmc": frozenset({"alerts:write"})})
    payload = dict(VALID_ALERT, client="revysmc")

    client.post("/alert", json=payload, headers=_basic("node1", "secret"))
    assert len(store.login_calls) == 1

    # Different (wrong) password presented for the same key -- must MISS
    # and call verify_login fresh (and fail), not serve the cached-good
    # entry for the previously-correct password.
    resp = client.post("/alert", json=payload, headers=_basic("node1", "wrong-password"))
    assert resp.status_code == 401
    assert len(store.login_calls) == 2


# ---------------------------------------------------------------------------
# GET /health -- fail-open on DB, fail-closed on MQTT
# ---------------------------------------------------------------------------

def test_health_200_when_mqtt_connected_and_db_ok(client, mqtt):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["mqtt_connected"] is True
    assert body["mqtt"] == {"connected": True}
    assert body["db"]["status"] == "ok"


def test_health_stays_200_on_a_db_blip(client, app, mqtt):
    app.extensions["db_pool"].getconn.side_effect = Exception("connection refused")
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["db"]["status"] == "error"
    assert "connection refused" in body["db"]["detail"]


def test_health_503_when_mqtt_disconnected(client, app):
    app.extensions["mqtt_publisher"] = FakeMqttPublisher(connected=False)
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "unhealthy"


# ---------------------------------------------------------------------------
# POST /test
# ---------------------------------------------------------------------------

def test_test_alert_requires_auth(client, store):
    resp = client.post("/test")
    assert resp.status_code == 401


def test_test_alert_succeeds_for_any_authenticated_principal_no_permission_needed(client, store, mqtt):
    # Unlike /alert, /test has no alerts:write/client scoping -- any valid
    # Basic/Bearer principal may trigger it, exactly like the original.
    store.add_user("alice", "hunter2", scopes={})
    resp = client.post("/test", headers=_basic("alice", "hunter2"))
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "success"
    assert mqtt.published[0]["metadata"]["authenticated_user"] == "alice"
    assert "password_hash" not in json.dumps(mqtt.published[0])


# ---------------------------------------------------------------------------
# GET /docs (auth required, serves CLIENTNODE.md) / GET /docs/<f> (public)
# ---------------------------------------------------------------------------

def test_docs_requires_auth(client, store):
    resp = client.get("/docs")
    assert resp.status_code == 401


def test_docs_serves_clientnode_md_when_authenticated(client, store):
    store.add_user("alice", "hunter2")
    resp = client.get("/docs", headers=_basic("alice", "hunter2"))
    assert resp.status_code == 200
    assert resp.mimetype == "text/markdown"
    assert "Alert Manager Client Integration Reference" in resp.get_data(as_text=True)


def test_serve_docs_file_is_public_no_auth_required(client):
    # /data/docs doesn't exist in this sandbox, so this 404s -- the point of
    # this test is that it is NOT a 401, proving no auth is required at all
    # (ported verbatim from http_gateway.py, which has no @require_auth on
    # this specific route, unlike /docs above).
    resp = client.get("/docs/SOME_FILE.md")
    assert resp.status_code == 404
    assert resp.status_code != 401


def test_serve_docs_rejects_directory_traversal(client):
    resp = client.get("/docs/../etc/passwd")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "Invalid filename"


def test_serve_docs_success(client, mocker):
    mocker.patch("gateway.api.routes.os.path.exists", return_value=True)
    mocker.patch("gateway.api.routes.os.path.isdir", return_value=False)
    mocker.patch("gateway.api.routes.os.path.realpath", side_effect=lambda p: p)
    mocker.patch("gateway.api.routes.send_from_directory", return_value=("file content", 200))

    resp = client.get("/docs/SOME_FILE.md")
    assert resp.status_code == 200


def test_serve_docs_directory_requested_is_rejected(client, mocker):
    mocker.patch("gateway.api.routes.os.path.exists", return_value=True)
    mocker.patch("gateway.api.routes.os.path.isdir", return_value=True)
    mocker.patch("gateway.api.routes.os.path.realpath", side_effect=lambda p: p)

    resp = client.get("/docs/some-dir")
    assert resp.status_code == 403
    assert resp.get_json()["error"] == "Directory browsing not allowed"
