"""
Tests for src/gateway/auth/cache.py (Task 19): CredentialCache's full
fresh/stale/miss/unavailable decision matrix, the per-boot-salted fast hash
(constant-time, credential-specific, cache-dump-safe across restarts), and
`invalidate()`/`entry_count()`'s diagnostic seams.

A `clock` callable is injected everywhere instead of sleeping real time, so
every FRESH_TTL/STALE_GRACE boundary is tested exactly and fast.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway.auth.cache import CredentialCache, CredentialCacheUnavailable  # noqa: E402


class FakeClock:
    """A controllable monotonic-style clock: starts at 0, advances only when told to."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def cache(clock):
    return CredentialCache(clock=clock)


PRINCIPAL = object()  # stand-in for a PrincipalData -- CredentialCache treats results opaquely


# ---------------------------------------------------------------------------
# Class-level constants match the plan's documented values
# ---------------------------------------------------------------------------

def test_default_ttls_match_the_plan():
    assert CredentialCache.FRESH_TTL == 60
    assert CredentialCache.STALE_GRACE == 300


# ---------------------------------------------------------------------------
# 1. MISS -- no entry yet
# ---------------------------------------------------------------------------

def test_miss_calls_verify_and_caches_positive_result(cache):
    calls = []

    def verify():
        calls.append(1)
        return PRINCIPAL

    result = cache.check("alice", "hunter2", verify)

    assert result is PRINCIPAL
    assert len(calls) == 1
    assert cache.entry_count() == 1


def test_miss_calls_verify_and_caches_negative_result(cache):
    def verify():
        return None

    result = cache.check("alice", "wrongpass", verify)

    assert result is None
    assert cache.entry_count() == 1  # negative results are cached too


def test_miss_with_no_entry_and_verify_raises_gives_unavailable(cache):
    def verify():
        raise ConnectionError("db down")

    with pytest.raises(CredentialCacheUnavailable):
        cache.check("alice", "hunter2", verify)

    assert cache.entry_count() == 0  # nothing trustworthy was ever cached


# ---------------------------------------------------------------------------
# 2. FRESH HIT (age < FRESH_TTL)
# ---------------------------------------------------------------------------

def test_fresh_hit_returns_cached_result_without_calling_verify(cache, clock):
    calls = []
    cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])
    assert len(calls) == 1

    clock.advance(59)  # still < FRESH_TTL (60)
    result = cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])

    assert result is PRINCIPAL
    assert len(calls) == 1  # verify() NOT called again -- fresh hit


def test_fresh_hit_at_exactly_ttl_boundary_is_no_longer_fresh(cache, clock):
    # age < FRESH_TTL is fresh; age == FRESH_TTL is stale (boundary is exclusive).
    calls = []
    cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])

    clock.advance(60)  # age == FRESH_TTL exactly
    cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])

    assert len(calls) == 2  # second call re-verified (stale path), not a fresh hit


def test_fresh_hit_with_different_password_is_not_a_hit(cache):
    # Different credential for the same key must not reuse the cached entry
    # at all -- this is the fast-hash-must-match requirement.
    calls = []
    cache.check("alice", "correct-password", lambda: (calls.append("first"), PRINCIPAL)[1])

    result = cache.check("alice", "different-password", lambda: (calls.append("second"), None)[1])

    assert result is None
    assert calls == ["first", "second"]  # verify() WAS called for the mismatched credential


# ---------------------------------------------------------------------------
# 3. STALE within grace (FRESH_TTL <= age < STALE_GRACE)
# ---------------------------------------------------------------------------

def test_stale_within_grace_live_check_succeeds_repopulates_cache(cache, clock):
    cache.check("alice", "hunter2", lambda: PRINCIPAL)

    clock.advance(100)  # stale, within grace (< 300)
    new_result = object()
    result = cache.check("alice", "hunter2", lambda: new_result)

    assert result is new_result  # fresh live result used, not the old cached object


def test_stale_within_grace_live_check_raises_serves_stale_with_warning(cache, clock, caplog):
    cache.check("alice", "hunter2", lambda: PRINCIPAL)

    clock.advance(100)  # stale, within grace

    def verify():
        raise ConnectionError("db blip")

    with caplog.at_level("WARNING"):
        result = cache.check("alice", "hunter2", verify)

    assert result is PRINCIPAL  # stale result served
    assert any("stale" in record.message.lower() for record in caplog.records)
    assert any(record.levelname == "WARNING" for record in caplog.records)


def test_stale_within_grace_mismatched_credential_has_no_fallback(cache, clock):
    cache.check("alice", "correct-password", lambda: PRINCIPAL)
    clock.advance(100)

    def verify():
        raise ConnectionError("db down")

    # A different (wrong) password presented during the outage cannot fall
    # back to a stale result cached for a DIFFERENT credential.
    with pytest.raises(CredentialCacheUnavailable):
        cache.check("alice", "wrong-password", verify)


# ---------------------------------------------------------------------------
# 4. TOO STALE (age >= STALE_GRACE)
# ---------------------------------------------------------------------------

def test_beyond_grace_live_check_succeeds_repopulates_cache(cache, clock):
    cache.check("alice", "hunter2", lambda: PRINCIPAL)

    clock.advance(400)  # beyond STALE_GRACE (300)
    new_result = object()
    result = cache.check("alice", "hunter2", lambda: new_result)

    assert result is new_result


def test_beyond_grace_live_check_raises_gives_unavailable(cache, clock, caplog):
    cache.check("alice", "hunter2", lambda: PRINCIPAL)

    clock.advance(400)  # beyond STALE_GRACE

    def verify():
        raise ConnectionError("db still down")

    with caplog.at_level("ERROR"):
        with pytest.raises(CredentialCacheUnavailable):
            cache.check("alice", "hunter2", verify)

    assert any(record.levelname == "ERROR" for record in caplog.records)


def test_beyond_grace_at_exactly_stale_grace_boundary_is_unavailable(cache, clock):
    # age >= STALE_GRACE is "too stale" (boundary is inclusive of unavailability).
    cache.check("alice", "hunter2", lambda: PRINCIPAL)
    clock.advance(300)  # age == STALE_GRACE exactly

    with pytest.raises(CredentialCacheUnavailable):
        cache.check("alice", "hunter2", lambda: (_ for _ in ()).throw(ConnectionError("down")))


# ---------------------------------------------------------------------------
# Per-boot-salted fast hash: constant-time, credential-specific, not persisted
# ---------------------------------------------------------------------------

def test_two_cache_instances_have_different_salts(clock):
    cache_a = CredentialCache(clock=clock)
    cache_b = CredentialCache(clock=clock)
    assert cache_a._salt != cache_b._salt


def test_salt_is_32_bytes_of_randomness(cache):
    assert isinstance(cache._salt, bytes)
    assert len(cache._salt) == 32


def test_cached_entry_never_stores_the_plaintext_credential(cache):
    cache.check("alice", "super-secret-password", lambda: PRINCIPAL)

    for entry in cache._entries.values():
        assert entry.credential_hash != b"super-secret-password"
        assert b"super-secret-password" not in entry.credential_hash


def test_different_instances_produce_different_hashes_for_the_same_password(clock):
    # Demonstrates that a cache dump from one process (one salt) is
    # meaningless against a fresh process (a new salt) -- the same password
    # hashes differently per-instance.
    cache_a = CredentialCache(clock=clock)
    cache_b = CredentialCache(clock=clock)

    cache_a.check("alice", "hunter2", lambda: PRINCIPAL)
    cache_b.check("alice", "hunter2", lambda: PRINCIPAL)

    hash_a = cache_a._entries["alice"].credential_hash
    hash_b = cache_b._entries["alice"].credential_hash
    assert hash_a != hash_b


# ---------------------------------------------------------------------------
# invalidate() / entry_count() -- manual seams
# ---------------------------------------------------------------------------

def test_invalidate_drops_the_entry_forcing_a_fresh_verify(cache):
    calls = []
    cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])
    assert cache.entry_count() == 1

    cache.invalidate("alice")
    assert cache.entry_count() == 0

    cache.check("alice", "hunter2", lambda: (calls.append(1), PRINCIPAL)[1])
    assert len(calls) == 2  # re-verified -- no stale entry survived invalidation


def test_invalidate_unknown_key_is_a_no_op(cache):
    cache.invalidate("no-such-key")  # must not raise
    assert cache.entry_count() == 0


def test_entry_count_tracks_distinct_keys(cache):
    cache.check("alice", "pw1", lambda: PRINCIPAL)
    cache.check("bob", "pw2", lambda: PRINCIPAL)
    assert cache.entry_count() == 2


# ---------------------------------------------------------------------------
# Different keys never interfere with each other
# ---------------------------------------------------------------------------

def test_independent_keys_do_not_share_cache_state(cache):
    cache.check("alice", "hunter2", lambda: "alice-principal")

    calls = []

    def bob_verify():
        calls.append(1)
        return "bob-principal"

    result = cache.check("bob", "swordfish", bob_verify)

    assert result == "bob-principal"
    assert len(calls) == 1  # bob's key never hit alice's fresh cache entry
