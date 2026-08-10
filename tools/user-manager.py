#!/usr/bin/env python3
"""
Alert Manager - tools/user-manager.py (Task 16)

Thin argparse CLI over src/userstore/store.py's UserStore (Task 15) --
the canonical way to administer the Phase 4 identity schema
(sql/migrations/0002_identity.sql: users, roles, user_roles, role_scopes,
auth_tokens, auth_audit) from a shell. Retires tools/http-auth-manager.sh,
deleted by Task 28 alongside the gateway cutover -- see
docs/OVERHAUL_PLAN.md section 4.2/4.8.

Design: every subcommand is a near-direct call into one UserStore method.
No validation/business logic is duplicated here -- UserStore (and, below
it, the DB's own CHECK constraints) is the single authority on what is a
valid user/role/scope/token; this file only parses argv, calls the right
method with the right arguments, and formats the result for a terminal.
The one exception is `init-admin`, which is a genuinely CLI-only bootstrap
concern: it has to *check* (via list_users() + get_principal()) whether a
master_admin already exists before creating one, since UserStore itself
has no single-master-admin invariant of its own to enforce.

Connection: UserStore.from_env() (POSTGRES_* environment variables) is the
standard entry point, same as every other consumer of this module.

Usage (per the brief -- run inside the gateway container, or as a one-off
container if the gateway itself is down):

    docker exec -it alert-http-gateway python3 tools/user-manager.py <command> ...
    docker run --rm -it --env-file .env <image> python3 tools/user-manager.py <command> ...

Exit codes: 0 on success. Non-zero (1) for a clean, expected business-rule
failure (UserStoreError subclasses: UserNotFoundError/RoleNotFoundError/
TokenNotFoundError; ValueError from add_scope's permission check; the
init-admin "already exists" guard) -- these print "Error: ..." to stderr,
no traceback. Anything else (e.g. a lost DB connection, a UNIQUE violation
on a duplicate username) is a genuine, unexpected failure and is left to
propagate as a raw traceback with Python's default exit code (1) --
per CLAUDE.md's "do not implement failsafes" rule, this CLI does not add a
catch-all around errors UserStore itself deliberately does not catch.
"""

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))  # userstore/ lives under src/

from userstore import (  # noqa: E402
    MASTER_ADMIN_ROLE,
    UserStore,
    UserStoreError,
)

# secrets.token_urlsafe() byte counts for CLI-generated secrets. Tokens use
# UserStore.create_token()'s own TOKEN_RANDOM_BYTES internally (this file
# never generates a token itself); this constant is only for passwords this
# CLI generates on the operator's behalf (init-admin, and create-user/
# set-password when --password is omitted).
GENERATED_PASSWORD_BYTES = 18


# ---------------------------------------------------------------------------
# Small formatting/generation helpers (CLI-only concerns -- no UserStore
# business logic lives here).
# ---------------------------------------------------------------------------

def _generate_password() -> str:
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def _default_actor() -> Optional[str]:
    """
    Best-effort 'who ran this CLI' string for the audit log's actor field,
    overridable via --actor. Falls back to None -- which UserStore's own
    _detail() helper simply omits from the audit row's detail JSON, i.e.
    "unattributed" -- rather than a fixed "unknown" literal, deliberately.

    $USER/$USERNAME are unset for this tool's own documented primary
    invocation path (`docker exec -it alert-http-gateway python3
    tools/user-manager.py ...`): docker-compose.yml runs that container as
    `user: "0:0"` with no USER/USERNAME env var set anywhere, and `docker
    exec` doesn't go through a login shell/PAM to populate one. A fixed
    "unknown" string there would become every forgetful operator's
    identical, indistinguishable default -- audit data that *looks* like it
    attributes the action to someone, but never actually does. None reads
    honestly as "no actor was recorded" instead.
    """
    user = os.environ.get("USER") or os.environ.get("USERNAME")
    return f"cli:{user}" if user else None


def _print_secret_once(label: str, value: str) -> None:
    """Print a plaintext secret (password/token) exactly once, with a loud one-time warning. Never call this twice for the same secret."""
    banner = "=" * 70
    print(banner)
    print(f"  {label}: {value}")
    print("  SAVE THIS NOW -- it will not be shown again.")
    print(banner)


def _print_kv(row: Dict[str, Any]) -> None:
    width = max((len(str(k)) for k in row), default=0)
    for key, value in row.items():
        print(f"  {str(key).ljust(width)} : {value}")


def _print_table(rows: Sequence[Dict[str, Any]], columns: Sequence[str]) -> None:
    if not rows:
        print("(none)")
        return
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in columns}
    print("  ".join(c.upper().ljust(widths[c]) for c in columns))
    print("  ".join("-" * widths[c] for c in columns))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def _find_master_admin(store: UserStore) -> Optional[str]:
    """
    Return the username of an existing master_admin holder, or None if there
    isn't one. Used only by init-admin's guard.

    O(n) in the number of users -- one get_principal() round trip per user,
    since UserStore has no "does any master_admin exist" query of its own.
    init-admin is a rare, manual, one-off bootstrap operation (not a hot
    path), so this is a fine tradeoff at the account-count scale this tool
    operates at; it would need revisiting if this guard were ever reused
    somewhere called frequently against a large user table.
    """
    for user in store.list_users():
        principal = store.get_principal(user["username"])
        if principal is not None and principal.is_master_admin():
            return user["username"]
    return None


# ---------------------------------------------------------------------------
# Subcommand handlers -- each takes (store, args) and returns an exit code.
# ---------------------------------------------------------------------------

def cmd_init_admin(store: UserStore, args: argparse.Namespace) -> int:
    existing = _find_master_admin(store)
    if existing is not None:
        print(
            f"Error: a master_admin user already exists ({existing!r}). Refusing to create another.",
            file=sys.stderr,
        )
        return 1

    password = _generate_password()
    user_id = store.create_user(
        args.username,
        password,
        default_client=None,
        must_change_password=True,
        status="active",
        actor=args.actor,
        source_ip=args.source_ip,
    )
    store.grant_role(args.username, MASTER_ADMIN_ROLE, actor=args.actor, source_ip=args.source_ip)

    print(f"Created master_admin user {args.username!r} (id={user_id}).")
    _print_secret_once("Password", password)
    return 0


def cmd_create_user(store: UserStore, args: argparse.Namespace) -> int:
    generated = args.password is None
    password = args.password if not generated else _generate_password()

    user_id = store.create_user(
        args.username,
        password,
        default_client=args.default_client,
        must_change_password=args.must_change_password,
        status=args.status,
        actor=args.actor,
        source_ip=args.source_ip,
    )
    print(f"Created user {args.username!r} (id={user_id}, status={args.status!r}).")
    if generated:
        _print_secret_once("Password", password)
    return 0


def cmd_set_password(store: UserStore, args: argparse.Namespace) -> int:
    generated = args.password is None
    password = args.password if not generated else _generate_password()

    store.set_password(
        args.username,
        password,
        must_change_password=args.must_change_password,
        actor=args.actor,
        source_ip=args.source_ip,
    )
    print(f"Password updated for {args.username!r}.")
    if generated:
        _print_secret_once("Password", password)
    return 0


def cmd_list(store: UserStore, args: argparse.Namespace) -> int:
    users = store.list_users()
    _print_table(
        users,
        ["username", "status", "default_client", "must_change_password", "locked_until", "last_login_at"],
    )
    return 0


def cmd_show(store: UserStore, args: argparse.Namespace) -> int:
    user = store.get_user(args.username)
    if user is None:
        print(f"Error: no such user: {args.username!r}", file=sys.stderr)
        return 1
    _print_kv(user)
    return 0


def cmd_disable(store: UserStore, args: argparse.Namespace) -> int:
    store.disable_user(args.username, actor=args.actor, source_ip=args.source_ip)
    print(f"Disabled user {args.username!r}.")
    return 0


def cmd_enable(store: UserStore, args: argparse.Namespace) -> int:
    store.enable_user(args.username, actor=args.actor, source_ip=args.source_ip)
    print(f"Enabled user {args.username!r}.")
    return 0


def cmd_delete(store: UserStore, args: argparse.Namespace) -> int:
    store.delete_user(args.username, actor=args.actor, source_ip=args.source_ip)
    print(f"Deleted user {args.username!r}.")
    return 0


def cmd_create_role(store: UserStore, args: argparse.Namespace) -> int:
    role_id = store.create_role(
        args.name, description=args.description, is_system=args.is_system,
        actor=args.actor, source_ip=args.source_ip,
    )
    print(f"Created role {args.name!r} (id={role_id}, is_system={args.is_system}).")
    return 0


def cmd_grant_role(store: UserStore, args: argparse.Namespace) -> int:
    store.grant_role(args.username, args.role, actor=args.actor, source_ip=args.source_ip)
    print(f"Granted role {args.role!r} to {args.username!r}.")
    return 0


def cmd_revoke_role(store: UserStore, args: argparse.Namespace) -> int:
    store.revoke_role(args.username, args.role, actor=args.actor, source_ip=args.source_ip)
    print(f"Revoked role {args.role!r} from {args.username!r}.")
    return 0


def cmd_add_scope(store: UserStore, args: argparse.Namespace) -> int:
    store.add_scope(args.role, args.client, args.permission, actor=args.actor, source_ip=args.source_ip)
    print(f"Added scope (client={args.client!r}, permission={args.permission!r}) to role {args.role!r}.")
    return 0


def cmd_remove_scope(store: UserStore, args: argparse.Namespace) -> int:
    store.remove_scope(args.role, args.client, args.permission, actor=args.actor, source_ip=args.source_ip)
    print(f"Removed scope (client={args.client!r}, permission={args.permission!r}) from role {args.role!r}.")
    return 0


def cmd_create_token(store: UserStore, args: argparse.Namespace) -> int:
    if args.user_id is not None:
        user_id = args.user_id
    else:
        user = store.get_user(args.username)
        if user is None:
            print(f"Error: no such user: {args.username!r}", file=sys.stderr)
            return 1
        user_id = user["id"]

    token = store.create_token(
        args.name, user_id, expires_at=args.expires_at, actor=args.actor, source_ip=args.source_ip,
    )
    print(f"Created token {args.name!r} for user_id={user_id}.")
    _print_secret_once("Token", token)
    return 0


def cmd_list_tokens(store: UserStore, args: argparse.Namespace) -> int:
    """
    List tokens so `revoke-token <id>` is actually usable.

    Before this existed, UserStore.list_tokens() had no caller at all and
    neither `list` nor `show` printed tokens -- so an operator following
    docs/CLIENTNODE.md's "find its id" instruction had no command that
    showed one, and token ids were recoverable only by reading auth_audit.
    """
    user_id = None
    if args.username is not None:
        user = store.get_user(args.username)
        if user is None:
            print(f"Error: no such user: {args.username!r}", file=sys.stderr)
            return 1
        user_id = user["id"]

    tokens = store.list_tokens(user_id)
    if not tokens:
        print("(no tokens)")
        return 0
    _print_table(
        tokens,
        ["id", "name", "username", "status", "expires_at", "last_used_at", "created_at"],
    )
    return 0


def cmd_unlock(store: UserStore, args: argparse.Namespace) -> int:
    store.unlock_user(args.username, actor=args.actor, source_ip=args.source_ip)
    print(f"Cleared lockout for {args.username!r}.")
    return 0


def cmd_revoke_token(store: UserStore, args: argparse.Namespace) -> int:
    store.revoke_token(args.token_id, actor=args.actor, source_ip=args.source_ip)
    print(f"Revoked token id={args.token_id}.")
    return 0


def cmd_audit(store: UserStore, args: argparse.Namespace) -> int:
    rows = store.recent_audit(username=args.username, event=args.event, limit=args.limit)
    if not rows:
        print("(no audit rows)")
        return 0
    for row in rows:
        detail = row.get("detail")
        detail_str = f"  {detail}" if detail else ""
        username = row.get("username") or "-"
        source_ip = row.get("source_ip") or "-"
        print(f"[{row['at']}] {row['event']:<16} user={username:<20} ip={source_ip}{detail_str}")
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------

def _add_actor_args(sp: argparse.ArgumentParser) -> None:
    """Shared --actor/--source-ip flags for every mutating subcommand (auth_audit's `detail.actor`, per store.py's audit convention)."""
    sp.add_argument(
        "--actor", default=_default_actor(),
        help=(
            "Operator performing this action, recorded in the audit log "
            "(default: cli:$USER/$USERNAME if either is set, otherwise omitted/unattributed -- "
            "pass this explicitly when running via `docker exec`, which sets neither)"
        ),
    )
    sp.add_argument(
        "--source-ip", default=None,
        help="Source IP to record in the audit log (default: none)",
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="user-manager.py",
        description=(
            "Alert Manager identity administration CLI -- thin wrapper over "
            "src/userstore/store.py's UserStore. Canonical replacement for "
            "tools/http-auth-manager.sh."
        ),
        epilog=(
            "Run inside the gateway container:\n"
            "  docker exec -it alert-http-gateway python3 tools/user-manager.py <command> ...\n\n"
            "Or as a one-off container if the gateway itself is down:\n"
            "  docker run --rm -it --env-file .env <image> python3 tools/user-manager.py <command> ...\n\n"
            "Examples:\n"
            "  user-manager.py init-admin\n"
            "  user-manager.py create-user jimi --default-client revysmc\n"
            "  user-manager.py create-role revysmc-viewer --description 'Read-only revysmc access'\n"
            "  user-manager.py add-scope revysmc-viewer revysmc logs:read\n"
            "  user-manager.py grant-role jimi revysmc-viewer\n"
            "  user-manager.py create-token jimi-cron --username jimi --expires-at 1893456000\n"
            "  user-manager.py audit --username jimi --limit 20\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", required=True)

    # -- init-admin -----------------------------------------------------
    sp = subparsers.add_parser(
        "init-admin",
        help="Bootstrap the first master_admin user (refuses if one already exists)",
        description=(
            "Create the first master_admin user. Refuses (non-zero exit) if any "
            "user already holds the master_admin role. Generates a random "
            "password and prints it to stdout exactly once -- save it immediately."
        ),
    )
    sp.add_argument("username", nargs="?", default="admin", help="Username for the new master_admin account (default: admin)")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_init_admin)

    # -- create-user ------------------------------------------------------
    sp = subparsers.add_parser("create-user", help="Create a new user account")
    sp.add_argument("username", help="Username for the new account")
    sp.add_argument("--password", default=None, help="Password for the new account (default: randomly generated and printed once)")
    sp.add_argument("--default-client", default=None, help="Default client this account is scoped to (optional)")
    sp.add_argument("--must-change-password", action="store_true", help="Force a password change on next login")
    sp.add_argument("--status", choices=["active", "disabled"], default="active", help="Initial account status (default: active)")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_create_user)

    # -- set-password -----------------------------------------------------
    sp = subparsers.add_parser("set-password", help="Set a user's password")
    sp.add_argument("username", help="Username to update")
    sp.add_argument("--password", default=None, help="New password (default: randomly generated and printed once)")
    sp.add_argument("--must-change-password", action="store_true", help="Force a password change on next login")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_set_password)

    # -- list ---------------------------------------------------------------
    sp = subparsers.add_parser("list", help="List all user accounts")
    sp.set_defaults(func=cmd_list)

    # -- show -----------------------------------------------------------
    sp = subparsers.add_parser("show", help="Show one user's account details")
    sp.add_argument("username", help="Username to show")
    sp.set_defaults(func=cmd_show)

    # -- disable / enable / delete ---------------------------------------
    sp = subparsers.add_parser("disable", help="Disable a user account")
    sp.add_argument("username", help="Username to disable")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_disable)

    sp = subparsers.add_parser("enable", help="Re-enable a disabled user account")
    sp.add_argument("username", help="Username to enable")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_enable)

    sp = subparsers.add_parser("unlock", help="Clear a locked-out account's lockout state")
    sp.add_argument("username", help="Username to unlock")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_unlock)

    sp = subparsers.add_parser("delete", help="Permanently delete a user account")
    sp.add_argument("username", help="Username to delete")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_delete)

    # -- create-role / grant-role / revoke-role ---------------------------
    sp = subparsers.add_parser("create-role", help="Create a new role")
    sp.add_argument("name", help="Role name")
    sp.add_argument("--description", default=None, help="Human-readable description")
    sp.add_argument("--is-system", action="store_true", help="Mark this as a system role")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_create_role)

    sp = subparsers.add_parser("grant-role", help="Grant a role to a user")
    sp.add_argument("username", help="Username to grant the role to")
    sp.add_argument("role", help="Role name")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_grant_role)

    sp = subparsers.add_parser("revoke-role", help="Revoke a role from a user")
    sp.add_argument("username", help="Username to revoke the role from")
    sp.add_argument("role", help="Role name")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_revoke_role)

    # -- add-scope / remove-scope ------------------------------------------
    sp = subparsers.add_parser("add-scope", help="Add a (client, permission) scope to a role")
    sp.add_argument("role", help="Role name")
    sp.add_argument("client", help="Client name, or '*' for all clients")
    sp.add_argument("permission", help="Permission, or '*' for all permissions")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_add_scope)

    sp = subparsers.add_parser("remove-scope", help="Remove a (client, permission) scope from a role")
    sp.add_argument("role", help="Role name")
    sp.add_argument("client", help="Client name, or '*' for all clients")
    sp.add_argument("permission", help="Permission, or '*' for all permissions")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_remove_scope)

    # -- create-token / revoke-token ---------------------------------------
    sp = subparsers.add_parser("create-token", help="Create a Bearer token for a user")
    sp.add_argument("name", help="Human-readable token name")
    owner_group = sp.add_mutually_exclusive_group(required=True)
    owner_group.add_argument("--username", default=None, help="Owning username (resolved to a user id)")
    owner_group.add_argument("--user-id", type=int, default=None, help="Owning user id directly")
    sp.add_argument("--expires-at", type=int, default=None, help="Unix epoch seconds expiry (default: never)")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_create_token)

    sp = subparsers.add_parser("list-tokens", help="List Bearer tokens (all users, or one)")
    sp.add_argument("--username", default=None, help="Limit to this user's tokens (default: all users)")
    sp.set_defaults(func=cmd_list_tokens)

    sp = subparsers.add_parser("revoke-token", help="Revoke a token by id")
    sp.add_argument("token_id", type=int, help="Token id to revoke")
    _add_actor_args(sp)
    sp.set_defaults(func=cmd_revoke_token)

    # -- audit ----------------------------------------------------------
    sp = subparsers.add_parser("audit", help="Show recent auth_audit rows")
    sp.add_argument("--username", default=None, help="Filter to this subject username")
    sp.add_argument("--event", default=None, help="Filter to this event type")
    sp.add_argument("--limit", type=int, default=100, help="Maximum rows to return (default: 100)")
    sp.set_defaults(func=cmd_audit)

    return parser


def main(argv: Optional[List[str]] = None, store: Optional[UserStore] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if store is None:
        store = UserStore.from_env()

    try:
        return args.func(store, args)
    except (UserStoreError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
