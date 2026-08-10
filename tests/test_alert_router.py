"""
Tests for src/alert_router.py.

Task 4 healthcheck wiring:
- HEARTBEAT_FILE env-var-with-default convention, mirroring the pattern
  Task 3 established in src/database_logger.py (see src/utils/heartbeat.py).
- heartbeat.beat() called once per iteration of the 60s status-publish loop
  in run(), immediately after publish_service_status('running') and before
  the 60s sleep -- so the compose healthcheck (heartbeat.check()) sees a
  fresh file shortly after startup and every ~60s thereafter.

Task 10 publish_service_status() pathway_errors/async_errors merge:
- Each pathway's BasePathway.async_errors count (e.g. EmailPathway's
  storm-prevention timer-thread flush failures) is merged into the same
  per-pathway pathway_errors count used for synchronous send_alert()
  failures, without double-counting across repeated publishes and without
  mutating self.stats['pathway_errors'] itself.

No live MQTT broker is available in this environment (and per project rule,
docker/docker-compose is never invoked from automation). MQTT connect/loop
and pathway loading are mocked out; only this module's own control flow
around the heartbeat call and status-publish merge is under test. Signal
handling is also mocked so constructing an AlertRouter in-process doesn't
mutate the test runner's real SIGINT/SIGTERM handlers.
"""

import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from alert_router import AlertRouter  # noqa: E402


def _fake_setup_logging(self):
    self.logger = logging.getLogger('test_alert_router')


@pytest.fixture
def make_router(mocker, tmp_path):
    def _make(config_overrides=None):
        config = {
            "mqtt": {"broker": "test-broker", "port": 1883},
            "pathways": {"console_alerts": {"type": "console", "enabled": True}},
        }
        if config_overrides:
            config.update(config_overrides)

        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))

        # Don't let constructing an AlertRouter in-process install real
        # SIGINT/SIGTERM handlers for this test process.
        mocker.patch('alert_router.signal.signal')
        mocker.patch.object(AlertRouter, 'setup_logging', _fake_setup_logging)
        # Skip real pathway construction (console/file/mqtt/led pathway
        # objects) -- irrelevant to the heartbeat wiring under test here.
        mocker.patch.object(AlertRouter, 'load_pathways')

        return AlertRouter(str(config_path))

    return _make


# ---------------------------------------------------------------------------
# HEARTBEAT_FILE env-var-with-default convention
# ---------------------------------------------------------------------------

def test_heartbeat_path_defaults_when_env_unset(make_router, monkeypatch):
    monkeypatch.delenv('HEARTBEAT_FILE', raising=False)
    router = make_router()
    assert router.heartbeat_path == '/data/logs/ops/alert-router.heartbeat'


def test_heartbeat_path_reads_from_env(make_router, monkeypatch):
    monkeypatch.setenv('HEARTBEAT_FILE', '/data/logs/ops/alert-file-logger.heartbeat')
    router = make_router()
    assert router.heartbeat_path == '/data/logs/ops/alert-file-logger.heartbeat'


# ---------------------------------------------------------------------------
# run(): heartbeat.beat() called after each status publish, before sleep
# ---------------------------------------------------------------------------

def test_run_beats_heartbeat_after_status_publish_each_cycle(make_router, mocker):
    router = make_router()

    def fake_setup_mqtt(self):
        self.mqtt_client = mocker.MagicMock(name="mqtt_client")
        return True

    mocker.patch.object(AlertRouter, 'setup_mqtt', fake_setup_mqtt)

    order = []
    mocker.patch.object(
        router, 'publish_service_status',
        side_effect=lambda status: order.append(('status', status))
    )
    mock_beat = mocker.patch(
        'alert_router.heartbeat.beat',
        side_effect=lambda path: order.append(('beat', path))
    )

    def fake_sleep(seconds):
        order.append(('sleep', seconds))
        router.running = False  # stop after exactly one loop iteration

    mocker.patch('alert_router.time.sleep', side_effect=fake_sleep)

    router.run()

    # One full loop iteration: publish 'running' -> beat -> sleep(60), then
    # the loop condition is re-checked (False) and shutdown runs, which
    # publishes 'offline' as its own separate step.
    assert order == [
        ('status', 'running'),
        ('beat', router.heartbeat_path),
        ('sleep', 60),
        ('status', 'offline'),
    ]
    mock_beat.assert_called_once_with(router.heartbeat_path)


def test_run_beats_heartbeat_once_per_iteration_across_multiple_cycles(make_router, mocker):
    router = make_router()

    def fake_setup_mqtt(self):
        self.mqtt_client = mocker.MagicMock(name="mqtt_client")
        return True

    mocker.patch.object(AlertRouter, 'setup_mqtt', fake_setup_mqtt)
    mocker.patch.object(router, 'publish_service_status')
    mock_beat = mocker.patch('alert_router.heartbeat.beat')

    call_count = {'n': 0}

    def fake_sleep(seconds):
        call_count['n'] += 1
        if call_count['n'] >= 3:
            router.running = False

    mocker.patch('alert_router.time.sleep', side_effect=fake_sleep)

    router.run()

    assert mock_beat.call_count == 3
    mock_beat.assert_called_with(router.heartbeat_path)


def test_run_does_not_beat_heartbeat_if_mqtt_setup_fails(make_router, mocker):
    # If MQTT setup fails, run() returns before entering the status loop at
    # all -- no heartbeat should ever be written for a service that never
    # started successfully.
    router = make_router()
    mocker.patch.object(AlertRouter, 'setup_mqtt', return_value=False)
    mock_beat = mocker.patch('alert_router.heartbeat.beat')

    router.run()

    mock_beat.assert_not_called()


# ---------------------------------------------------------------------------
# publish_service_status(): async_errors merged into pathway_errors
# ---------------------------------------------------------------------------

class _FakePathway:
    """Minimal pathway double exposing just what PathwayManager reads."""

    def __init__(self, async_errors=0, enabled=True):
        self.async_errors = async_errors
        self._enabled = enabled

    def is_enabled(self):
        return self._enabled


def _published_status(router, mocker):
    router.mqtt_client = mocker.MagicMock(name="mqtt_client")
    router.publish_service_status('running')
    payload = router.mqtt_client.publish.call_args.args[1]
    return json.loads(payload)


def test_pathway_errors_includes_async_errors_for_pathway_with_no_sync_errors(make_router, mocker):
    # e.g. EmailPathway's storm-prevention timer-thread flush failed, but
    # process_alert() was never involved -- self.stats['pathway_errors']
    # has no entry for it at all before the merge.
    router = make_router()
    router.pathway_manager.pathways['email_alerts'] = _FakePathway(async_errors=2)

    status = _published_status(router, mocker)

    assert status['stats']['pathway_errors'] == {'email_alerts': 2}


def test_pathway_errors_adds_async_errors_onto_existing_sync_count(make_router, mocker):
    router = make_router()
    router.pathway_manager.pathways['email_alerts'] = _FakePathway(async_errors=3)
    router.stats['pathway_errors']['email_alerts'] = 5  # pre-existing sync failures

    status = _published_status(router, mocker)

    assert status['stats']['pathway_errors']['email_alerts'] == 8


def test_pathway_errors_omits_pathways_with_zero_async_errors(make_router, mocker):
    router = make_router()
    router.pathway_manager.pathways['console_alerts'] = _FakePathway(async_errors=0)

    status = _published_status(router, mocker)

    # Unchanged from historical shape: only pathways with an actual error
    # (sync or async) appear in pathway_errors.
    assert status['stats']['pathway_errors'] == {}


def test_pathway_errors_merge_does_not_double_count_across_publishes(make_router, mocker):
    # pathway.async_errors is itself a cumulative counter -- publishing
    # status twice must not add it in twice.
    router = make_router()
    router.pathway_manager.pathways['email_alerts'] = _FakePathway(async_errors=2)

    first = _published_status(router, mocker)
    second = _published_status(router, mocker)

    assert first['stats']['pathway_errors']['email_alerts'] == 2
    assert second['stats']['pathway_errors']['email_alerts'] == 2


def test_pathway_errors_merge_does_not_mutate_internal_stats(make_router, mocker):
    # self.stats['pathway_errors'] stays the pure sync-error record;
    # the merge happens only in the published payload.
    router = make_router()
    router.pathway_manager.pathways['email_alerts'] = _FakePathway(async_errors=2)

    _published_status(router, mocker)

    assert router.stats['pathway_errors'] == {}
