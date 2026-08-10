"""
Alert Manager - userstore

Framework-free (psycopg2 + argon2-cffi only -- no web framework of any
kind) single owner of all account/role/scope/token/audit database
operations against the Phase 4 identity schema
(sql/migrations/0002_identity.sql). Imported by the gateway and by
tools/user-manager.py.

Typical usage::

    from userstore import UserStore, ALL_CLIENTS

    store = UserStore.from_env()
    principal = store.verify_login("jimi", "hunter2", source_ip="1.2.3.4")
    if principal is not None and principal.has_permission("logs:read", "revysmc"):
        ...
"""

from .passwords import hash_password, needs_rehash, verify_password
from .store import (
    ALL_CLIENTS,
    ALLOWED_PERMISSIONS,
    LOCKOUT_DURATION_SECONDS,
    LOCKOUT_THRESHOLD,
    MASTER_ADMIN_ROLE,
    TOKEN_PREFIX,
    TOKEN_RANDOM_BYTES,
    PrincipalData,
    RoleNotFoundError,
    TokenNotFoundError,
    UserNotFoundError,
    UserStore,
    UserStoreError,
)

__all__ = [
    "hash_password",
    "verify_password",
    "needs_rehash",
    "UserStore",
    "PrincipalData",
    "ALL_CLIENTS",
    "ALLOWED_PERMISSIONS",
    "MASTER_ADMIN_ROLE",
    "LOCKOUT_THRESHOLD",
    "LOCKOUT_DURATION_SECONDS",
    "TOKEN_PREFIX",
    "TOKEN_RANDOM_BYTES",
    "UserStoreError",
    "UserNotFoundError",
    "RoleNotFoundError",
    "TokenNotFoundError",
]
