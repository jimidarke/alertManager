"""
Tests for src/gateway/auth/ip_throttle.py (Task 19): IPThrottle's sliding
10-minute/20-failure window, per-IP isolation, and success-does-not-reset
semantics.

A `clock` callable is injected instead of sleeping real time.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway.auth.ip_throttle import IPThrottle  # noqa: E402


class FakeClock:
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
def throttle(clock):
    return IPThrottle(clock=clock)


def test_defaults_match_the_plan():
    assert IPThrottle.MAX_FAILURES == 20
    assert IPThrottle.WINDOW_SECONDS == 600


def test_not_blocked_with_no_failures(throttle):
    assert throttle.is_blocked("1.2.3.4") is False


def test_not_blocked_below_threshold(throttle):
    for _ in range(19):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is False
    assert throttle.failure_count("1.2.3.4") == 19


def test_blocked_at_exactly_threshold(throttle):
    for _ in range(20):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True


def test_blocked_beyond_threshold(throttle):
    for _ in range(25):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True


def test_different_ips_are_independent(throttle):
    for _ in range(20):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True
    assert throttle.is_blocked("5.6.7.8") is False
    assert throttle.failure_count("5.6.7.8") == 0


def test_failures_outside_the_window_expire(throttle, clock):
    for _ in range(20):
        throttle.record_failure("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4") is True

    clock.advance(601)  # just past WINDOW_SECONDS (600)
    assert throttle.is_blocked("1.2.3.4") is False
    assert throttle.failure_count("1.2.3.4") == 0


def test_partial_window_expiry_only_prunes_old_failures(throttle, clock):
    for _ in range(10):
        throttle.record_failure("1.2.3.4")  # t=0: 10 failures

    clock.advance(601)  # these 10 have now expired

    for _ in range(15):
        throttle.record_failure("1.2.3.4")  # t=601: 15 fresh failures

    # Only the 15 recent failures should count -- the first 10 pruned away.
    assert throttle.failure_count("1.2.3.4") == 15
    assert throttle.is_blocked("1.2.3.4") is False


def test_sliding_window_blocks_once_recent_failures_reach_threshold(throttle, clock):
    for _ in range(10):
        throttle.record_failure("1.2.3.4")

    clock.advance(300)  # still within the window

    for _ in range(10):
        throttle.record_failure("1.2.3.4")  # 20 total, all within the last 600s

    assert throttle.is_blocked("1.2.3.4") is True


def test_no_record_success_method_exists():
    # Deliberate design choice (see module docstring): successes never
    # reset a shared IP's failure count, so there is no such method at all.
    assert not hasattr(IPThrottle(), "record_success")


def test_is_blocked_does_not_itself_record_a_failure(throttle):
    # Checking whether an IP is blocked must be side-effect-free.
    for _ in range(19):
        throttle.record_failure("1.2.3.4")
    throttle.is_blocked("1.2.3.4")
    throttle.is_blocked("1.2.3.4")
    assert throttle.failure_count("1.2.3.4") == 19
