"""
Alert Manager - gateway.auth.cache

CredentialCache fronts the DB-backed credential checks `UserStore.verify_login()`
(Basic auth: username+password, argon2) and `UserStore.verify_token()` (Bearer:
opaque token, sha256 lookup) so a burst of high-frequency node `POST /alert`
traffic (docs/OVERHAUL_PLAN.md section 4.4) doesn't pay a DB round trip (and,
for verify_login, an expensive argon2 hash comparison) on every single request.

This module builds and thoroughly tests the cache itself. Wiring it into
gateway.principal's `_resolve_basic`/`_resolve_bearer` (or calling it directly
from Task 20's `api` blueprint) is deliberately left to Task 20 -- see this
module's "Integration note for Task 20" section below. Task 19's job is
`auth/` (routes.py, cache.py, ip_throttle.py) plus session/CSRF mechanics, not
the `/alert` route itself; wiring a shared cache instance into the two
resolvers Task 18 already implemented is exactly the kind of change that
should happen alongside the route that actually needs the speedup, so its
tests can exercise the real end-to-end behavior instead of this task guessing
at Task 20's shape.

--------------------------------------------------------------------------
Decision matrix (FRESH_TTL=60s, STALE_GRACE=300s)
--------------------------------------------------------------------------
`check(key, credential, verify)` is called with:
  - `key`: a cache identity, e.g. a username (Basic) or a token's own hash
    (Bearer) -- whatever uniquely identifies "the same credential slot".
  - `credential`: the raw secret being verified this request (password or
    token plaintext) -- hashed (per-boot-salted, see below) so a cache dump
    never contains a comparable, restart-durable credential.
  - `verify`: a zero-arg callable that performs the real, authoritative
    check (typically `lambda: user_store.verify_login(username, password,
    source_ip=...)` or `lambda: user_store.verify_token(token)`), called
    only when the cache cannot answer from memory alone.

Outcomes, in priority order:
  1. No entry for `key`, or the entry's stored credential-hash does not
     match this request's `credential` (a different password/token was
     presented than the one currently cached) -- MISS. Always calls
     `verify()`:
       - succeeds (returns a PrincipalData or None) -> cache the result
         (positive OR negative) under `key` with a fresh timestamp, return it.
       - raises -> no cached result is trustworthy for THIS credential
         (serving some other stale entry here would mean answering for a
         credential we never actually validated) -> raise
         `CredentialCacheUnavailable`.
  2. Entry matches `credential`, age < FRESH_TTL -- FRESH HIT. Returns the
     cached result immediately. `verify()` is NOT called at all -- this is
     the whole point (no DB, no argon2, per fresh request).
  3. Entry matches `credential`, FRESH_TTL <= age < STALE_GRACE -- STALE
     but within grace. Always calls `verify()` (a stale entry is not served
     just because it's convenient -- only as a fallback if the live check
     itself fails):
       - succeeds -> cache the fresh result, return it.
       - raises -> log a WARNING and return the stale cached result (age is
         still within STALE_GRACE, an acceptable bounded trade-off during a
         DB blip).
  4. Entry matches `credential`, age >= STALE_GRACE -- TOO STALE. Always
     calls `verify()`:
       - succeeds -> cache the fresh result, return it.
       - raises -> too old to trust -> raise `CredentialCacheUnavailable`.

Callers (Task 20's `/alert` route) turn `CredentialCacheUnavailable` into a
503 response -- this is why it is a distinct exception type rather than,
say, `check()` returning `None` on failure (which already has a meaning:
"not authenticated" -> 401 -- and must never be confused with "could not
even determine that").

--------------------------------------------------------------------------
Per-boot-salted fast hash
--------------------------------------------------------------------------
`CredentialCache.__init__` generates `secrets.token_bytes(32)` once, held
only in this process's memory for its lifetime (never persisted, never
logged). Every cached credential is stored as `hmac.new(salt,
credential.encode(), sha256).digest()`, compared with
`hmac.compare_digest()` (constant-time, so cache lookups don't leak timing
information about how much of a candidate credential matched). If the
process restarts (deploy, crash, OOM), the salt is regenerated and every
previously-cached hash becomes meaningless -- a memory dump of the old
process's cache reveals nothing usable against a *new* process, and even
within one process's lifetime the cache never stores the credential
itself, only a salted digest of it.

--------------------------------------------------------------------------
Invalidation on password change / disable -- design decision
--------------------------------------------------------------------------
(See also Task 19's brief and docs/OVERHAUL_PLAN.md 4.4's "Invalidated on
password change/disable".) `UserStore` (Task 15) has no callback/observer
hook today for "a credential just changed" -- confirmed by grep before
writing this module. Two options were on the table:

  (a) Add a minimal hook to UserStore (set_password/disable_user/
      enable_user each accept an optional callback, or a module-level
      subscriber list) so this cache can be told to evict a key the
      instant it changes.
  (b) Rely purely on FRESH_TTL/STALE_GRACE: a disabled account or changed
      password is only ever served from a STALE cache entry for at most
      STALE_GRACE (300s) -- and only when the live DB check itself is
      unreachable, since (per the decision matrix above) `verify()` is
      re-attempted, not skipped, on every stale hit. The *only* time a
      stale (now-wrong) result survives is when Postgres itself is down.

This module chooses **(b)**. Reasoning:
  - This cache is scoped to the high-frequency node `/alert` ingestion path
    (docs/OVERHAUL_PLAN.md 4.4's own framing: "no DB/argon2 per node
    POST"), not human login -- `POST /login` (auth/routes.py) always calls
    `UserStore.verify_login()` directly, never through this cache, so a
    password change/disable is reflected immediately for every *future*
    login attempt regardless of this cache's state.
  - Session-based auth (gateway/principal.py's `_resolve_session`, also
    this task) re-fetches the principal's live `status` from the DB on
    EVERY request -- a disabled account's browser session is killed on its
    very next page load, with zero staleness window at all. The staleness
    window this module introduces is strictly narrower in scope (Basic/
    Bearer node credentials only) than the session path, which has none.
  - A hook-based design (a) requires every mutation path that can
    invalidate a cached credential (set_password, disable_user, and
    arguably enable_user/delete_user) to reliably call back into a
    gateway-side cache instance it has no other reference to -- across
    three different callers (gateway routes, the CLI, the one-off
    migration script) -- and a single missed call site quietly
    reintroduces the exact staleness window (b) already bounds explicitly
    and by design. (b) gets the same worst-case bound (five minutes, and
    only during a simultaneous DB outage) with no additional coupling
    between the framework-free `userstore` package and a gateway-only
    concern.
  - Five minutes of "a just-disabled node can still POST one more alert,
    but only while Postgres is also unreachable" is an explicitly
    acceptable trade-off for this project (nodes are trusted devices on an
    internal network, not adversarial actors); it is a materially
    different, and much smaller, risk than a compromised *human* login
    session persisting, which is why the two paths are handled
    differently above.

If a future requirement demands sub-second invalidation (e.g. a compromised
device's token must stop working immediately, DB or no DB), option (a) is
the right escalation -- it is not built here since nothing in this task's
brief or the plan's testing checklist calls for it, and `invalidate()`
below exists as a manual seam for exactly that future extension without
having to change this module's public shape.

--------------------------------------------------------------------------
Integration note for Task 20
--------------------------------------------------------------------------
Task 20's `/alert` route (Basic/Bearer only, per `API_ONLY` in
gateway/principal.py) is the intended caller. A single shared instance is
already wired onto `app.extensions["credential_cache"]` by `create_app()`
(gateway/__init__.py):

    from flask import current_app, request, abort
    from gateway.auth.cache import CredentialCacheUnavailable

    cache = current_app.extensions["credential_cache"]
    try:
        data = cache.check(
            key=f"basic:{username}",
            credential=password,
            verify=lambda: user_store.verify_login(username, password, source_ip=request.remote_addr),
        )
    except CredentialCacheUnavailable:
        abort(503)
    if data is None:
        abort(401)

A Bearer check follows the same shape with `key=f"bearer:{token}"` and
`verify=lambda: user_store.verify_token(token)`. The `key` prefix
("basic:"/"bearer:") keeps the two credential spaces from ever colliding if
the same literal string were somehow presented as both a username and a
token.
"""

import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Dict, TypeVar

logger = logging.getLogger("gateway.auth.cache")

# See module docstring's decision matrix.
FRESH_TTL = 60
STALE_GRACE = 300

T = TypeVar("T")


class CredentialCacheUnavailable(Exception):
    """
    Raised by `CredentialCache.check()` when neither a cached result NOR a
    live call to `verify()` can answer for the presented credential -- the
    authoritative check raised, and either there is no cached entry for
    this exact credential at all, or the cached entry is older than
    STALE_GRACE. Callers (Task 20's `/alert` route) should turn this into a
    503 response, per docs/OVERHAUL_PLAN.md 4.4 ("beyond -> 503 on /alert").
    """


@dataclass
class _Entry:
    result: object  # typically Optional[PrincipalData]; untyped here so this module needn't import userstore
    credential_hash: bytes
    cached_at: float  # per this cache's own `clock`


class CredentialCache:
    """Fresh/stale/miss/unavailable credential cache. See module docstring for the full decision matrix."""

    FRESH_TTL = FRESH_TTL
    STALE_GRACE = STALE_GRACE

    def __init__(
        self,
        fresh_ttl: float = FRESH_TTL,
        stale_grace: float = STALE_GRACE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._fresh_ttl = fresh_ttl
        self._stale_grace = stale_grace
        self._clock = clock
        # Per-boot only -- regenerated every process start, never persisted
        # or logged. See module docstring's "Per-boot-salted fast hash".
        self._salt = secrets.token_bytes(32)
        self._entries: Dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def _hash(self, credential: str) -> bytes:
        return hmac.new(self._salt, credential.encode("utf-8"), sha256).digest()

    def check(self, key: str, credential: str, verify: Callable[[], T]) -> T:
        """
        Return whatever `verify()` would return (typically
        `Optional[PrincipalData]`), either served from cache or freshly
        computed. Raises `CredentialCacheUnavailable` if `verify()` raises
        and no trustworthy cached fallback exists for this exact
        credential. See module docstring for the full decision matrix.
        """
        credential_hash = self._hash(credential)
        now = self._clock()

        with self._lock:
            entry = self._entries.get(key)

        if entry is not None and hmac.compare_digest(entry.credential_hash, credential_hash):
            age = now - entry.cached_at

            if age < self._fresh_ttl:
                return entry.result  # FRESH HIT -- verify() never called.

            # STALE (within or beyond grace): always attempt a live check;
            # a stale entry is only ever a fallback, never served preferentially.
            try:
                result = verify()
            except Exception:
                if age < self._stale_grace:
                    logger.warning(
                        "CredentialCache: live check failed for key=%r; serving stale cached result (age=%.1fs)",
                        key, age, exc_info=True,
                    )
                    return entry.result
                logger.error(
                    "CredentialCache: live check failed for key=%r and cached entry is beyond STALE_GRACE (age=%.1fs)",
                    key, age, exc_info=True,
                )
                raise CredentialCacheUnavailable(f"credential check unavailable for {key!r}")
            self._store(key, credential_hash, result, now)
            return result

        # MISS: no entry for this key, or the entry belongs to a different credential.
        try:
            result = verify()
        except Exception:
            logger.error(
                "CredentialCache: live check failed for key=%r with no matching cached entry to fall back on",
                key, exc_info=True,
            )
            raise CredentialCacheUnavailable(f"credential check unavailable for {key!r}")
        self._store(key, credential_hash, result, now)
        return result

    def _store(self, key: str, credential_hash: bytes, result: object, at: float) -> None:
        with self._lock:
            self._entries[key] = _Entry(result=result, credential_hash=credential_hash, cached_at=at)

    def invalidate(self, key: str) -> None:
        """
        Drop any cached entry for `key` outright. Not wired to any
        UserStore hook today (see module docstring's invalidation design
        decision) -- exposed as a manual/test seam and for a future caller
        that DOES have an explicit "this credential just changed" signal.
        """
        with self._lock:
            self._entries.pop(key, None)

    def entry_count(self) -> int:
        """Number of distinct keys currently cached. Diagnostic/test seam only."""
        with self._lock:
            return len(self._entries)
