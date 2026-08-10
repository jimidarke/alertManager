"""
Tests for tools/user-manager.py (Task 16).

This CLI is a thin argparse pass-through over src/userstore/store.py's
UserStore (Task 15, already tested in tests/test_userstore.py) -- these
tests do NOT exercise UserStore's own SQL/business logic again. Instead
they verify this file's own responsibility: argv -> the right UserStore
method call with the right arguments -> correctly formatted stdout/stderr
and exit code, for every subcommand.

No live Postgres is used or needed: every test builds a
`unittest.mock.create_autospec(UserStore, instance=True)` fake and passes
it straight into `user_manager.main(argv=[...], store=fake_store)`,
bypassing UserStore.from_env() (and therefore any DB connection) entirely.
create_autospec is used (not a bare MagicMock/hand-rolled fake) specifically
so a CLI bug that passes a wrong/misspelled keyword argument, or an argument
UserStore's real signature doesn't accept, raises a TypeError here rather
than silently passing -- the same protection a real UserStore would give,
without needing one.

tools/user-manager.py is loaded via importlib (its filename has a hyphen,
so it cannot be `import`ed as a normal module name).
"""

import importlib.util
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))  # userstore/ lives under src/

from userstore.store import (  # noqa: E402
    MASTER_ADMIN_ROLE,
    PrincipalData,
    RoleNotFoundError,
    TokenNotFoundError,
    UserNotFoundError,
    UserStore,
)


def _load_user_manager():
    path = _ROOT / "tools" / "user-manager.py"
    spec = importlib.util.spec_from_file_location("user_manager_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


um = _load_user_manager()


def _store() -> UserStore:
    """A fresh create_autospec(UserStore) fake -- signature-checked, call-recording, no DB."""
    return create_autospec(UserStore, instance=True)


def _principal(username, roles=frozenset(), scopes=None):
    return PrincipalData(
        user_id=1,
        username=username,
        status="active",
        default_client=None,
        must_change_password=False,
        roles=frozenset(roles),
        scopes=scopes or {},
    )


@pytest.fixture(autouse=True)
def _fixed_actor(monkeypatch):
    """Deterministic --actor default across all tests (build_arg_parser() reads $USER at call time)."""
    monkeypatch.setenv("USER", "tester")
    yield


# ===========================================================================
# init-admin
# ===========================================================================

def test_init_admin_refuses_when_master_admin_already_exists(capsys):
    store = _store()
    store.list_users.return_value = [{"username": "alice"}, {"username": "bob"}]
    store.get_principal.side_effect = lambda u: (
        _principal("bob", roles={MASTER_ADMIN_ROLE}) if u == "bob" else _principal("alice")
    )

    rc = um.main(["init-admin"], store=store)

    assert rc == 1
    store.create_user.assert_not_called()
    store.grant_role.assert_not_called()
    err = capsys.readouterr().err
    assert "already exists" in err
    assert "bob" in err


def test_init_admin_creates_user_and_grants_role_when_none_exists(capsys):
    store = _store()
    store.list_users.return_value = [{"username": "alice"}]
    store.get_principal.return_value = _principal("alice")  # not a master_admin
    store.create_user.return_value = 42

    rc = um.main(["init-admin", "newadmin"], store=store)

    assert rc == 0
    store.create_user.assert_called_once_with(
        "newadmin", store.create_user.call_args.args[1],
        default_client=None, must_change_password=True, status="active",
        actor="cli:tester", source_ip=None,
    )
    generated_password = store.create_user.call_args.args[1]
    assert isinstance(generated_password, str) and len(generated_password) > 10

    store.grant_role.assert_called_once_with(
        "newadmin", MASTER_ADMIN_ROLE, actor="cli:tester", source_ip=None,
    )

    out = capsys.readouterr().out
    assert out.count(generated_password) == 1
    assert "SAVE THIS NOW" in out


def test_init_admin_defaults_username_to_admin(capsys):
    store = _store()
    store.list_users.return_value = []
    store.create_user.return_value = 1

    um.main(["init-admin"], store=store)

    assert store.create_user.call_args.args[0] == "admin"


def test_init_admin_respects_actor_override(capsys):
    store = _store()
    store.list_users.return_value = []
    store.create_user.return_value = 1

    um.main(["init-admin", "--actor", "someone-else"], store=store)

    assert store.create_user.call_args.kwargs["actor"] == "someone-else"


# ===========================================================================
# --actor default resolution ($USER/$USERNAME/neither)
#
# Regression coverage for a reviewer-flagged issue: the CLI's documented
# primary invocation path (`docker exec -it alert-http-gateway python3
# tools/user-manager.py ...`) has neither $USER nor $USERNAME set
# (docker-compose.yml runs that container as `user: "0:0"`, and `docker
# exec` doesn't go through a login shell/PAM), so the default must NOT
# resolve to a fixed, indistinguishable "cli:unknown" string that would
# look like real attribution but never actually be one. It must resolve to
# None instead -- which UserStore's own _detail() helper omits from the
# audit row entirely -- so an unattributed action reads as unattributed.
# ===========================================================================

def test_actor_defaults_to_none_when_neither_user_nor_username_env_var_set(monkeypatch, capsys):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERNAME", raising=False)
    store = _store()

    rc = um.main(["disable", "jimi"], store=store)

    assert rc == 0
    store.disable_user.assert_called_once_with("jimi", actor=None, source_ip=None)


def test_actor_falls_back_to_username_env_var_when_user_unset(monkeypatch, capsys):
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setenv("USERNAME", "winuser")
    store = _store()

    rc = um.main(["disable", "jimi"], store=store)

    assert rc == 0
    store.disable_user.assert_called_once_with("jimi", actor="cli:winuser", source_ip=None)


def test_actor_prefers_user_env_var_over_username(monkeypatch, capsys):
    monkeypatch.setenv("USER", "posixuser")
    monkeypatch.setenv("USERNAME", "winuser")
    store = _store()

    rc = um.main(["disable", "jimi"], store=store)

    assert rc == 0
    store.disable_user.assert_called_once_with("jimi", actor="cli:posixuser", source_ip=None)


# ===========================================================================
# create-user
# ===========================================================================

def test_create_user_with_explicit_password_does_not_print_secret(capsys):
    store = _store()
    store.create_user.return_value = 7

    rc = um.main(["create-user", "jimi", "--password", "hunter2", "--default-client", "revysmc"], store=store)

    assert rc == 0
    store.create_user.assert_called_once_with(
        "jimi", "hunter2",
        default_client="revysmc", must_change_password=False, status="active",
        actor="cli:tester", source_ip=None,
    )
    out = capsys.readouterr().out
    assert "SAVE THIS NOW" not in out


def test_create_user_without_password_generates_and_prints_once(capsys):
    store = _store()
    store.create_user.return_value = 8

    rc = um.main(["create-user", "jimi"], store=store)

    assert rc == 0
    generated_password = store.create_user.call_args.args[1]
    assert isinstance(generated_password, str) and len(generated_password) > 10
    out = capsys.readouterr().out
    assert out.count(generated_password) == 1
    assert "SAVE THIS NOW" in out


def test_create_user_passes_must_change_password_and_status_flags(capsys):
    store = _store()
    store.create_user.return_value = 9

    um.main(
        ["create-user", "svc-node", "--password", "x", "--must-change-password", "--status", "disabled"],
        store=store,
    )

    store.create_user.assert_called_once_with(
        "svc-node", "x",
        default_client=None, must_change_password=True, status="disabled",
        actor="cli:tester", source_ip=None,
    )


# ===========================================================================
# set-password
# ===========================================================================

def test_set_password_with_explicit_password(capsys):
    store = _store()

    rc = um.main(["set-password", "jimi", "--password", "newpass", "--must-change-password"], store=store)

    assert rc == 0
    store.set_password.assert_called_once_with(
        "jimi", "newpass", must_change_password=True, actor="cli:tester", source_ip=None,
    )
    assert "SAVE THIS NOW" not in capsys.readouterr().out


def test_set_password_generates_when_omitted(capsys):
    store = _store()

    rc = um.main(["set-password", "jimi"], store=store)

    assert rc == 0
    generated_password = store.set_password.call_args.args[1]
    out = capsys.readouterr().out
    assert out.count(generated_password) == 1
    assert "SAVE THIS NOW" in out


def test_set_password_user_not_found_is_clean_error(capsys):
    store = _store()
    store.set_password.side_effect = UserNotFoundError("ghost")

    rc = um.main(["set-password", "ghost", "--password", "x"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no such user" in err
    assert "ghost" in err


# ===========================================================================
# list / show
# ===========================================================================

def test_list_calls_list_users_and_prints_rows(capsys):
    store = _store()
    store.list_users.return_value = [
        {"username": "alice", "status": "active", "default_client": None,
         "must_change_password": False, "expires_at": None, "last_login_at": None},
        {"username": "bob", "status": "disabled", "default_client": "revysmc",
         "must_change_password": True, "expires_at": None, "last_login_at": None},
    ]

    rc = um.main(["list"], store=store)

    assert rc == 0
    store.list_users.assert_called_once_with()
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out


def test_list_with_no_users_prints_none(capsys):
    store = _store()
    store.list_users.return_value = []

    rc = um.main(["list"], store=store)

    assert rc == 0
    assert "(none)" in capsys.readouterr().out


def test_show_existing_user_prints_fields(capsys):
    store = _store()
    store.get_user.return_value = {"id": 1, "username": "jimi", "status": "active"}

    rc = um.main(["show", "jimi"], store=store)

    assert rc == 0
    store.get_user.assert_called_once_with("jimi")
    out = capsys.readouterr().out
    assert "jimi" in out
    assert "active" in out


def test_show_missing_user_is_clean_error_not_traceback(capsys):
    store = _store()
    store.get_user.return_value = None

    rc = um.main(["show", "ghost"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no such user" in err
    assert "ghost" in err


# ===========================================================================
# disable / enable / delete
# ===========================================================================

def test_disable_calls_disable_user(capsys):
    store = _store()

    rc = um.main(["disable", "jimi"], store=store)

    assert rc == 0
    store.disable_user.assert_called_once_with("jimi", actor="cli:tester", source_ip=None)


def test_enable_calls_enable_user(capsys):
    store = _store()

    rc = um.main(["enable", "jimi"], store=store)

    assert rc == 0
    store.enable_user.assert_called_once_with("jimi", actor="cli:tester", source_ip=None)


def test_delete_calls_delete_user(capsys):
    store = _store()

    rc = um.main(["delete", "jimi"], store=store)

    assert rc == 0
    store.delete_user.assert_called_once_with("jimi", actor="cli:tester", source_ip=None)


@pytest.mark.parametrize("subcommand,method_name", [
    ("disable", "disable_user"),
    ("enable", "enable_user"),
    ("delete", "delete_user"),
])
def test_user_not_found_is_clean_error_for_mutating_user_commands(capsys, subcommand, method_name):
    store = _store()
    getattr(store, method_name).side_effect = UserNotFoundError("ghost")

    rc = um.main([subcommand, "ghost"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:")
    assert "no such user" in err
    assert "ghost" in err
    assert "Traceback" not in err


# ===========================================================================
# create-role / grant-role / revoke-role
# ===========================================================================

def test_create_role(capsys):
    store = _store()
    store.create_role.return_value = 3

    rc = um.main(["create-role", "revysmc-viewer", "--description", "Read-only", "--is-system"], store=store)

    assert rc == 0
    store.create_role.assert_called_once_with(
        "revysmc-viewer", description="Read-only", is_system=True,
        actor="cli:tester", source_ip=None,
    )


def test_create_role_defaults(capsys):
    store = _store()
    store.create_role.return_value = 4

    um.main(["create-role", "plain-role"], store=store)

    store.create_role.assert_called_once_with(
        "plain-role", description=None, is_system=False,
        actor="cli:tester", source_ip=None,
    )


def test_grant_role(capsys):
    store = _store()

    rc = um.main(["grant-role", "jimi", "revysmc-viewer"], store=store)

    assert rc == 0
    store.grant_role.assert_called_once_with("jimi", "revysmc-viewer", actor="cli:tester", source_ip=None)


def test_revoke_role(capsys):
    store = _store()

    rc = um.main(["revoke-role", "jimi", "revysmc-viewer"], store=store)

    assert rc == 0
    store.revoke_role.assert_called_once_with("jimi", "revysmc-viewer", actor="cli:tester", source_ip=None)


def test_grant_role_role_not_found_is_clean_error(capsys):
    store = _store()
    store.grant_role.side_effect = RoleNotFoundError("ghost-role")

    rc = um.main(["grant-role", "jimi", "ghost-role"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no such role" in err
    assert "ghost-role" in err


# ===========================================================================
# add-scope / remove-scope
# ===========================================================================

def test_add_scope(capsys):
    store = _store()

    rc = um.main(["add-scope", "revysmc-viewer", "revysmc", "logs:read"], store=store)

    assert rc == 0
    store.add_scope.assert_called_once_with(
        "revysmc-viewer", "revysmc", "logs:read", actor="cli:tester", source_ip=None,
    )


def test_remove_scope(capsys):
    store = _store()

    rc = um.main(["remove-scope", "revysmc-viewer", "revysmc", "logs:read"], store=store)

    assert rc == 0
    store.remove_scope.assert_called_once_with(
        "revysmc-viewer", "revysmc", "logs:read", actor="cli:tester", source_ip=None,
    )


def test_add_scope_invalid_permission_is_clean_error_not_traceback(capsys):
    store = _store()
    store.add_scope.side_effect = ValueError(
        "invalid permission 'bogus'; must be one of ['*', 'alerts:write', 'logs:read']"
    )

    rc = um.main(["add-scope", "revysmc-viewer", "revysmc", "bogus"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("Error:")
    assert "invalid permission" in err
    assert "Traceback" not in err


# ===========================================================================
# create-token / revoke-token
# ===========================================================================

def test_create_token_by_username_resolves_user_id(capsys):
    store = _store()
    store.get_user.return_value = {"id": 5, "username": "jimi"}
    store.create_token.return_value = "amt_plaintexttoken"

    rc = um.main(["create-token", "jimi-cron", "--username", "jimi", "--expires-at", "1893456000"], store=store)

    assert rc == 0
    store.get_user.assert_called_once_with("jimi")
    store.create_token.assert_called_once_with(
        "jimi-cron", 5, expires_at=1893456000, actor="cli:tester", source_ip=None,
    )
    out = capsys.readouterr().out
    assert out.count("amt_plaintexttoken") == 1
    assert "SAVE THIS NOW" in out


def test_create_token_by_username_not_found(capsys):
    store = _store()
    store.get_user.return_value = None

    rc = um.main(["create-token", "orphan-token", "--username", "ghost"], store=store)

    assert rc == 1
    store.create_token.assert_not_called()
    err = capsys.readouterr().err
    assert "no such user" in err
    assert "ghost" in err


def test_create_token_by_user_id_skips_lookup(capsys):
    store = _store()
    store.create_token.return_value = "amt_plaintexttoken2"

    rc = um.main(["create-token", "direct-token", "--user-id", "99"], store=store)

    assert rc == 0
    store.get_user.assert_not_called()
    store.create_token.assert_called_once_with(
        "direct-token", 99, expires_at=None, actor="cli:tester", source_ip=None,
    )


def test_create_token_requires_username_or_user_id(capsys):
    store = _store()

    with pytest.raises(SystemExit):
        um.main(["create-token", "no-owner"], store=store)

    store.create_token.assert_not_called()


def test_revoke_token(capsys):
    store = _store()

    rc = um.main(["revoke-token", "123"], store=store)

    assert rc == 0
    store.revoke_token.assert_called_once_with(123, actor="cli:tester", source_ip=None)


def test_revoke_token_not_found_is_clean_error(capsys):
    store = _store()
    store.revoke_token.side_effect = TokenNotFoundError(999)

    rc = um.main(["revoke-token", "999"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert "no such token" in err
    assert "999" in err
    assert "Traceback" not in err


# ===========================================================================
# audit
# ===========================================================================

def test_audit_default_args(capsys):
    store = _store()
    store.recent_audit.return_value = [
        {"id": 1, "at": 1700000000, "event": "login_success", "username": "jimi", "source_ip": "1.2.3.4", "detail": None},
    ]

    rc = um.main(["audit"], store=store)

    assert rc == 0
    store.recent_audit.assert_called_once_with(username=None, event=None, limit=100)
    out = capsys.readouterr().out
    assert "login_success" in out
    assert "jimi" in out


def test_audit_with_filters(capsys):
    store = _store()
    store.recent_audit.return_value = []

    rc = um.main(["audit", "--username", "jimi", "--event", "login_failed", "--limit", "5"], store=store)

    assert rc == 0
    store.recent_audit.assert_called_once_with(username="jimi", event="login_failed", limit=5)
    assert "(no audit rows)" in capsys.readouterr().out


# ===========================================================================
# generic argument-parsing behavior
# ===========================================================================

def test_no_subcommand_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        um.main([], store=_store())
    assert exc_info.value.code != 0


def test_unknown_subcommand_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc_info:
        um.main(["bogus-command"], store=_store())
    assert exc_info.value.code != 0


# ===========================================================================
# unlock / list-tokens (v3.0.0)
#
# Both close gaps the auth reset exposed: an account locked by 5 failed
# logins had no operator override at all, and `revoke-token <id>` was
# unusable because nothing in the CLI ever printed a token id --
# UserStore.list_tokens() had zero callers and docs/CLIENTNODE.md pointed
# operators at `list`/`show`, neither of which shows tokens.
# ===========================================================================

def test_unlock_calls_unlock_user(capsys):
    store = _store()

    rc = um.main(["unlock", "jimi"], store=store)

    assert rc == 0
    store.unlock_user.assert_called_once_with("jimi", actor="cli:tester", source_ip=None)


def test_unlock_not_found_is_clean_error(capsys):
    store = _store()
    store.unlock_user.side_effect = UserNotFoundError("nobody")

    rc = um.main(["unlock", "nobody"], store=store)

    assert rc == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_list_tokens_all_users_when_no_username(capsys):
    store = _store()
    store.list_tokens.return_value = [
        {
            "id": 3, "name": "price-checker", "username": "price-checker",
            "status": "active", "expires_at": None, "last_used_at": 1754500000,
            "created_at": 1754400000,
        },
    ]

    rc = um.main(["list-tokens"], store=store)

    assert rc == 0
    store.list_tokens.assert_called_once_with(None)
    out = capsys.readouterr().out
    assert "price-checker" in out
    assert "3" in out


def test_list_tokens_resolves_username_to_user_id(capsys):
    store = _store()
    store.get_user.return_value = {"id": 42, "username": "jimi"}
    store.list_tokens.return_value = []

    rc = um.main(["list-tokens", "--username", "jimi"], store=store)

    assert rc == 0
    store.list_tokens.assert_called_once_with(42)
    assert "(no tokens)" in capsys.readouterr().out


def test_list_tokens_unknown_username_is_clean_error(capsys):
    store = _store()
    store.get_user.return_value = None

    rc = um.main(["list-tokens", "--username", "nobody"], store=store)

    assert rc == 1
    store.list_tokens.assert_not_called()
    assert "no such user" in capsys.readouterr().err


def test_list_tokens_never_prints_token_material(capsys):
    """Only the SHA-256 is stored; list_tokens() must not select or print it."""
    store = _store()
    store.list_tokens.return_value = [
        {
            "id": 1, "name": "t", "username": "u", "status": "active",
            "expires_at": None, "last_used_at": None, "created_at": 1,
        },
    ]

    um.main(["list-tokens"], store=store)

    out = capsys.readouterr().out
    assert "amt_" not in out
    assert "token_sha256" not in out
