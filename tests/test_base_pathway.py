"""
Tests for pathways/base_pathway.py's Task 10 addition: the generic
async_errors counter (BasePathway.async_errors / increment_async_errors()).

This is deliberately pathway-agnostic -- EmailPathway is the first (and
so far only) caller, wiring it in when its storm-prevention timer-thread
flush fails (see tests/test_email_pathway.py), but the counter itself
lives on BasePathway so any future pathway with background/async work can
use it the same way. Tested here against a minimal concrete subclass so
these tests don't depend on any particular pathway's send_alert() logic.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from pathways.base_pathway import BasePathway  # noqa: E402


class _ConcretePathway(BasePathway):
    """Smallest possible concrete subclass -- just to exercise BasePathway."""

    def send_alert(self, alert):
        return True

    def validate_config(self):
        return True


def _pathway(**config):
    return _ConcretePathway('test_pathway', config)


def test_async_errors_starts_at_zero():
    p = _pathway()
    assert p.async_errors == 0


def test_increment_async_errors_default_adds_one():
    p = _pathway()
    p.increment_async_errors()
    assert p.async_errors == 1


def test_increment_async_errors_accumulates_across_calls():
    p = _pathway()
    p.increment_async_errors()
    p.increment_async_errors()
    p.increment_async_errors()
    assert p.async_errors == 3


def test_increment_async_errors_accepts_explicit_count():
    p = _pathway()
    p.increment_async_errors(5)
    assert p.async_errors == 5


def test_async_errors_is_per_instance_not_shared_across_pathways():
    a = _pathway()
    b = _pathway()
    a.increment_async_errors(2)
    assert a.async_errors == 2
    assert b.async_errors == 0
