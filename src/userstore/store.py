"""
Alert Manager - userstore.store

Single owner of all account/role/scope/token/audit database operations
against the Phase 4 identity schema (sql/migrations/0002_identity.sql:
users, roles, user_roles, role_scopes, auth_tokens, auth_audit). Imported by
the gateway (Task 18+), the `user-manager` CLI (Task 16), and the one-off
users.json migration script (Task 17) -- none of which exist yet, which is
exactly why this module's interface is written to be self-contained and
framework-free: only `psycopg2` and this package's own `passwords` module
are imported here. No web framework of any kind.

Connection strategy: each public method opens its own short-lived
connection (see `_cursor()`), wrapped in a transaction that commits on
success and rolls back on any exception, then always closes the connection.
This is a deliberate choice over a long-lived pooled connection: account
operations (login, CLI admin actions, admin-UI form submits) are low
frequency, not a hot per-request path -- the one path that *could* be hot
(`verify_token`, called on every Bearer-token API request from node
devices) is designed in docs/OVERHAUL_PLAN.md section 4.4 to sit behind a
gateway-side CredentialCache specifically so this module's DB round trip is
the rare cache-miss case, not the common case. A future gateway task is
free to wrap a pooled connection behind the same `_cursor()` seam if
profiling ever shows otherwise; nothing here assumes connect-per-call.

Fail-fast, not fail-open: unlike pathways/email_log.py's EmailLogWriter
(email is a last-resort channel that must survive a tracking-DB outage),
this module is the *authority* on who is allowed to do what -- a DB error
here must propagate, never be silently swallowed. The only exceptions this
module defines (UserNotFoundError / RoleNotFoundError / TokenNotFoundError)
are deliberate "no such row" business-logic checks; genuine DB errors
(connection failure, a UNIQUE violation on a duplicate username, the
auth_tokens ON DELETE RESTRICT blocking delete_user while tokens exist)
are left to propagate as raw psycopg2 exceptions, per CLAUDE.md's
"do not implement failsafes" rule.

Username/client normalization: usernames and scope `client` values are
trimmed and lowercased before every lookup/write here, mirroring this
project's established string-normalization convention for
level/category/client on the alert-ingestion side (see CLAUDE.md "String
Normalization") and matching the DB's own
`chk_users_username_lowercase` CHECK constraint. Role names are NOT
normalized -- role_scopes/roles carry no such CHECK, and role naming
convention (`master_admin`, `<client>-node`, `<client>-viewer`, ...) is a
later task's (16/17) concern, not this module's.

Audit convention: every `auth_audit` row's `username` column holds the
*subject* of the event (the account the event is about -- e.g. the account
created/disabled/granted-a-role, or the account that attempted to log in),
never the operator performing an admin action. When a mutating method is
given an `actor` kwarg (the admin/session performing the action, distinct
from the subject), it is folded into the row's `detail` JSON as
`{"actor": "<actor>", ...event-specific fields}` rather than overwriting
`username`. This keeps "show one user's full history" (Task 16's `audit`
subcommand) a single `WHERE username = :subject` query.
"""

import hashlib
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, ClassVar, Dict, FrozenSet, List, Optional, Set, Union

import psycopg2
from psycopg2.extras import Json, RealDictCursor

from .passwords import hash_password, verify_password

# Postgres connect() timeout. Longer than pathways/email_log.py's 3s
# fail-open default: that module drops a slow connect attempt and moves on
# (email can wait), but this module's callers (a login attempt, an admin
# CLI command) should not spuriously fail just because the DB was briefly
# slow to accept a TCP connection.
CONNECT_TIMEOUT_SECONDS = 10

# Account lockout (docs/OVERHAUL_PLAN.md section 4.2): the 5th consecutive
# failed login attempt sets locked_until 900 seconds into the future.
LOCKOUT_THRESHOLD = 5
LOCKOUT_DURATION_SECONDS = 900

# Bearer token format: "amt_" + secrets.token_urlsafe(TOKEN_RANDOM_BYTES).
#
# secrets.token_urlsafe(nbytes) base64url-encodes nbytes of randomness and
# strips padding; the output length is NOT nbytes (it's the base64
# expansion, ceil(nbytes * 4 / 3) characters minus any stripped '='). The
# brief calls for exactly 43 characters after the "amt_" prefix.
# token_urlsafe(32) was verified empirically to produce exactly 43
# characters (32 bytes = 256 bits of randomness; 32 = 10*3 + 2, so base64
# emits 10 full 4-char groups (40 chars) plus one partial group encoding
# the trailing 2 bytes as 3 base64 chars + 1 '=' pad character, and
# token_urlsafe strips that trailing '=' -> 40 + 3 = 43):
#
#   >>> import secrets
#   >>> len(secrets.token_urlsafe(32))
#   43
#
# 32 bytes (256 bits) is also comfortably in line with secrets' own module
# docs recommendation of "at least 32 bytes" for security tokens, so this
# is not just a length-matching coincidence -- 32 is the right byte count
# on security grounds too.
TOKEN_PREFIX = "amt_"
TOKEN_RANDOM_BYTES = 32

# Mirrors sql/migrations/0002_identity.sql's chk_role_scopes_permission
# CHECK constraint exactly. Kept here so add_scope() can reject an invalid
# permission with a clear ValueError instead of a raw psycopg2
# CheckViolation -- defense-in-depth, same rationale the migration's own
# header comments give for enforcing constraints at the DB layer too.
ALLOWED_PERMISSIONS = frozenset({"*", "logs:read", "alerts:write"})

MASTER_ADMIN_ROLE = "master_admin"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class UserStoreError(Exception):
    """Base class for userstore.store's own "no such row" business errors."""


class UserNotFoundError(UserStoreError):
    def __init__(self, username: str):
        super().__init__(f"no such user: {username!r}")
        self.username = username


class RoleNotFoundError(UserStoreError):
    def __init__(self, role_name: str):
        super().__init__(f"no such role: {role_name!r}")
        self.role_name = role_name


class TokenNotFoundError(UserStoreError):
    def __init__(self, token_id: Any):
        super().__init__(f"no such token: {token_id!r}")
        self.token_id = token_id


# ---------------------------------------------------------------------------
# PrincipalData
# ---------------------------------------------------------------------------

class _AllClientsType:
    """
    Singleton sentinel returned by PrincipalData.visible_clients() to mean
    "every client, unrestricted" -- as opposed to a frozenset of specific
    client names. This is a distinct object, not a frozenset and not the
    string '*', specifically so callers cannot accidentally treat it as an
    iterable of two clients or confuse it with a legitimate (if unlikely)
    client literally named "*". Compare with `is ALL_CLIENTS`.

    Contract for callers (e.g. Task 22's dashboard scope predicate,
    docs/OVERHAUL_PLAN.md section 4.6): `visible_clients(perm) is
    ALL_CLIENTS` means "do not filter by client at all" (the principal may
    see every client's data for that permission, including rows with
    client IS NULL/''). A frozenset() return -- including the *empty*
    frozenset -- always means "filter to exactly these client(s)"; an empty
    frozenset therefore means "no clients visible for this permission",
    i.e. deny/show-nothing, not "show everything". Never treat the
    frozenset case as falsy-means-unrestricted.
    """

    _instance: ClassVar[Optional["_AllClientsType"]] = None

    def __new__(cls) -> "_AllClientsType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ALL_CLIENTS"


ALL_CLIENTS = _AllClientsType()


@dataclass(frozen=True)
class PrincipalData:
    """
    Everything an authorization decision needs about one authenticated
    account, assembled by UserStore.get_principal()/verify_login()/
    verify_token() from a single users -> user_roles -> roles ->
    role_scopes join.

    `user_id`/`username`/`status`/`must_change_password` go beyond the
    bare minimum the brief lists (roles/scopes/default_client) because
    downstream consumers already named in docs/OVERHAUL_PLAN.md need them:
    the gateway's hand-rolled session payload is literally `{uid, u, iat}`
    (section 4.4), and `must_change_password` drives the post-login
    forced-password-change redirect.

    `scopes` is `{client: frozenset(permissions)}` exactly as the brief
    specifies -- a plain dict, not a MappingProxyType. `@dataclass(frozen=True)`
    prevents reassigning `principal.scopes = ...`, but does not stop a
    caller from mutating the dict's contents in place; by convention,
    nothing in this codebase does that and callers should not either.
    """

    user_id: int
    username: str
    status: str
    default_client: Optional[str]
    must_change_password: bool
    roles: FrozenSet[str]
    scopes: Dict[str, FrozenSet[str]]

    def is_master_admin(self) -> bool:
        """True if this principal holds the master_admin system role seeded by 0002_identity.sql."""
        return MASTER_ADMIN_ROLE in self.roles

    def has_permission(self, permission: str, client: str) -> bool:
        """
        True if this principal holds `permission` for `client`, via any
        combination of the two wildcards a role_scopes row can carry:

        - a scope with client == '*' matches ANY requested client
        - a scope with permission == '*' matches ANY requested permission
        - a scope with BOTH ('*', '*') (master_admin's seeded scope)
          matches every (client, permission) pair unconditionally

        Checking both `self.scopes.get(client)` and `self.scopes.get('*')`
        covers every combination: a concrete-client scope's own
        permission set may itself contain '*' (wildcard permission on a
        specific client), and the '*'-client scope's permission set may
        contain either the exact permission or '*' (wildcard client,
        possibly also wildcard permission).

        `client` is compared verbatim -- NOT normalized here. Callers must
        pass an already-lowercased value, exactly as UserStore.add_scope()
        normalizes and stores it and as alert ingestion normalizes
        `alerts.client` (CLAUDE.md "String Normalization"); a mismatched
        case would silently read as "no scope for this client".
        """
        for scope_client in (client, "*"):
            perms = self.scopes.get(scope_client)
            if perms and ("*" in perms or permission in perms):
                return True
        return False

    def visible_clients(self, permission: str) -> Union[FrozenSet[str], _AllClientsType]:
        """
        Return the set of clients for which this principal holds
        `permission`, OR the ALL_CLIENTS sentinel (see its docstring above)
        if a client=='*' scope grants it -- meaning "unrestricted", not
        "the literal client named *".

        A concrete-client scope whose own permission set contains '*'
        (wildcard permission on that one client) counts that client as
        visible for every permission, including `permission`.
        """
        wildcard_perms = self.scopes.get("*", frozenset())
        if "*" in wildcard_perms or permission in wildcard_perms:
            return ALL_CLIENTS

        return frozenset(
            client
            for client, perms in self.scopes.items()
            if client != "*" and ("*" in perms or permission in perms)
        )


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _normalize_client(client: str) -> str:
    return client.strip().lower()


def _detail(actor: Optional[str], **extra: Any) -> Optional[Dict[str, Any]]:
    """Build an auth_audit `detail` dict: event-specific fields plus 'actor' if given, or None if empty."""
    d = {k: v for k, v in extra.items() if v is not None}
    if actor is not None:
        d["actor"] = actor
    return d or None


# ---------------------------------------------------------------------------
# UserStore
# ---------------------------------------------------------------------------

class UserStore:
    """Single owner of all account/role/scope/token/audit operations against the identity schema."""

    _USER_COLUMNS = (
        "id, username, status, default_client, must_change_password, "
        "failed_attempts, locked_until, last_login_at, created_at, updated_at"
    )

    _PRINCIPAL_QUERY = """
        SELECT u.id AS user_id, u.username, u.status, u.default_client,
               u.must_change_password,
               r.name AS role_name,
               rs.client AS scope_client, rs.permission AS scope_permission
        FROM users u
        LEFT JOIN user_roles ur ON ur.user_id = u.id
        LEFT JOIN roles r ON r.id = ur.role_id
        LEFT JOIN role_scopes rs ON rs.role_id = r.id
        WHERE u.username = %s
    """

    def __init__(self, dsn: Dict[str, Any]):
        """
        Args:
            dsn: psycopg2 connection kwargs (host, port, database, user,
                password). Use UserStore.from_env() for the standard
                POSTGRES_* environment-variable-derived instance.
        """
        self._dsn = dsn

    @classmethod
    def from_env(cls) -> "UserStore":
        """
        Build a UserStore from the same POSTGRES_* environment variables
        src/database_logger.py and src/db_migrate.py read (host/port/db/
        user/password), so every consumer of this module (gateway, CLI,
        migration script) points at the same database with zero
        additional configuration.
        """
        return cls({
            "host": os.getenv("POSTGRES_HOST", "localhost"),
            "port": int(os.getenv("POSTGRES_PORT", 5432)),
            "database": os.getenv("POSTGRES_DB", "alertmanager"),
            "user": os.getenv("POSTGRES_USER", "alertmanager"),
            "password": os.getenv("POSTGRES_PASSWORD", ""),
        })

    # -- connection plumbing -------------------------------------------------

    @contextmanager
    def _cursor(self):
        """
        Open a connection, yield a RealDictCursor for the duration of one
        transaction (commit on clean exit, rollback on exception), then
        always close the connection. See module docstring for why this is
        connect-per-call rather than a pooled/long-lived connection.
        """
        conn = psycopg2.connect(connect_timeout=CONNECT_TIMEOUT_SECONDS, **self._dsn)
        try:
            with conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    yield cur
        finally:
            conn.close()

    def _write_audit(
        self, cur, event: str, username: Optional[str], source_ip: Optional[str], detail: Optional[Dict[str, Any]]
    ) -> None:
        cur.execute(
            "INSERT INTO auth_audit (at, event, username, source_ip, detail) VALUES (%s, %s, %s, %s, %s)",
            (int(time.time()), event, username, source_ip, Json(detail) if detail is not None else None),
        )

    def _fetch_principal(self, cur, username: str) -> Optional[PrincipalData]:
        """
        Shared by get_principal() and verify_login()/verify_token() (which
        call this against their own already-open cursor/transaction so a
        successful login/token check costs one connection, not two).
        """
        cur.execute(self._PRINCIPAL_QUERY, (username,))
        rows = cur.fetchall()
        if not rows:
            return None

        roles = set()
        scopes: Dict[str, set] = {}
        for row in rows:
            if row["role_name"] is not None:
                roles.add(row["role_name"])
            if row["scope_client"] is not None and row["scope_permission"] is not None:
                scopes.setdefault(row["scope_client"], set()).add(row["scope_permission"])

        first = rows[0]
        return PrincipalData(
            user_id=first["user_id"],
            username=first["username"],
            status=first["status"],
            default_client=first["default_client"],
            must_change_password=first["must_change_password"],
            roles=frozenset(roles),
            scopes={client: frozenset(perms) for client, perms in scopes.items()},
        )

    # -- account CRUD ---------------------------------------------------------

    def create_user(
        self,
        username: str,
        password: str,
        *,
        default_client: Optional[str] = None,
        must_change_password: bool = False,
        status: str = "active",
        actor: Optional[str] = None,
        source_ip: Optional[str] = None,
    ) -> int:
        """Create a new user account (password stored as an argon2id hash) and return its id."""
        username_norm = _normalize_username(username)
        password_hash = hash_password(password)
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (
                    username, password_hash, status, default_client,
                    must_change_password, failed_attempts, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, 0, %s, %s)
                RETURNING id
                """,
                (username_norm, password_hash, status, default_client, must_change_password, now, now),
            )
            user_id = cur.fetchone()["id"]
            self._write_audit(cur, "user_created", username_norm, source_ip, _detail(actor, user_id=user_id))
        return user_id

    def get_user(self, username: str) -> Optional[Dict[str, Any]]:
        """Return one user's public row (never includes password_hash), or None if no such user."""
        username_norm = _normalize_username(username)
        with self._cursor() as cur:
            cur.execute(f"SELECT {self._USER_COLUMNS} FROM users WHERE username = %s", (username_norm,))
            row = cur.fetchone()
        return dict(row) if row else None

    def list_users(self) -> List[Dict[str, Any]]:
        """Return every user's public row (never includes password_hash), ordered by username."""
        with self._cursor() as cur:
            cur.execute(f"SELECT {self._USER_COLUMNS} FROM users ORDER BY username")
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def set_password(
        self,
        username: str,
        new_password: str,
        *,
        must_change_password: bool = False,
        actor: Optional[str] = None,
        source_ip: Optional[str] = None,
    ) -> None:
        """
        Set a new password hash for an existing user. Also clears any
        lockout state (failed_attempts=0, locked_until=NULL) -- an admin
        (or the user, via a forced change) setting a new password is a
        legitimate, deliberate path to un-stick a locked-out account, same
        as a successful login would.
        """
        username_norm = _normalize_username(username)
        password_hash = hash_password(new_password)
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s, must_change_password = %s,
                    failed_attempts = 0, locked_until = NULL, updated_at = %s
                WHERE username = %s
                RETURNING id
                """,
                (password_hash, must_change_password, now, username_norm),
            )
            if cur.fetchone() is None:
                raise UserNotFoundError(username_norm)
            self._write_audit(cur, "password_changed", username_norm, source_ip, _detail(actor))

    def disable_user(self, username: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None) -> None:
        username_norm = _normalize_username(username)
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                "UPDATE users SET status = 'disabled', updated_at = %s WHERE username = %s RETURNING id",
                (now, username_norm),
            )
            if cur.fetchone() is None:
                raise UserNotFoundError(username_norm)
            self._write_audit(cur, "user_disabled", username_norm, source_ip, _detail(actor))

    def enable_user(self, username: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None) -> None:
        """
        Re-activate a disabled account. Also clears any lockout state, on
        the same reasoning as set_password() above -- an admin explicitly
        re-enabling an account clearly intends for it to be usable again
        immediately, not still locked out from before it was disabled.
        """
        username_norm = _normalize_username(username)
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET status = 'active', failed_attempts = 0, locked_until = NULL, updated_at = %s
                WHERE username = %s
                RETURNING id
                """,
                (now, username_norm),
            )
            if cur.fetchone() is None:
                raise UserNotFoundError(username_norm)
            self._write_audit(cur, "user_enabled", username_norm, source_ip, _detail(actor))

    def unlock_user(self, username: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None) -> None:
        """
        Clear lockout state (failed_attempts -> 0, locked_until -> NULL)
        without otherwise touching the account.

        LOCKOUT_THRESHOLD consecutive failures lock an account for
        LOCKOUT_DURATION_SECONDS. Before this method existed, the only ways
        out were waiting for the lock to expire, a successful login, or
        set_password()/enable_user() clearing it as a side effect -- so an
        operator's only lever for "unlock this account, change nothing else"
        was to reset a password that was never the problem. Deliberately
        does NOT change `status`: unlocking a *disabled* account leaves it
        disabled, because those are two separate operator decisions.
        """
        username_norm = _normalize_username(username)
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET failed_attempts = 0, locked_until = NULL, updated_at = %s
                WHERE username = %s
                RETURNING id
                """,
                (now, username_norm),
            )
            if cur.fetchone() is None:
                raise UserNotFoundError(username_norm)
            self._write_audit(cur, "user_unlocked", username_norm, source_ip, _detail(actor))

    def delete_user(self, username: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None) -> None:
        """
        Permanently delete a user (user_roles rows cascade; any auth_tokens
        row still referencing this user blocks the delete with a raw
        psycopg2 IntegrityError -- ON DELETE RESTRICT, by design, per
        sql/migrations/0002_identity.sql's auth_tokens.user_id comment:
        tokens must be explicitly revoked/cleaned up first). Not caught
        here -- fail fast, per CLAUDE.md.
        """
        username_norm = _normalize_username(username)
        with self._cursor() as cur:
            cur.execute("DELETE FROM users WHERE username = %s RETURNING id", (username_norm,))
            if cur.fetchone() is None:
                raise UserNotFoundError(username_norm)
            self._write_audit(cur, "user_deleted", username_norm, source_ip, _detail(actor))

    # -- login / principal / tokens -------------------------------------------

    def verify_login(
        self, username: str, password: str, *, source_ip: Optional[str] = None
    ) -> Optional[PrincipalData]:
        """
        Authenticate by username/password, enforcing (in this exact order,
        per the brief) status='active', not expired, and not locked --
        ALL checked before any password comparison is attempted, so a
        locked or disabled account never pays (or leaks timing about) an
        argon2 hash comparison.

        Lockout: on a wrong password, failed_attempts increments; the
        attempt that brings it to exactly LOCKOUT_THRESHOLD (5) also sets
        locked_until = now + LOCKOUT_DURATION_SECONDS (900) and is audited
        as event='lockout' instead of 'login_failed'. On success,
        failed_attempts resets to 0, locked_until clears, last_login_at
        updates. Every call writes exactly one auth_audit row (never zero,
        never two) -- login_success, login_failed, or lockout.

        Judgment call (not explicit in the brief, but necessary to define
        exactly what "exactly 5" means across more than one lockout cycle
        on the same account): once a previously-set locked_until is found
        to be in the past, this method treats that as the lock naturally
        expiring and resets failed_attempts to 0 *before* evaluating the
        current attempt -- so a fresh streak of failures after a lockout
        expires needs its own 5 failures to re-lock, rather than the
        counter climbing forever and re-locking on every single failure
        after the first lockout. Without this reset, failed_attempts would
        never return to 0 except on a successful login, and "exactly 5"
        would only ever be true once per account, ever.

        Returns the authenticated PrincipalData on success, None on any
        failure (nonexistent username, disabled, expired, locked, or wrong
        password) -- deliberately not distinguishing *why* in the return
        value, matching a generic "invalid username or password" UI
        response (docs/OVERHAUL_PLAN.md section 4.5); the *why* is in the
        audit trail's `detail`, not the return value.
        """
        username_norm = _normalize_username(username)
        now = int(time.time())

        with self._cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, status, locked_until, failed_attempts
                FROM users
                WHERE username = %s
                """,
                (username_norm,),
            )
            row = cur.fetchone()

            if row is None:
                self._write_audit(cur, "login_failed", username_norm, source_ip, {"reason": "no_such_user"})
                return None

            if row["status"] != "active":
                self._write_audit(cur, "login_failed", username_norm, source_ip, {"reason": "disabled"})
                return None

            failed_attempts = row["failed_attempts"]
            if row["locked_until"] is not None:
                if row["locked_until"] > now:
                    self._write_audit(
                        cur, "login_failed", username_norm, source_ip,
                        {"reason": "locked", "locked_until": row["locked_until"]},
                    )
                    return None
                # Lock has expired: treat this as a fresh start for the counter (see docstring).
                failed_attempts = 0
                cur.execute(
                    "UPDATE users SET locked_until = NULL, failed_attempts = 0, updated_at = %s WHERE id = %s",
                    (now, row["id"]),
                )

            if not verify_password(password, row["password_hash"]):
                failed_attempts += 1
                if failed_attempts >= LOCKOUT_THRESHOLD:
                    cur.execute(
                        "UPDATE users SET failed_attempts = %s, locked_until = %s, updated_at = %s WHERE id = %s",
                        (failed_attempts, now + LOCKOUT_DURATION_SECONDS, now, row["id"]),
                    )
                    self._write_audit(
                        cur, "lockout", username_norm, source_ip, {"failed_attempts": failed_attempts}
                    )
                else:
                    cur.execute(
                        "UPDATE users SET failed_attempts = %s, updated_at = %s WHERE id = %s",
                        (failed_attempts, now, row["id"]),
                    )
                    self._write_audit(
                        cur, "login_failed", username_norm, source_ip,
                        {"reason": "bad_password", "failed_attempts": failed_attempts},
                    )
                return None

            cur.execute(
                """
                UPDATE users
                SET failed_attempts = 0, locked_until = NULL, last_login_at = %s, updated_at = %s
                WHERE id = %s
                """,
                (now, now, row["id"]),
            )
            self._write_audit(cur, "login_success", username_norm, source_ip, None)

            return self._fetch_principal(cur, username_norm)

    def get_principal(self, username: str) -> Optional[PrincipalData]:
        """
        Build a user's full PrincipalData (roles + {client: permission set}
        scopes) via one users -> user_roles -> roles -> role_scopes join.
        Returns None if no user with that username exists (including a
        user with zero roles/scopes -- LEFT JOINs ensure that case still
        returns one row per user, so it is never confused with
        "user does not exist").
        """
        username_norm = _normalize_username(username)
        with self._cursor() as cur:
            return self._fetch_principal(cur, username_norm)

    def create_token(
        self,
        name: str,
        user_id: int,
        *,
        expires_at: Optional[int] = None,
        actor: Optional[str] = None,
        source_ip: Optional[str] = None,
    ) -> str:
        """
        Create a new Bearer token for user_id and return the PLAINTEXT
        token -- the only time it is ever available. Only its SHA-256 hex
        digest (token_sha256) is persisted; the plaintext is not
        recoverable afterward.

        Format: TOKEN_PREFIX ("amt_") + secrets.token_urlsafe(TOKEN_RANDOM_BYTES)
        -- see the module-level comment above TOKEN_RANDOM_BYTES for why 32
        bytes produces exactly the 43 characters after the prefix the
        brief calls for.
        """
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_RANDOM_BYTES)
        token_sha256 = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                INSERT INTO auth_tokens (name, token_sha256, user_id, status, expires_at, created_at)
                VALUES (%s, %s, %s, 'active', %s, %s)
                RETURNING id
                """,
                (name, token_sha256, user_id, expires_at, now),
            )
            token_id = cur.fetchone()["id"]
            cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            owner_row = cur.fetchone()
            subject = owner_row["username"] if owner_row else None
            self._write_audit(
                cur, "token_created", subject, source_ip, _detail(actor, token_id=token_id, name=name)
            )
        return plaintext

    def verify_token(self, plaintext: str) -> Optional[PrincipalData]:
        """
        Authenticate a Bearer token: hash `plaintext` with SHA-256, look it
        up by token_sha256, and check the token's own status/expiry AND
        (judgment call: not spelled out in the brief for verify_token
        specifically, but the users.status column comment makes clear it
        applies "to authenticate" in general, not just password login -- a
        disabled *account* must not authenticate via ANY credential, token
        included) the owning user's status.
        Does not check the user's locked_until: lockout exists specifically
        to slow down password-guessing (docs/OVERHAUL_PLAN.md section 4.2),
        a concern that does not apply to a token, which is looked up by
        exact hash match rather than guessed incrementally.

        On success, updates last_used_at and returns the token owner's
        PrincipalData. Unlike verify_login, this does not write an
        auth_audit row on every call -- the brief specifies audit writes
        for verify_login explicitly and is silent for verify_token, and
        this method sits on a per-request hot path (every node-device
        POST /alert, per section 4.4) where a per-call audit row would be
        excessive audit volume for what is normally a routine check, not a
        security event in its own right.
        """
        token_sha256 = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        now = int(time.time())
        with self._cursor() as cur:
            cur.execute(
                """
                SELECT t.id AS token_id, t.status AS token_status, t.expires_at AS token_expires_at,
                       u.username, u.status AS user_status
                FROM auth_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token_sha256 = %s
                """,
                (token_sha256,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            if row["token_status"] != "active":
                return None
            if row["token_expires_at"] is not None and row["token_expires_at"] <= now:
                return None
            if row["user_status"] != "active":
                return None

            cur.execute("UPDATE auth_tokens SET last_used_at = %s WHERE id = %s", (now, row["token_id"]))

            return self._fetch_principal(cur, row["username"])

    def list_tokens(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Return tokens (active and revoked) most recently created first --
        every token in the system when `user_id` is None, otherwise just
        that user's.

        Includes the owner's `username` via a join so callers can render a
        whole-system listing without an N+1 lookup. Never returns token
        material: only the SHA-256 is stored, and it is not selected here.
        """
        sql = """
            SELECT t.id, t.name, t.user_id, u.username, t.status,
                   t.expires_at, t.last_used_at, t.created_at
            FROM auth_tokens t
            JOIN users u ON u.id = t.user_id
        """
        params: tuple = ()
        if user_id is not None:
            sql += " WHERE t.user_id = %s"
            params = (user_id,)
        sql += " ORDER BY t.created_at DESC"

        with self._cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def revoke_token(self, token_id: int, *, actor: Optional[str] = None, source_ip: Optional[str] = None) -> None:
        """Soft-delete a token (status='revoked'); the row is kept so its history stays in auth_audit."""
        with self._cursor() as cur:
            cur.execute(
                "UPDATE auth_tokens SET status = 'revoked' WHERE id = %s RETURNING user_id, name",
                (token_id,),
            )
            row = cur.fetchone()
            if row is None:
                raise TokenNotFoundError(token_id)
            cur.execute("SELECT username FROM users WHERE id = %s", (row["user_id"],))
            owner_row = cur.fetchone()
            subject = owner_row["username"] if owner_row else None
            self._write_audit(
                cur, "token_revoked", subject, source_ip, _detail(actor, token_id=token_id, name=row["name"])
            )

    # -- role / scope CRUD -----------------------------------------------------

    def create_role(
        self,
        name: str,
        description: Optional[str] = None,
        is_system: bool = False,
        *,
        actor: Optional[str] = None,
        source_ip: Optional[str] = None,
    ) -> int:
        with self._cursor() as cur:
            cur.execute(
                "INSERT INTO roles (name, description, is_system) VALUES (%s, %s, %s) RETURNING id",
                (name, description, is_system),
            )
            role_id = cur.fetchone()["id"]
            self._write_audit(cur, "role_created", None, source_ip, _detail(actor, role=name))
        return role_id

    def list_roles(self) -> List[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT id, name, description, is_system FROM roles ORDER BY name")
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def get_role(self, name: str) -> Optional[Dict[str, Any]]:
        with self._cursor() as cur:
            cur.execute("SELECT id, name, description, is_system FROM roles WHERE name = %s", (name,))
            row = cur.fetchone()
        return dict(row) if row else None

    def grant_role(
        self, username: str, role_name: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None
    ) -> None:
        username_norm = _normalize_username(username)
        with self._cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username_norm,))
            user_row = cur.fetchone()
            if user_row is None:
                raise UserNotFoundError(username_norm)

            cur.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role_row = cur.fetchone()
            if role_row is None:
                raise RoleNotFoundError(role_name)

            cur.execute(
                "INSERT INTO user_roles (user_id, role_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (user_row["id"], role_row["id"]),
            )
            self._write_audit(cur, "role_granted", username_norm, source_ip, _detail(actor, role=role_name))

    def revoke_role(
        self, username: str, role_name: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None
    ) -> None:
        username_norm = _normalize_username(username)
        with self._cursor() as cur:
            cur.execute("SELECT id FROM users WHERE username = %s", (username_norm,))
            user_row = cur.fetchone()
            if user_row is None:
                raise UserNotFoundError(username_norm)

            cur.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role_row = cur.fetchone()
            if role_row is None:
                raise RoleNotFoundError(role_name)

            cur.execute(
                "DELETE FROM user_roles WHERE user_id = %s AND role_id = %s",
                (user_row["id"], role_row["id"]),
            )
            self._write_audit(cur, "role_revoked", username_norm, source_ip, _detail(actor, role=role_name))

    def add_scope(
        self, role_name: str, client: str, permission: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None
    ) -> None:
        if permission not in ALLOWED_PERMISSIONS:
            raise ValueError(f"invalid permission {permission!r}; must be one of {sorted(ALLOWED_PERMISSIONS)}")
        client_norm = _normalize_client(client)
        with self._cursor() as cur:
            cur.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role_row = cur.fetchone()
            if role_row is None:
                raise RoleNotFoundError(role_name)

            cur.execute(
                "INSERT INTO role_scopes (role_id, client, permission) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (role_row["id"], client_norm, permission),
            )
            self._write_audit(
                cur, "scope_added", None, source_ip,
                _detail(actor, role=role_name, client=client_norm, permission=permission),
            )

    def remove_scope(
        self, role_name: str, client: str, permission: str, *, actor: Optional[str] = None, source_ip: Optional[str] = None
    ) -> None:
        client_norm = _normalize_client(client)
        with self._cursor() as cur:
            cur.execute("SELECT id FROM roles WHERE name = %s", (role_name,))
            role_row = cur.fetchone()
            if role_row is None:
                raise RoleNotFoundError(role_name)

            cur.execute(
                "DELETE FROM role_scopes WHERE role_id = %s AND client = %s AND permission = %s",
                (role_row["id"], client_norm, permission),
            )
            self._write_audit(
                cur, "scope_removed", None, source_ip,
                _detail(actor, role=role_name, client=client_norm, permission=permission),
            )

    def list_role_scopes(self, role_id: int) -> List[Dict[str, Any]]:
        """
        Return every (client, permission) row `role_scopes` holds for
        `role_id`, ordered by client then permission -- the read-only
        counterpart to `add_scope()`/`remove_scope()` that was missing from
        this class's surface (an API gap flagged in the final whole-branch
        review: `list_roles()`/`get_role()` return only `id, name,
        description, is_system`, nothing about a role's actual scopes).
        Added specifically so callers like `gateway/admin/routes.py`'s
        per-user "personal role" scope reconciliation (diffing a submitted
        edit form against a role's CURRENT scopes) can read `role_scopes`
        through `UserStore` instead of issuing their own raw SQL against
        `app.extensions['db_pool']` -- this module remains the single owner
        of every account/role/scope/token/audit DB operation, per its own
        module docstring, with no exception carved out for reads.

        Does not use `UserStore`'s aggregated cross-role `get_principal()`
        join: that join mixes every role a user holds into one
        `{client: permission set}` dict and cannot be un-mixed back into a
        single role's own contribution once a user holds more than one
        role, which is exactly the case this method needs to support.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT client, permission FROM role_scopes WHERE role_id = %s ORDER BY client, permission",
                (role_id,),
            )
            rows = cur.fetchall()
        return [dict(row) for row in rows]

    def list_all_scope_clients(self) -> Set[str]:
        """
        Return the set of distinct, real (non-null, non-empty,
        non-wildcard) client names appearing anywhere in `role_scopes`,
        across every role. Purpose-built for `gateway/admin/routes.py`'s
        client-autocomplete datalist, which UNIONs this against
        `alerts.client` (a different table that is legitimately NOT this
        module's business -- alert data, not identity data) to build one
        suggestion list; kept as its own small primitive rather than
        forcing `list_role_scopes()` (which is naturally scoped to ONE
        role_id) to also serve this "distinct clients across ALL roles"
        shape.

        The `'*'` wildcard sentinel is excluded -- it is not a real client
        name to ever suggest in an autocomplete list.
        """
        with self._cursor() as cur:
            cur.execute(
                "SELECT DISTINCT client FROM role_scopes WHERE client IS NOT NULL AND client != '' AND client != '*'"
            )
            rows = cur.fetchall()
        return {row["client"] for row in rows}

    # -- audit -------------------------------------------------------------

    def audit(
        self,
        event: str,
        username: Optional[str] = None,
        source_ip: Optional[str] = None,
        detail: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Write one arbitrary auth_audit row. Exposed for callers (e.g. the gateway) with events this module has no dedicated method for."""
        with self._cursor() as cur:
            self._write_audit(cur, event, username, source_ip, detail)

    def recent_audit(
        self, *, username: Optional[str] = None, event: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, Any]]:
        """Return auth_audit rows, most recent first, optionally filtered by subject username and/or event."""
        query = "SELECT id, at, event, username, source_ip, detail FROM auth_audit WHERE 1 = 1"
        params: List[Any] = []
        if username is not None:
            query += " AND username = %s"
            params.append(_normalize_username(username))
        if event is not None:
            query += " AND event = %s"
            params.append(event)
        query += " ORDER BY at DESC LIMIT %s"
        params.append(limit)

        with self._cursor() as cur:
            cur.execute(query, tuple(params))
            rows = cur.fetchall()
        return [dict(row) for row in rows]
