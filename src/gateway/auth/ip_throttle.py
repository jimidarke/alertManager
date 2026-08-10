"""
Alert Manager - gateway.auth.ip_throttle

`IPThrottle`: a per-source-IP failed-login counter, in-memory, correct
specifically because docs/OVERHAUL_PLAN.md section 4.9 commits this gateway
to a SINGLE gunicorn worker process (1 worker x 8 gthread threads) -- an
in-memory counter would silently under-count (or simply not share state at
all) across multiple worker processes, but with exactly one process it is
exact and needs no external store (Redis, etc.).

This is deliberately separate from -- and stacks with -- `UserStore`'s own
per-account lockout (Task 15: `LOCKOUT_THRESHOLD=5` failed attempts on ONE
username within `LOCKOUT_DURATION_SECONDS=900`): that mechanism protects one
account against being password-guessed from anywhere; this one protects the
login endpoint itself against being hammered from one source, regardless of
which username(s) it tries (20 failures/10 minutes/IP -> 429, per
docs/OVERHAUL_PLAN.md sections 4.3/4.4). Both checks apply on every
`POST /login` attempt -- see auth/routes.py.

Threshold is on FAILURES only, not successes -- a shared IP (NAT, office
network) legitimately mixes successful and failed logins across different
users; a success clearing the counter would let one user's good password
reset another, unrelated user's failure count on the same IP, which is not
what the plan's "20 failed logins per IP per 10 minutes" describes. There is
deliberately no `record_success()` -- nothing needs it.
"""

import threading
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict

MAX_FAILURES = 20
WINDOW_SECONDS = 600


class IPThrottle:
    """In-memory sliding-window failed-login counter, keyed by source IP. See module docstring."""

    MAX_FAILURES = MAX_FAILURES
    WINDOW_SECONDS = WINDOW_SECONDS

    def __init__(
        self,
        max_failures: int = MAX_FAILURES,
        window_seconds: float = WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._clock = clock
        self._failures: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune_locked(self, ip: str, now: float) -> None:
        """Drop timestamps older than the trailing window. Caller must hold `self._lock`."""
        dq = self._failures.get(ip)
        if dq is None:
            return
        cutoff = now - self._window_seconds
        while dq and dq[0] < cutoff:
            dq.popleft()
        if not dq:
            # Don't leak an empty deque per IP forever under sustained,
            # widely-distributed traffic (e.g. a scanner touching many IPs).
            del self._failures[ip]

    def is_blocked(self, ip: str) -> bool:
        """True if `ip` has already recorded >= max_failures failures within the trailing window."""
        now = self._clock()
        with self._lock:
            self._prune_locked(ip, now)
            return len(self._failures.get(ip, ())) >= self._max_failures

    def record_failure(self, ip: str) -> None:
        """Record one failed login attempt from `ip` at the current time."""
        now = self._clock()
        with self._lock:
            self._prune_locked(ip, now)
            self._failures[ip].append(now)

    def failure_count(self, ip: str) -> int:
        """Current (pruned) failure count for `ip` within the trailing window. Diagnostic/test seam."""
        now = self._clock()
        with self._lock:
            self._prune_locked(ip, now)
            return len(self._failures.get(ip, ()))
