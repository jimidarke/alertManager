"""
Tests for src/userstore/ (Task 15): passwords.py, PrincipalData, and
UserStore (store.py).

Three tiers, per the task brief:

1. Unconditional, pure-Python tests for passwords.py -- real argon2-cffi
   hash/verify round trips, no mocking, no DB. argon2-cffi has no external
   service dependency so these are fully real.
2. Unconditional, pure-Python tests for PrincipalData.has_permission() /
   visible_clients() -- a matrix covering master_admin, a multi-client
   support agent, a single-client node, wildcard-permission-on-one-client,
   wildcard-client-on-one-permission, and no-matching-scope. No DB
   involved -- PrincipalData is a plain frozen dataclass.
3. UserStore's DB-touching methods, in two flavors:
   a. Mocked-connection tests (unconditional): verify this module's own
      SQL shape, parameter binding, and control flow (lockout counter
      arithmetic, token format, principal-row aggregation) against a
      mocked psycopg2 connection/cursor -- exactly the pattern
      tests/test_email_log_writer.py and tests/test_db_migrate.py already
      use in this repo. These do NOT verify the SQL is valid against a
      real Postgres.
   b. Live-DB integration tests, gated on the AM_TEST_DSN environment
      variable and skipped cleanly when it is unset (no live Postgres is
      available in this environment, and per project rule
      docker/docker-compose is never invoked from automation) -- same
      convention tests/test_db_migrate.py established. These exercise
      real CRUD + the full lockout state machine against
      sql/migrations/0002_identity.sql applied to a scratch database, and
      MUST be run before this branch reaches prod.
"""

import hashlib
import os
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from userstore import passwords  # noqa: E402
from userstore.store import (  # noqa: E402
    ALL_CLIENTS,
    ALLOWED_PERMISSIONS,
    LOCKOUT_DURATION_SECONDS,
    LOCKOUT_THRESHOLD,
    TOKEN_PREFIX,
    TOKEN_RANDOM_BYTES,
    PrincipalData,
    RoleNotFoundError,
    TokenNotFoundError,
    UserNotFoundError,
    UserStore,
)

# ===========================================================================
# 1. passwords.py -- real argon2-cffi round trips, no mocking
# ===========================================================================


def test_hash_password_produces_argon2id_hash():
    h = passwords.hash_password("correct horse battery staple")
    assert h.startswith("$argon2id$")


def test_verify_password_round_trip_succeeds():
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password("correct horse battery staple", h) is True


def test_verify_password_wrong_password_fails():
    h = passwords.hash_password("right-password")
    assert passwords.verify_password("wrong-password", h) is False


def test_verify_password_malformed_hash_returns_false_not_raise():
    # A corrupt/foreign hash string must read as "wrong password", not crash
    # the login path -- verify_login() relies on this.
    assert passwords.verify_password("whatever", "not-a-real-argon2-hash") is False


def test_verify_password_empty_hash_returns_false_not_raise():
    assert passwords.verify_password("whatever", "") is False


def test_hash_password_uses_random_salt_each_call():
    h1 = passwords.hash_password("same-password")
    h2 = passwords.hash_password("same-password")
    assert h1 != h2
    assert passwords.verify_password("same-password", h1) is True
    assert passwords.verify_password("same-password", h2) is True


def test_needs_rehash_false_for_freshly_hashed_password():
    h = passwords.hash_password("x")
    assert passwords.needs_rehash(h) is False


def test_argon2_password_hasher_defaults_are_argon2id():
    # Verifies (not assumes) that argon2-cffi's PasswordHasher() defaults
    # are argon2id, per the task brief's explicit instruction not to just
    # assume this.
    from argon2 import Type

    assert passwords._hasher.type == Type.ID


# ===========================================================================
# 2. PrincipalData.has_permission() / visible_clients() matrix
# ===========================================================================


def _principal(roles, scopes, default_client=None, must_change_password=False):
    return PrincipalData(
        user_id=1,
        username="test-principal",
        status="active",
        default_client=default_client,
        must_change_password=must_change_password,
        roles=frozenset(roles),
        scopes=scopes,
    )


MASTER_ADMIN = _principal({"master_admin"}, {"*": frozenset({"*"})})

SUPPORT_AGENT = _principal(
    {"support-agent"},
    {
        "acme": frozenset({"logs:read", "alerts:write"}),
        "globex": frozenset({"logs:read"}),
    },
)

NODE = _principal({"acme-node"}, {"acme": frozenset({"alerts:write", "logs:read"})}, default_client="acme")

WILDCARD_PERM_ON_ONE_CLIENT = _principal({"acme-admin"}, {"acme": frozenset({"*"})})

WILDCARD_CLIENT_ON_ONE_PERM = _principal({"log-auditor"}, {"*": frozenset({"logs:read"})})

NO_SCOPES = _principal(set(), {})


# -- master_admin: ('*', '*') ------------------------------------------------


def test_master_admin_is_master_admin():
    assert MASTER_ADMIN.is_master_admin() is True


@pytest.mark.parametrize(
    "permission,client",
    [("logs:read", "acme"), ("alerts:write", "totally-unlisted-client"), ("alerts:write", "*")],
)
def test_master_admin_has_permission_for_any_client_and_permission(permission, client):
    assert MASTER_ADMIN.has_permission(permission, client) is True


def test_master_admin_visible_clients_is_all_clients_sentinel():
    assert MASTER_ADMIN.visible_clients("logs:read") is ALL_CLIENTS
    assert MASTER_ADMIN.visible_clients("alerts:write") is ALL_CLIENTS


# -- multi-client support agent ----------------------------------------------


def test_support_agent_is_not_master_admin():
    assert SUPPORT_AGENT.is_master_admin() is False


def test_support_agent_has_permission_scoped_per_client():
    assert SUPPORT_AGENT.has_permission("logs:read", "acme") is True
    assert SUPPORT_AGENT.has_permission("alerts:write", "acme") is True
    assert SUPPORT_AGENT.has_permission("logs:read", "globex") is True


def test_support_agent_lacks_permission_not_granted_for_that_client():
    # alerts:write was only granted for acme, not globex.
    assert SUPPORT_AGENT.has_permission("alerts:write", "globex") is False


def test_support_agent_lacks_permission_for_unlisted_client():
    assert SUPPORT_AGENT.has_permission("logs:read", "initech") is False


def test_support_agent_visible_clients_returns_concrete_sets():
    assert SUPPORT_AGENT.visible_clients("logs:read") == frozenset({"acme", "globex"})
    assert SUPPORT_AGENT.visible_clients("alerts:write") == frozenset({"acme"})
    assert SUPPORT_AGENT.visible_clients("not-a-real-permission") == frozenset()


# -- single-client node -------------------------------------------------------


def test_node_has_permission_for_its_own_client_only():
    assert NODE.has_permission("alerts:write", "acme") is True
    assert NODE.has_permission("logs:read", "acme") is True


def test_node_lacks_permission_for_other_clients():
    assert NODE.has_permission("alerts:write", "othercompany") is False


def test_node_visible_clients_is_just_its_own_client():
    assert NODE.visible_clients("logs:read") == frozenset({"acme"})
    assert NODE.visible_clients("not-a-real-permission") == frozenset()


# -- wildcard permission on one specific client -------------------------------


def test_wildcard_permission_on_specific_client_matches_any_permission_there():
    assert WILDCARD_PERM_ON_ONE_CLIENT.has_permission("logs:read", "acme") is True
    assert WILDCARD_PERM_ON_ONE_CLIENT.has_permission("alerts:write", "acme") is True
    assert WILDCARD_PERM_ON_ONE_CLIENT.has_permission("not-a-real-permission", "acme") is True


def test_wildcard_permission_on_specific_client_does_not_leak_to_other_clients():
    assert WILDCARD_PERM_ON_ONE_CLIENT.has_permission("logs:read", "othercompany") is False


def test_wildcard_permission_on_specific_client_visible_clients_includes_it_for_any_permission():
    assert WILDCARD_PERM_ON_ONE_CLIENT.visible_clients("alerts:write") == frozenset({"acme"})
    assert WILDCARD_PERM_ON_ONE_CLIENT.visible_clients("logs:read") == frozenset({"acme"})


# -- wildcard client on one specific permission -------------------------------


def test_wildcard_client_on_specific_permission_matches_any_client():
    assert WILDCARD_CLIENT_ON_ONE_PERM.has_permission("logs:read", "acme") is True
    assert WILDCARD_CLIENT_ON_ONE_PERM.has_permission("logs:read", "totally-unlisted-client") is True


def test_wildcard_client_on_specific_permission_does_not_grant_other_permissions():
    assert WILDCARD_CLIENT_ON_ONE_PERM.has_permission("alerts:write", "acme") is False


def test_wildcard_client_on_specific_permission_visible_clients_is_all_clients_for_that_permission():
    assert WILDCARD_CLIENT_ON_ONE_PERM.visible_clients("logs:read") is ALL_CLIENTS


def test_wildcard_client_on_specific_permission_visible_clients_empty_for_other_permission():
    assert WILDCARD_CLIENT_ON_ONE_PERM.visible_clients("alerts:write") == frozenset()


# -- no matching scope at all --------------------------------------------------


def test_no_scopes_has_no_permissions_anywhere():
    assert NO_SCOPES.has_permission("logs:read", "acme") is False
    assert NO_SCOPES.is_master_admin() is False


def test_no_scopes_visible_clients_is_empty_frozenset_not_all_clients():
    # Critical distinction the ALL_CLIENTS sentinel exists to make
    # unambiguous: "no visible clients" must never be conflated with
    # "all clients" just because both are falsy-ish.
    result = NO_SCOPES.visible_clients("logs:read")
    assert result == frozenset()
    assert result is not ALL_CLIENTS


# ===========================================================================
# 3a. UserStore -- mocked psycopg2 connection/cursor tests (unconditional)
# ===========================================================================


@pytest.fixture
def store():
    return UserStore({"host": "test-host", "port": 5432, "database": "testdb", "user": "testuser", "password": "x"})


def _mock_cursor(mocker, mock_conn):
    """Return the cursor mock that UserStore._cursor()'s `with conn.cursor(...) as cur:` yields."""
    return mock_conn.cursor.return_value.__enter__.return_value


def _patch_connect(mocker, mock_conn=None):
    if mock_conn is None:
        mock_conn = mocker.MagicMock()
    mocker.patch("userstore.store.psycopg2.connect", return_value=mock_conn)
    return mock_conn


# -- from_env() ----------------------------------------------------------------


def test_from_env_uses_postgres_env_vars(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "dbhost")
    monkeypatch.setenv("POSTGRES_PORT", "5555")
    monkeypatch.setenv("POSTGRES_DB", "mydb")
    monkeypatch.setenv("POSTGRES_USER", "myuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "mypass")

    us = UserStore.from_env()

    assert us._dsn == {
        "host": "dbhost",
        "port": 5555,
        "database": "mydb",
        "user": "myuser",
        "password": "mypass",
    }


def test_from_env_defaults_match_db_migrate_and_database_logger(monkeypatch):
    for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    us = UserStore.from_env()

    assert us._dsn == {
        "host": "localhost",
        "port": 5432,
        "database": "alertmanager",
        "user": "alertmanager",
        "password": "",
    }


def test_cursor_closes_connection_even_on_exception(mocker, store):
    mock_conn = _patch_connect(mocker)

    with pytest.raises(RuntimeError):
        with store._cursor():
            raise RuntimeError("boom")

    mock_conn.close.assert_called_once()


# -- create_user -----------------------------------------------------------


def test_create_user_inserts_hashed_password_and_audits(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 42}

    user_id = store.create_user("  ALICE  ", "hunter2", default_client="acme")

    assert user_id == 42
    insert_sql, insert_params = cur.execute.call_args_list[0].args
    assert "INSERT INTO users" in insert_sql
    assert insert_params[0] == "alice"  # normalized: stripped + lowercased
    assert insert_params[1] != "hunter2"  # never store the plaintext
    assert insert_params[1].startswith("$argon2id$")
    assert insert_params[3] == "acme"  # default_client

    audit_sql, audit_params = cur.execute.call_args_list[1].args
    assert "INSERT INTO auth_audit" in audit_sql
    assert audit_params[1] == "user_created"
    assert audit_params[2] == "alice"


def test_create_user_never_references_expires_at(mocker, store):
    # v3.0.0 removed users.expires_at entirely: it was read by verify_login()
    # and verify_token(), but the only caller that ever set a non-NULL value
    # was tools/migrate-users-json.py, now deleted. This test pins the column
    # staying gone -- a stray INSERT naming it would now be a hard SQL error
    # against the real schema, which mocks would otherwise hide.
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 7}

    store.create_user("bob", "hunter2")

    insert_sql, _ = cur.execute.call_args_list[0].args
    assert "expires_at" not in insert_sql


def test_get_user_never_selects_password_hash(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 1, "username": "alice"}

    store.get_user("alice")

    sql, _ = cur.execute.call_args.args
    assert "password_hash" not in sql


def test_get_user_returns_none_for_missing_user(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    assert store.get_user("ghost") is None


def test_disable_user_raises_when_no_such_user(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    with pytest.raises(UserNotFoundError):
        store.disable_user("ghost")


def test_delete_user_raises_when_no_such_user(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    with pytest.raises(UserNotFoundError):
        store.delete_user("ghost")


# -- verify_login: lockout state machine ------------------------------------


def _user_row(**overrides):
    row = {
        "id": 7,
        "username": "alice",
        "password_hash": passwords.hash_password("correct-password"),
        "status": "active",
        "locked_until": None,
        "failed_attempts": 0,
    }
    row.update(overrides)
    return row


def _principal_rows(username="alice"):
    # What _fetch_principal()'s join query would return for a user with no roles.
    return [
        {
            "user_id": 7,
            "username": username,
            "status": "active",
            "default_client": None,
            "must_change_password": False,
            "role_name": None,
            "scope_client": None,
            "scope_permission": None,
        }
    ]


def test_verify_login_nonexistent_user_returns_none_and_audits_login_failed(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    result = store.verify_login("ghost", "whatever")

    assert result is None
    audit_sql, audit_params = cur.execute.call_args.args
    assert "INSERT INTO auth_audit" in audit_sql
    assert audit_params[1] == "login_failed"


def test_verify_login_never_checks_password_when_disabled(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(status="disabled")
    mock_verify = mocker.patch("userstore.store.verify_password")

    result = store.verify_login("alice", "correct-password")

    assert result is None
    mock_verify.assert_not_called()


def test_verify_login_never_checks_password_when_still_locked(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(locked_until=int(time.time()) + 500, failed_attempts=5)
    mock_verify = mocker.patch("userstore.store.verify_password")

    result = store.verify_login("alice", "correct-password")

    assert result is None
    mock_verify.assert_not_called()
    audit_sql, audit_params = cur.execute.call_args.args
    assert audit_params[1] == "login_failed"
    assert audit_params[4].adapted["reason"] == "locked"


def test_verify_login_never_checks_password_when_disabled(mocker, store):
    # Account status is now the only pre-password gate besides lockout;
    # users.expires_at was removed in v3.0.0.
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(status="disabled")
    mock_verify = mocker.patch("userstore.store.verify_password")

    result = store.verify_login("alice", "correct-password")

    assert result is None
    mock_verify.assert_not_called()


def test_verify_login_wrong_password_increments_failed_attempts_below_threshold(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(failed_attempts=2)

    result = store.verify_login("alice", "wrong-password")

    assert result is None
    # call_args_list[0] is the initial SELECT; [1] is the failed-attempts UPDATE.
    update_sql, update_params = cur.execute.call_args_list[1].args
    assert "SET failed_attempts = %s, updated_at" in update_sql
    assert update_params[0] == 3  # 2 -> 3, no lock yet
    audit_sql, audit_params = cur.execute.call_args_list[2].args
    assert audit_params[1] == "login_failed"


def test_verify_login_wrong_password_at_exactly_five_sets_lockout(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(failed_attempts=LOCKOUT_THRESHOLD - 1)

    before = int(time.time())
    result = store.verify_login("alice", "wrong-password")
    after = int(time.time())

    assert result is None
    update_sql, update_params = cur.execute.call_args_list[1].args
    assert "locked_until" in update_sql
    assert update_params[0] == LOCKOUT_THRESHOLD
    locked_until = update_params[1]
    assert before + LOCKOUT_DURATION_SECONDS <= locked_until <= after + LOCKOUT_DURATION_SECONDS

    audit_sql, audit_params = cur.execute.call_args_list[2].args
    assert audit_params[1] == "lockout"


def test_verify_login_success_resets_failed_attempts_and_returns_principal(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = _user_row(failed_attempts=3)
    cur.fetchall.return_value = _principal_rows()

    result = store.verify_login("alice", "correct-password")

    assert isinstance(result, PrincipalData)
    assert result.username == "alice"

    reset_sql, reset_params = cur.execute.call_args_list[1].args
    assert "failed_attempts = 0, locked_until = NULL, last_login_at" in reset_sql

    audit_sql, audit_params = cur.execute.call_args_list[2].args
    assert audit_params[1] == "login_success"


def test_verify_login_expired_lock_resets_counter_before_evaluating_attempt(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    # Lock expired in the past; failed_attempts still shows the old count.
    cur.fetchone.return_value = _user_row(locked_until=int(time.time()) - 10, failed_attempts=5)
    cur.fetchall.return_value = _principal_rows()

    result = store.verify_login("alice", "correct-password")

    assert isinstance(result, PrincipalData)
    # call_args_list[0] is the initial SELECT; [1] must be the lock-expiry reset.
    reset_sql, _ = cur.execute.call_args_list[1].args
    assert "locked_until = NULL, failed_attempts = 0" in reset_sql


# -- get_principal / _fetch_principal aggregation ---------------------------


def test_get_principal_aggregates_multiple_roles_and_scopes(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = [
        {
            "user_id": 1, "username": "agent", "status": "active", "default_client": None,
            "must_change_password": False, "role_name": "support-agent",
            "scope_client": "acme", "scope_permission": "logs:read",
        },
        {
            "user_id": 1, "username": "agent", "status": "active", "default_client": None,
            "must_change_password": False, "role_name": "support-agent",
            "scope_client": "acme", "scope_permission": "alerts:write",
        },
        {
            "user_id": 1, "username": "agent", "status": "active", "default_client": None,
            "must_change_password": False, "role_name": "support-agent",
            "scope_client": "globex", "scope_permission": "logs:read",
        },
    ]

    principal = store.get_principal("agent")

    assert principal.roles == frozenset({"support-agent"})
    assert principal.scopes == {
        "acme": frozenset({"logs:read", "alerts:write"}),
        "globex": frozenset({"logs:read"}),
    }


def test_get_principal_returns_none_for_nonexistent_user(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    assert store.get_principal("ghost") is None


def test_get_principal_user_with_no_roles_returns_principal_with_empty_roles(mocker, store):
    # LEFT JOINs mean a valid user with zero role grants still returns one
    # row (with role_name/scope_* NULL) -- this must NOT be confused with
    # "no such user" (empty result set).
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = _principal_rows()

    principal = store.get_principal("alice")

    assert principal is not None
    assert principal.roles == frozenset()
    assert principal.scopes == {}


def test_get_principal_query_joins_all_four_tables(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.get_principal("alice")

    sql, params = cur.execute.call_args.args
    for table in ("users", "user_roles", "roles", "role_scopes"):
        assert table in sql
    assert params == ("alice",)


# -- create_token / verify_token ---------------------------------------------


def test_create_token_format(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.side_effect = [{"id": 1}, {"username": "alice"}]

    plaintext = store.create_token("price-checker prod", user_id=99)

    assert plaintext.startswith(TOKEN_PREFIX)
    random_part = plaintext[len(TOKEN_PREFIX):]
    assert len(random_part) == 43
    assert TOKEN_RANDOM_BYTES == 32


def test_create_token_stores_only_sha256_hash_not_plaintext(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.side_effect = [{"id": 1}, {"username": "alice"}]

    plaintext = store.create_token("test-token", user_id=99)

    insert_sql, insert_params = cur.execute.call_args_list[0].args
    assert "INSERT INTO auth_tokens" in insert_sql
    stored_hash = insert_params[1]
    assert stored_hash == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
    assert stored_hash != plaintext
    assert plaintext not in str(insert_params)


def test_create_token_two_calls_produce_different_tokens(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.side_effect = [{"id": 1}, {"username": "alice"}, {"id": 2}, {"username": "alice"}]

    t1 = store.create_token("a", user_id=1)
    t2 = store.create_token("b", user_id=1)

    assert t1 != t2


def test_verify_token_hashes_plaintext_for_lookup(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    plaintext = "amt_" + "x" * 43
    store.verify_token(plaintext)

    sql, params = cur.execute.call_args.args
    assert "auth_tokens" in sql
    assert params[0] == hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def test_verify_token_rejects_revoked_token(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {
        "token_id": 1, "token_status": "revoked", "token_expires_at": None,
        "username": "alice", "user_status": "active", "user_expires_at": None,
    }

    assert store.verify_token("amt_whatever") is None


def test_verify_token_rejects_expired_token(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {
        "token_id": 1, "token_status": "active", "token_expires_at": int(time.time()) - 10,
        "username": "alice", "user_status": "active", "user_expires_at": None,
    }

    assert store.verify_token("amt_whatever") is None


def test_verify_token_rejects_disabled_user(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {
        "token_id": 1, "token_status": "active", "token_expires_at": None,
        "username": "alice", "user_status": "disabled", "user_expires_at": None,
    }

    assert store.verify_token("amt_whatever") is None


def test_verify_token_success_updates_last_used_at_and_returns_principal(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {
        "token_id": 1, "token_status": "active", "token_expires_at": None,
        "username": "alice", "user_status": "active", "user_expires_at": None,
    }
    cur.fetchall.return_value = _principal_rows()

    result = store.verify_token("amt_whatever")

    assert isinstance(result, PrincipalData)
    update_calls = [c for c in cur.execute.call_args_list if "last_used_at" in c.args[0]]
    assert len(update_calls) == 1


# -- role / scope CRUD --------------------------------------------------------


def test_grant_role_raises_when_user_missing(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    with pytest.raises(UserNotFoundError):
        store.grant_role("ghost", "master_admin")


def test_grant_role_raises_when_role_missing(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.side_effect = [{"id": 1}, None]

    with pytest.raises(RoleNotFoundError):
        store.grant_role("alice", "ghost-role")


def test_add_scope_rejects_invalid_permission_before_hitting_db(mocker, store):
    mock_connect = mocker.patch("userstore.store.psycopg2.connect")

    with pytest.raises(ValueError):
        store.add_scope("acme-node", "acme", "not-a-real-permission")

    mock_connect.assert_not_called()


@pytest.mark.parametrize("permission", sorted(ALLOWED_PERMISSIONS))
def test_add_scope_accepts_every_allowed_permission(mocker, store, permission):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 1}

    store.add_scope("acme-node", "ACME", permission)  # also exercises client normalization

    # call_args_list[0] is the role lookup SELECT; [1] is the INSERT.
    insert_sql, insert_params = cur.execute.call_args_list[1].args
    assert "INSERT INTO role_scopes" in insert_sql
    assert insert_params[1] == "acme"  # normalized
    assert insert_params[2] == permission


def test_revoke_token_raises_when_missing(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    with pytest.raises(TokenNotFoundError):
        store.revoke_token(999)


# -- list_role_scopes / list_all_scope_clients --------------------------------
# Added in the final whole-branch cleanup pass to close the "UserStore single
# owner" charter leak: gateway/admin/routes.py used to issue raw SQL directly
# against role_scopes (an identity-schema table) because UserStore exposed no
# read method for it (only add_scope()/remove_scope() writes). These two
# methods are that read surface.


def test_list_role_scopes_returns_client_permission_rows(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = [
        {"client": "acme", "permission": "alerts:write"},
        {"client": "acme", "permission": "logs:read"},
    ]

    rows = store.list_role_scopes(7)

    sql, params = cur.execute.call_args.args
    assert "SELECT client, permission FROM role_scopes WHERE role_id = %s" in sql
    assert params == (7,)
    assert rows == [
        {"client": "acme", "permission": "alerts:write"},
        {"client": "acme", "permission": "logs:read"},
    ]


def test_list_role_scopes_empty_for_role_with_no_scopes(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    assert store.list_role_scopes(99) == []


def test_list_all_scope_clients_excludes_null_empty_and_wildcard_in_sql(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = [{"client": "acme"}, {"client": "revysmc"}]

    result = store.list_all_scope_clients()

    (sql,) = cur.execute.call_args.args
    assert "role_scopes" in sql
    assert "client IS NOT NULL" in sql
    assert "client != ''" in sql
    assert "client != '*'" in sql
    assert result == {"acme", "revysmc"}


def test_list_all_scope_clients_empty_when_no_scopes_exist(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    assert store.list_all_scope_clients() == set()


# -- recent_audit filtering ---------------------------------------------------


def test_recent_audit_filters_by_username_and_event(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.recent_audit(username="Alice", event="login_failed", limit=10)

    sql, params = cur.execute.call_args.args
    assert "username = %s" in sql
    assert "event = %s" in sql
    assert "LIMIT %s" in sql
    assert params == ("alice", "login_failed", 10)


def test_recent_audit_no_filters_still_orders_and_limits(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.recent_audit()

    sql, params = cur.execute.call_args.args
    assert "ORDER BY at DESC" in sql
    assert params == (100,)


# ===========================================================================
# 3b. Live-DB integration tests, gated on AM_TEST_DSN
# ===========================================================================
#
# No live Postgres is available in this environment, and per project rule
# docker/docker-compose is never invoked from automation -- these are
# written and gated per this repo's established convention
# (tests/test_db_migrate.py) but have never actually been executed here.
# They MUST be run against a scratch Postgres 16 instance with
# sql/migrations/0001-0003 applied before this branch reaches prod.
#
# UserStore's dsn dict is passed straight through to
# psycopg2.connect(**dsn); psycopg2.connect() itself accepts a `dsn=`
# keyword carrying a full libpq DSN/URI, so `UserStore({"dsn": AM_TEST_DSN})`
# is all that's needed here -- no hand-rolled DSN parsing.

AM_TEST_DSN = os.environ.get("AM_TEST_DSN")


def _live_store() -> UserStore:
    return UserStore({"dsn": AM_TEST_DSN})


def _cleanup_test_user(username: str) -> None:
    import psycopg2

    conn = psycopg2.connect(AM_TEST_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM auth_tokens WHERE user_id IN (SELECT id FROM users WHERE username = %s)", (username,)
            )
            cur.execute(
                "DELETE FROM user_roles WHERE user_id IN (SELECT id FROM users WHERE username = %s)", (username,)
            )
            cur.execute("DELETE FROM auth_audit WHERE username = %s", (username,))
            cur.execute("DELETE FROM users WHERE username = %s", (username,))
        conn.commit()
    finally:
        conn.close()


def _cleanup_test_role(role_name: str) -> None:
    import psycopg2

    conn = psycopg2.connect(AM_TEST_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM roles WHERE name = %s", (role_name,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_create_user_and_verify_login_round_trip_against_real_postgres():
    username = "am_test_userstore_alice"
    _cleanup_test_user(username)
    store = _live_store()
    try:
        user_id = store.create_user(username, "correct-horse-battery-staple")
        assert isinstance(user_id, int)

        principal = store.verify_login(username, "correct-horse-battery-staple")
        assert principal is not None
        assert principal.username == username
        assert principal.roles == frozenset()

        assert store.verify_login(username, "wrong-password") is None

        audit_rows = store.recent_audit(username=username)
        events = [row["event"] for row in audit_rows]
        assert "login_success" in events
        assert "login_failed" in events
    finally:
        _cleanup_test_user(username)


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_lockout_after_five_failed_attempts_against_real_postgres():
    username = "am_test_userstore_bob"
    _cleanup_test_user(username)
    store = _live_store()
    try:
        store.create_user(username, "correct-password")

        for _ in range(LOCKOUT_THRESHOLD):
            assert store.verify_login(username, "wrong-password") is None

        # Correct password now rejected: account is locked, not just failing credentials.
        assert store.verify_login(username, "correct-password") is None

        row = store.get_user(username)
        assert row["failed_attempts"] == LOCKOUT_THRESHOLD
        assert row["locked_until"] is not None and row["locked_until"] > int(time.time())

        events = [r["event"] for r in store.recent_audit(username=username)]
        assert "lockout" in events
    finally:
        _cleanup_test_user(username)


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_role_and_scope_grant_shapes_get_principal_against_real_postgres():
    username = "am_test_userstore_carol"
    role_name = "am_test_userstore_role"
    _cleanup_test_user(username)
    _cleanup_test_role(role_name)
    store = _live_store()
    try:
        store.create_user(username, "correct-password")
        store.create_role(role_name, description="integration test role")
        store.add_scope(role_name, "acme", "logs:read")
        store.grant_role(username, role_name)

        principal = store.get_principal(username)
        assert principal.roles == frozenset({role_name})
        assert principal.has_permission("logs:read", "acme") is True
        assert principal.has_permission("alerts:write", "acme") is False

        store.revoke_role(username, role_name)
        principal_after_revoke = store.get_principal(username)
        assert principal_after_revoke.roles == frozenset()
    finally:
        _cleanup_test_user(username)
        _cleanup_test_role(role_name)


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_create_token_and_verify_token_round_trip_against_real_postgres():
    username = "am_test_userstore_dave"
    _cleanup_test_user(username)
    store = _live_store()
    try:
        user_id = store.create_user(username, "correct-password")

        plaintext = store.create_token("integration-test-token", user_id)
        assert plaintext.startswith(TOKEN_PREFIX)

        principal = store.verify_token(plaintext)
        assert principal is not None
        assert principal.username == username

        tokens = store.list_tokens(user_id)
        assert len(tokens) == 1
        store.revoke_token(tokens[0]["id"])

        assert store.verify_token(plaintext) is None
    finally:
        _cleanup_test_user(username)


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_list_role_scopes_and_list_all_scope_clients_against_real_postgres():
    role_name = "am_test_userstore_scope_role"
    _cleanup_test_role(role_name)
    store = _live_store()
    try:
        role_id = store.create_role(role_name, description="integration test scope role")
        store.add_scope(role_name, "acme", "logs:read")
        store.add_scope(role_name, "ACME", "alerts:write")  # exercises client normalization
        store.add_scope(role_name, "*", "*")  # wildcard client -- excluded from list_all_scope_clients()

        scopes = store.list_role_scopes(role_id)
        assert {(row["client"], row["permission"]) for row in scopes} == {
            ("acme", "logs:read"),
            ("acme", "alerts:write"),
            ("*", "*"),
        }

        all_clients = store.list_all_scope_clients()
        assert "acme" in all_clients
        assert "*" not in all_clients
    finally:
        _cleanup_test_role(role_name)


# -- unlock_user / list_tokens (v3.0.0) -------------------------------------


def test_unlock_user_clears_lockout_without_touching_status(mocker, store):
    """
    Unlock and enable are separate operator decisions: unlocking a disabled
    account must leave it disabled. enable_user() already clears lockout as a
    side effect; this is the inverse, and must NOT set status.
    """
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 7}

    store.unlock_user("alice")

    sql, params = cur.execute.call_args_list[0].args
    assert "failed_attempts = 0" in sql
    assert "locked_until = NULL" in sql
    assert "status" not in sql
    assert params[-1] == "alice"


def test_unlock_user_raises_when_user_missing(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = None

    with pytest.raises(UserNotFoundError):
        store.unlock_user("nobody")


def test_unlock_user_writes_audit_row(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchone.return_value = {"id": 7}

    store.unlock_user("alice", actor="cli:tester")

    _, audit_params = cur.execute.call_args.args
    assert audit_params[1] == "user_unlocked"
    assert audit_params[2] == "alice"


def test_list_tokens_without_user_id_lists_every_token(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.list_tokens()

    sql, params = cur.execute.call_args.args
    assert "WHERE t.user_id" not in sql
    assert params == ()


def test_list_tokens_with_user_id_filters(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.list_tokens(42)

    sql, params = cur.execute.call_args.args
    assert "WHERE t.user_id = %s" in sql
    assert params == (42,)


def test_list_tokens_joins_username_and_never_selects_token_hash(mocker, store):
    mock_conn = _patch_connect(mocker)
    cur = _mock_cursor(mocker, mock_conn)
    cur.fetchall.return_value = []

    store.list_tokens()

    sql, _ = cur.execute.call_args.args
    assert "u.username" in sql
    assert "token_sha256" not in sql
