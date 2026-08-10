"""
Tests for the Task 4 healthcheck wiring added to src/analytics_engine.py:

- HEARTBEAT_FILE env-var-with-default convention, mirroring the pattern
  Task 3 established in src/database_logger.py (see src/utils/heartbeat.py).
- heartbeat.beat() called once per healthy iteration of the 60s main loop
  in run(), after schedule.run_pending() succeeds and before the 60s sleep;
  NOT called on an iteration where schedule.run_pending() raises (the
  existing except branch logs and sleeps without touching the heartbeat).

No live scheduler/email/analyzer work is exercised here -- email pathway
setup, analyzer loading, and analyzer scheduling are mocked out so only
this module's own control flow around the heartbeat call is under test.
Signal handling is also mocked so constructing an AnalyticsEngine in-process
doesn't mutate the test runner's real SIGINT/SIGTERM handlers.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))  # analytics_engine.py imports the top-level `pathways` package
from analytics_engine import AnalyticsEngine  # noqa: E402


@pytest.fixture
def make_engine(mocker, tmp_path):
    def _make(analytics_config=None):
        config = {"analytics": analytics_config if analytics_config is not None else {}}
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))

        # Don't let constructing an AnalyticsEngine in-process install real
        # SIGINT/SIGTERM handlers for this test process.
        mocker.patch('analytics_engine.signal.signal')
        mocker.patch.object(
            AnalyticsEngine, '_setup_logging',
            return_value=logging.getLogger('test_analytics_engine')
        )

        return AnalyticsEngine(str(config_path))

    return _make


# ---------------------------------------------------------------------------
# HEARTBEAT_FILE env-var-with-default convention
# ---------------------------------------------------------------------------

def test_heartbeat_path_defaults_when_env_unset(make_engine, monkeypatch):
    monkeypatch.delenv('HEARTBEAT_FILE', raising=False)
    engine = make_engine()
    assert engine.heartbeat_path == '/data/logs/ops/analytics-engine.heartbeat'


def test_heartbeat_path_reads_from_env(make_engine, monkeypatch):
    monkeypatch.setenv('HEARTBEAT_FILE', '/data/logs/ops/custom-analytics.heartbeat')
    engine = make_engine()
    assert engine.heartbeat_path == '/data/logs/ops/custom-analytics.heartbeat'


# ---------------------------------------------------------------------------
# run(): heartbeat.beat() called after each successful scheduler tick,
# before sleep; skipped on an iteration that raises
# ---------------------------------------------------------------------------

def _prime_for_run(engine, mocker):
    """
    Bypass the real setup/scheduling machinery (irrelevant to the heartbeat
    wiring under test) while still satisfying run()'s
    `if not self.analyzers: sys.exit(1)` startup gate.
    """
    mocker.patch.object(engine, '_setup_email_pathway')
    mocker.patch.object(engine, '_load_analyzers')
    mocker.patch.object(engine, '_schedule_analyzers')
    engine.analyzers = {'dummy': object()}


def test_run_beats_heartbeat_after_scheduler_tick_each_cycle(make_engine, mocker):
    engine = make_engine()
    _prime_for_run(engine, mocker)

    order = []
    mocker.patch('analytics_engine.schedule.run_pending', side_effect=lambda: order.append('tick'))
    mock_beat = mocker.patch(
        'analytics_engine.heartbeat.beat',
        side_effect=lambda path: order.append(('beat', path))
    )

    def fake_sleep(seconds):
        order.append(('sleep', seconds))
        engine.running = False  # stop after exactly one loop iteration

    mocker.patch('analytics_engine.time.sleep', side_effect=fake_sleep)

    engine.run()

    assert order == ['tick', ('beat', engine.heartbeat_path), ('sleep', 60)]
    mock_beat.assert_called_once_with(engine.heartbeat_path)


def test_run_beats_heartbeat_once_per_iteration_across_multiple_cycles(make_engine, mocker):
    engine = make_engine()
    _prime_for_run(engine, mocker)

    mocker.patch('analytics_engine.schedule.run_pending')
    mock_beat = mocker.patch('analytics_engine.heartbeat.beat')

    call_count = {'n': 0}

    def fake_sleep(seconds):
        call_count['n'] += 1
        if call_count['n'] >= 3:
            engine.running = False

    mocker.patch('analytics_engine.time.sleep', side_effect=fake_sleep)

    engine.run()

    assert mock_beat.call_count == 3
    mock_beat.assert_called_with(engine.heartbeat_path)


def test_run_skips_heartbeat_on_exception_in_scheduler_tick(make_engine, mocker):
    # schedule.run_pending() raising must be caught by the existing
    # except-and-continue branch, logged, and must NOT reach the heartbeat
    # call for that iteration -- a failing cycle should not look healthy.
    engine = make_engine()
    _prime_for_run(engine, mocker)

    mocker.patch('analytics_engine.schedule.run_pending', side_effect=Exception("scheduler boom"))
    mock_beat = mocker.patch('analytics_engine.heartbeat.beat')
    error_log = mocker.patch.object(engine.logger, 'error')

    def fake_sleep(seconds):
        engine.running = False

    mocker.patch('analytics_engine.time.sleep', side_effect=fake_sleep)

    engine.run()  # must not raise

    mock_beat.assert_not_called()
    error_log.assert_called_once()
    assert "scheduler boom" in error_log.call_args.args[0]


def test_run_exits_1_when_no_analyzers_loaded(make_engine, mocker):
    engine = make_engine()
    mocker.patch.object(engine, '_setup_email_pathway')
    mocker.patch.object(engine, '_load_analyzers')  # leaves engine.analyzers == {}
    mock_beat = mocker.patch('analytics_engine.heartbeat.beat')

    with pytest.raises(SystemExit) as exc_info:
        engine.run()

    assert exc_info.value.code == 1
    mock_beat.assert_not_called()
