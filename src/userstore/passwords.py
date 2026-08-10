"""
Alert Manager - userstore.passwords

Argon2id password hashing for the Phase 4 identity schema
(sql/migrations/0002_identity.sql: users.password_hash).

Uses argon2-cffi's PasswordHasher with its library defaults, which ARE
argon2id -- verified empirically (not just assumed) at the time this module
was written:

    >>> from argon2 import PasswordHasher, Type
    >>> PasswordHasher().type == Type.ID
    True
    >>> PasswordHasher().hash("x")
    '$argon2id$v=19$m=65536,t=3,p=4$...'

PasswordHasher()'s unmodified defaults are time_cost=3, memory_cost=65536
KiB (64 MiB), parallelism=4, hash_len=32, salt_len=16, type=Type.ID. These
are argon2-cffi's own considered, actively-maintained recommended
parameters (they get revised upward over time as hardware improves) and
this module deliberately does not override any of them -- CLAUDE.md's
"do not implement backwards compatibility unless explicitly asked" spirit
applies equally to freezing cost parameters early against future guidance.

Framework-free: only argon2-cffi is imported here. No web framework, no psycopg2.
"""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# One shared hasher instance -- PasswordHasher() is stateless/thread-safe
# aside from its fixed cost parameters, so a module-level singleton avoids
# re-reading those defaults on every call.
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    """
    Hash a plaintext password, returning an encoded argon2id hash string
    (e.g. '$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>').

    A fresh random salt is generated internally on every call, so hashing
    the same password twice produces two different (but both valid) hashes.
    """
    return _hasher.hash(password)


def verify_password(password: str, hash: str) -> bool:
    """
    Return True if `password` matches `hash`, False otherwise.

    Never raises: a wrong password (VerifyMismatchError), a hash encoded
    with parameters argon2-cffi can no longer verify (VerificationError),
    or a malformed/non-argon2 hash string (InvalidHashError) all simply
    result in False. Callers (UserStore.verify_login) rely on this -- a
    corrupt or foreign hash in the database must read as "wrong password",
    not crash the login path.
    """
    try:
        return _hasher.verify(hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hash: str) -> bool:
    """
    Return True if `hash` was encoded with parameters older/weaker than
    this module's current PasswordHasher defaults and should be
    regenerated (typically: verify the password successfully, then call
    hash_password() again and persist the new hash).
    """
    return _hasher.check_needs_rehash(hash)
