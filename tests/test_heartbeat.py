"""
Tests for src/utils/heartbeat.py

Pure filesystem I/O, no DB or network involved -- these run for real, with
no mocking, exercising beat()/check() against tmp_path.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from utils import heartbeat  # noqa: E402


# ---------------------------------------------------------------------------
# beat()
# ---------------------------------------------------------------------------

def test_beat_writes_current_epoch_timestamp(tmp_path):
    path = tmp_path / "svc.heartbeat"

    before = int(time.time())
    heartbeat.beat(str(path))
    after = int(time.time())

    written = int(path.read_text().strip())
    assert before <= written <= after


def test_beat_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "nested" / "does" / "not" / "exist" / "svc.heartbeat"
    assert not path.parent.exists()

    heartbeat.beat(str(path))

    assert path.exists()
    assert int(path.read_text().strip()) > 0


def test_beat_overwrites_previous_value(tmp_path):
    path = tmp_path / "svc.heartbeat"

    path.write_text("1")  # deliberately stale/bogus prior value
    heartbeat.beat(str(path))

    written = int(path.read_text().strip())
    assert written > 1
    assert abs(written - time.time()) < 5


def test_beat_with_no_parent_component_in_path(tmp_path, monkeypatch):
    # A bare filename (no directory component) must not blow up on
    # os.path.dirname(path) == "".
    monkeypatch.chdir(tmp_path)
    heartbeat.beat("bare.heartbeat")

    assert (tmp_path / "bare.heartbeat").exists()


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------

def test_check_true_immediately_after_beat(tmp_path):
    path = tmp_path / "svc.heartbeat"
    heartbeat.beat(str(path))

    assert heartbeat.check(str(path)) is True


def test_check_false_when_file_missing(tmp_path):
    path = tmp_path / "does_not_exist.heartbeat"

    assert heartbeat.check(str(path)) is False


def test_check_false_when_stale_beyond_max_age(tmp_path):
    path = tmp_path / "svc.heartbeat"
    stale_timestamp = int(time.time()) - 1000
    path.write_text(str(stale_timestamp))

    assert heartbeat.check(str(path), max_age=150) is False


def test_check_true_when_within_max_age(tmp_path):
    path = tmp_path / "svc.heartbeat"
    recent_timestamp = int(time.time()) - 30
    path.write_text(str(recent_timestamp))

    assert heartbeat.check(str(path), max_age=150) is True


def test_check_default_max_age_is_150(tmp_path):
    path = tmp_path / "svc.heartbeat"
    path.write_text(str(int(time.time()) - 149))
    assert heartbeat.check(str(path)) is True

    path.write_text(str(int(time.time()) - 151))
    assert heartbeat.check(str(path)) is False


def test_check_false_on_corrupt_non_integer_content(tmp_path):
    path = tmp_path / "svc.heartbeat"
    path.write_text("not-a-timestamp")

    assert heartbeat.check(str(path)) is False


def test_check_false_on_empty_file(tmp_path):
    path = tmp_path / "svc.heartbeat"
    path.write_text("")

    assert heartbeat.check(str(path)) is False


def test_check_tolerates_surrounding_whitespace(tmp_path):
    path = tmp_path / "svc.heartbeat"
    path.write_text(f"  {int(time.time())}  \n")

    assert heartbeat.check(str(path)) is True


@pytest.mark.parametrize("max_age", [1, 60, 86400])
def test_check_respects_custom_max_age(tmp_path, max_age):
    path = tmp_path / "svc.heartbeat"
    path.write_text(str(int(time.time())))

    assert heartbeat.check(str(path), max_age=max_age) is True

    path.write_text(str(int(time.time()) - max_age - 10))
    assert heartbeat.check(str(path), max_age=max_age) is False


def test_check_max_age_zero_rejects_any_nonzero_age(tmp_path):
    # max_age=0 is a valid (if extreme) input: only a beat written in the
    # exact current integer second still counts as fresh.
    path = tmp_path / "svc.heartbeat"
    path.write_text(str(int(time.time()) - 1))

    assert heartbeat.check(str(path), max_age=0) is False


def test_round_trip_beat_then_check_multiple_times(tmp_path):
    path = tmp_path / "svc.heartbeat"

    for _ in range(3):
        heartbeat.beat(str(path))
        assert heartbeat.check(str(path)) is True
