"""
Tests for src/url_monitor.py: URL uptime/latency monitor service.

No live network/MQTT/scheduler is exercised -- requests.get, MQTTPublisher,
and the BackgroundScheduler's add_job/start/shutdown are all mocked, mirroring
tests/test_analytics_engine.py's approach for the sibling producer service.
"""

import json
import sys
from pathlib import Path

import pytest
import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

from url_monitor import (  # noqa: E402
    UrlMonitor,
    check_url,
    resolve_url_settings,
    load_config,
    signal_handler,
    _DEFAULT_SETTINGS,
)
from version import __version__  # noqa: E402
import url_monitor  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data))
    return str(path)


def _settings(**overrides):
    base = {
        'name': 'site-a',
        'url': 'https://a.example.com/',
        'client': 'acme-co',
        'timeout_seconds': 5,
        'max_response_ms': 200,
        'expected_status': 200,
        'follow_redirects': False,
        'reminder_interval_seconds': 43200,
    }
    base.update(overrides)
    return base


class _FakeMaxRetryError:
    """Stand-in for urllib3.exceptions.MaxRetryError: exposes `.reason`."""

    def __init__(self, reason):
        self.reason = reason

    def __str__(self):
        return str(self.reason)


def _connection_error(reason_class_name, message):
    reason_cls = type(reason_class_name, (Exception,), {})
    reason = reason_cls(message)
    return requests.exceptions.ConnectionError(_FakeMaxRetryError(reason))


def _ok(response_ms=50.0, status_code=200):
    return url_monitor.CheckResult('OK', status_code=status_code, response_ms=response_ms)


def _degraded(response_ms=300.0, status_code=200, detail='slow'):
    return url_monitor.CheckResult('DEGRADED', status_code=status_code, response_ms=response_ms, detail=detail)


def _down(failure='timeout', detail='Timed out after 5s'):
    return url_monitor.CheckResult('DOWN', failure=failure, detail=detail)


@pytest.fixture
def make_monitor(mocker, tmp_path):
    def _make(url_monitor_config=None):
        config = {"url_monitor": url_monitor_config if url_monitor_config is not None else {
            "enabled": True,
            "mqtt": {"broker": "localhost", "port": 1883, "topic": "alerts/input"},
            "defaults": dict(_DEFAULT_SETTINGS),
            "urls": [{"name": "site-a", "url": "https://a.example.com/", "client": "acme-co"}],
        }}
        config_path = _write_config(tmp_path, config)
        mocker.patch('url_monitor.MQTTPublisher')
        return UrlMonitor(config_path)
    return _make


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def test_load_config_missing_url_monitor_key_raises(tmp_path):
    path = _write_config(tmp_path, {})
    with pytest.raises(ValueError, match="url_monitor"):
        load_config(path)


def test_load_config_missing_required_subkey_raises(tmp_path):
    path = _write_config(tmp_path, {"url_monitor": {"enabled": True, "urls": []}})
    with pytest.raises(ValueError, match="mqtt"):
        load_config(path)


def test_load_config_missing_url_entry_field_raises(tmp_path):
    path = _write_config(tmp_path, {"url_monitor": {
        "enabled": True,
        "mqtt": {"broker": "x", "port": 1, "topic": "t"},
        "urls": [{"name": "a", "url": "http://a"}],  # missing client
    }})
    with pytest.raises(ValueError, match="client"):
        load_config(path)


def test_load_config_duplicate_names_raises(tmp_path):
    path = _write_config(tmp_path, {"url_monitor": {
        "enabled": True,
        "mqtt": {"broker": "x", "port": 1, "topic": "t"},
        "urls": [
            {"name": "a", "url": "http://a", "client": "c"},
            {"name": "a", "url": "http://b", "client": "c"},
        ],
    }})
    with pytest.raises(ValueError, match="duplicate"):
        load_config(path)


def test_load_config_valid_returns_dict(tmp_path):
    data = {"url_monitor": {
        "enabled": True,
        "mqtt": {"broker": "x", "port": 1, "topic": "t"},
        "urls": [{"name": "a", "url": "http://a", "client": "c"}],
    }}
    path = _write_config(tmp_path, data)
    assert load_config(path) == data


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "nope.json"))


# ---------------------------------------------------------------------------
# resolve_url_settings
# ---------------------------------------------------------------------------

def test_resolve_url_settings_falls_back_to_defaults():
    settings = resolve_url_settings({'name': 'x', 'url': 'http://x', 'client': 'c'}, {})
    for key, value in _DEFAULT_SETTINGS.items():
        assert settings[key] == value
    assert settings['name'] == 'x'
    assert settings['client'] == 'c'


def test_resolve_url_settings_per_url_override_wins():
    settings = resolve_url_settings(
        {'name': 'x', 'url': 'http://x', 'client': 'c', 'max_response_ms': 500},
        {'max_response_ms': 200},
    )
    assert settings['max_response_ms'] == 500


def test_resolve_url_settings_missing_defaults_block():
    settings = resolve_url_settings({'name': 'x', 'url': 'http://x', 'client': 'c'}, {})
    assert settings['interval_seconds'] == _DEFAULT_SETTINGS['interval_seconds']


# ---------------------------------------------------------------------------
# check_url classification
# ---------------------------------------------------------------------------

def test_check_url_fast_200_is_ok(mocker):
    resp = mocker.Mock(status_code=200)
    resp.elapsed.total_seconds.return_value = 0.05
    mock_get = mocker.patch('url_monitor.requests.get', return_value=resp)

    result = check_url(_settings())

    assert result.state == 'OK'
    assert result.status_code == 200
    resp.close.assert_called_once()
    _, kwargs = mock_get.call_args
    assert kwargs['stream'] is True
    assert kwargs['allow_redirects'] is False
    assert kwargs['headers']['User-Agent'] == f"alertManager-url-monitor/{__version__}"


def test_check_url_slow_200_is_degraded(mocker):
    resp = mocker.Mock(status_code=200)
    resp.elapsed.total_seconds.return_value = 0.3
    mocker.patch('url_monitor.requests.get', return_value=resp)

    result = check_url(_settings(max_response_ms=200))

    assert result.state == 'DEGRADED'
    assert result.response_ms == pytest.approx(300, abs=1)


def test_check_url_wrong_status_is_degraded(mocker):
    resp = mocker.Mock(status_code=404)
    resp.elapsed.total_seconds.return_value = 0.01
    mocker.patch('url_monitor.requests.get', return_value=resp)

    result = check_url(_settings(expected_status=200))

    assert result.state == 'DEGRADED'
    assert result.status_code == 404


def test_check_url_unfollowed_redirect_without_matching_expected_is_degraded(mocker):
    resp = mocker.Mock(status_code=301)
    resp.elapsed.total_seconds.return_value = 0.01
    mocker.patch('url_monitor.requests.get', return_value=resp)

    result = check_url(_settings(expected_status=200, follow_redirects=False))

    assert result.state == 'DEGRADED'
    assert result.status_code == 301


def test_check_url_redirect_as_expected_status_is_ok(mocker):
    resp = mocker.Mock(status_code=301)
    resp.elapsed.total_seconds.return_value = 0.01
    mocker.patch('url_monitor.requests.get', return_value=resp)

    result = check_url(_settings(expected_status=301, follow_redirects=False))

    assert result.state == 'OK'


def test_check_url_timeout_is_down(mocker):
    mocker.patch('url_monitor.requests.get', side_effect=requests.exceptions.Timeout())

    result = check_url(_settings(timeout_seconds=5))

    assert result.state == 'DOWN'
    assert result.failure == 'timeout'


def test_check_url_ssl_error_is_down_tls_not_connection(mocker):
    # Regression: SSLError subclasses ConnectionError -- must classify as
    # 'tls', not fall through to the generic 'connection' bucket.
    mocker.patch('url_monitor.requests.get', side_effect=requests.exceptions.SSLError("cert verify failed"))

    result = check_url(_settings())

    assert result.state == 'DOWN'
    assert result.failure == 'tls'


def test_check_url_dns_failure_is_down(mocker):
    exc = _connection_error(
        'NameResolutionError',
        "Failed to resolve 'bad.example.com' ([Errno -2] Name or service not known)",
    )
    mocker.patch('url_monitor.requests.get', side_effect=exc)

    result = check_url(_settings())

    assert result.state == 'DOWN'
    assert result.failure == 'dns'


def test_check_url_connection_refused_is_down(mocker):
    exc = _connection_error(
        'NewConnectionError',
        "Failed to establish a new connection: [Errno 111] Connection refused",
    )
    mocker.patch('url_monitor.requests.get', side_effect=exc)

    result = check_url(_settings())

    assert result.state == 'DOWN'
    assert result.failure == 'refused'


def test_check_url_generic_connection_error_is_down_connection(mocker):
    mocker.patch(
        'url_monitor.requests.get',
        side_effect=requests.exceptions.ConnectionError("something else broke"),
    )

    result = check_url(_settings())

    assert result.state == 'DOWN'
    assert result.failure == 'connection'


# ---------------------------------------------------------------------------
# state machine (check_and_alert)
# ---------------------------------------------------------------------------

def test_first_seen_ok_publishes_debug(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())

    monitor.check_and_alert('site-a')

    monitor.mqtt_publisher.publish_alert.assert_called_once()
    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'debug'
    assert monitor.states['site-a'].state == 'OK'


def test_first_seen_down_publishes_critical(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_down())

    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'critical'
    assert 'is DOWN' in payload['title']


def test_ok_to_degraded_transition_is_warning(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())
    monitor.check_and_alert('site-a')

    mocker.patch('url_monitor.check_url', return_value=_degraded())
    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'warning'
    assert payload['metadata']['previous_state'] == 'OK'


def test_ok_to_down_is_critical(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())
    monitor.check_and_alert('site-a')

    mocker.patch('url_monitor.check_url', return_value=_down())
    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'critical'


def test_degraded_to_down_is_critical(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_degraded())
    monitor.check_and_alert('site-a')

    mocker.patch('url_monitor.check_url', return_value=_down())
    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'critical'


def test_down_to_degraded_is_warning(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_down())
    monitor.check_and_alert('site-a')

    mocker.patch('url_monitor.check_url', return_value=_degraded())
    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'warning'


def test_recovery_from_down_is_notice_with_duration(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_down())
    monitor.check_and_alert('site-a')

    mocker.patch('url_monitor.time.time', return_value=1300.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())
    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['level'] == 'notice'
    assert 'recovered' in payload['title']
    assert payload['metadata']['duration_seconds'] == 300


def test_unchanged_ok_publishes_debug_every_poll(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())

    monitor.check_and_alert('site-a')
    monitor.check_and_alert('site-a')

    assert monitor.mqtt_publisher.publish_alert.call_count == 2


def test_unchanged_unhealthy_suppressed_within_reminder_window(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.check_url', return_value=_down())

    mocker.patch('url_monitor.time.time', return_value=1000.0)
    monitor.check_and_alert('site-a')  # first-seen -> alerts

    mocker.patch('url_monitor.time.time', return_value=1000.0 + 100)
    monitor.check_and_alert('site-a')  # inside 12h window -> suppressed

    assert monitor.mqtt_publisher.publish_alert.call_count == 1


def test_unchanged_unhealthy_reminder_fires_after_interval(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.check_url', return_value=_down())

    mocker.patch('url_monitor.time.time', return_value=1000.0)
    monitor.check_and_alert('site-a')  # first-seen

    reminder_seconds = _DEFAULT_SETTINGS['reminder_interval_seconds']
    mocker.patch('url_monitor.time.time', return_value=1000.0 + reminder_seconds)
    monitor.check_and_alert('site-a')

    assert monitor.mqtt_publisher.publish_alert.call_count == 2
    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert 'STILL DOWN' in payload['title']
    assert payload['metadata']['down_since'] == 1000
    assert payload['metadata']['duration_seconds'] == reminder_seconds


# ---------------------------------------------------------------------------
# payload shape
# ---------------------------------------------------------------------------

def test_payload_shape_basic_fields(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_ok())

    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['category'] == 'application'
    assert payload['client'] == 'acme-co'
    assert payload['source'] == 'url-monitor/site-a'
    # OK state carries no failure and isn't a recovery/reminder -> both stripped
    assert 'failure' not in payload['metadata']
    assert 'duration_seconds' not in payload['metadata']


def test_payload_metadata_includes_failure_for_down(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch('url_monitor.time.time', return_value=1000.0)
    mocker.patch('url_monitor.check_url', return_value=_down(failure='dns'))

    monitor.check_and_alert('site-a')

    payload = monitor.mqtt_publisher.publish_alert.call_args.args[0]
    assert payload['metadata']['failure'] == 'dns'
    assert payload['metadata']['state'] == 'DOWN'
    # first-seen -> previous_state is None -> stripped by _clean_metadata
    assert 'previous_state' not in payload['metadata']


# ---------------------------------------------------------------------------
# scheduler / lifecycle
# ---------------------------------------------------------------------------

def test_start_connects_mqtt_runs_sweep_and_schedules(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch.object(monitor, 'check_and_alert')
    mocker.patch.object(monitor.scheduler, 'add_job')
    mocker.patch.object(monitor.scheduler, 'start')
    mocker.patch('url_monitor.heartbeat.beat')

    def fake_sleep(seconds):
        monitor.running = False

    mocker.patch('url_monitor.time.sleep', side_effect=fake_sleep)

    monitor.start()

    monitor.mqtt_publisher.connect.assert_called_once()
    monitor.check_and_alert.assert_called_once_with('site-a')

    monitor.scheduler.add_job.assert_called_once()
    _, kwargs = monitor.scheduler.add_job.call_args
    assert kwargs['id'] == 'url_site-a'
    assert kwargs['replace_existing'] is True
    assert kwargs['max_instances'] == 1
    assert kwargs['coalesce'] is True

    monitor.scheduler.start.assert_called_once()


def test_initial_sweep_continues_after_one_url_raises(mocker, tmp_path):
    config = {"url_monitor": {
        "enabled": True,
        "mqtt": {"broker": "localhost", "port": 1883, "topic": "alerts/input"},
        "defaults": dict(_DEFAULT_SETTINGS),
        "urls": [
            {"name": "site-a", "url": "https://a.example.com/", "client": "acme-co"},
            {"name": "site-b", "url": "https://b.example.com/", "client": "acme-co"},
        ],
    }}
    config_path = _write_config(tmp_path, config)
    mocker.patch('url_monitor.MQTTPublisher')
    monitor = UrlMonitor(config_path)

    calls = []

    def fake_check(name):
        calls.append(name)
        if name == 'site-a':
            raise RuntimeError("boom")

    mocker.patch.object(monitor, 'check_and_alert', side_effect=fake_check)
    mocker.patch.object(monitor.scheduler, 'add_job')
    mocker.patch.object(monitor.scheduler, 'start')
    mocker.patch('url_monitor.heartbeat.beat')

    def fake_sleep(seconds):
        monitor.running = False

    mocker.patch('url_monitor.time.sleep', side_effect=fake_sleep)

    monitor.start()

    assert sorted(calls) == ['site-a', 'site-b']


def test_heartbeat_called_once_per_main_loop_tick(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch.object(monitor, 'check_and_alert')
    mocker.patch.object(monitor.scheduler, 'add_job')
    mocker.patch.object(monitor.scheduler, 'start')
    mock_beat = mocker.patch('url_monitor.heartbeat.beat')

    call_count = {'n': 0}

    def fake_sleep(seconds):
        call_count['n'] += 1
        if call_count['n'] >= 3:
            monitor.running = False

    mocker.patch('url_monitor.time.sleep', side_effect=fake_sleep)

    monitor.start()

    assert mock_beat.call_count == 3
    mock_beat.assert_called_with(monitor.heartbeat_path)


def test_stop_shuts_down_running_scheduler_and_disconnects_mqtt(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch.object(monitor.scheduler, 'shutdown')
    mocker.patch.object(type(monitor.scheduler), 'running', new_callable=mocker.PropertyMock, return_value=True)

    monitor.stop()

    monitor.scheduler.shutdown.assert_called_once_with(wait=False)
    monitor.mqtt_publisher.disconnect.assert_called_once()
    assert monitor.running is False


def test_stop_skips_shutdown_when_scheduler_not_running(make_monitor, mocker):
    monitor = make_monitor()
    mocker.patch.object(monitor.scheduler, 'shutdown')
    mocker.patch.object(type(monitor.scheduler), 'running', new_callable=mocker.PropertyMock, return_value=False)

    monitor.stop()

    monitor.scheduler.shutdown.assert_not_called()
    monitor.mqtt_publisher.disconnect.assert_called_once()


# ---------------------------------------------------------------------------
# HEARTBEAT_FILE env convention
# ---------------------------------------------------------------------------

def test_heartbeat_path_defaults_when_env_unset(make_monitor, monkeypatch):
    monkeypatch.delenv('HEARTBEAT_FILE', raising=False)
    monitor = make_monitor()
    assert monitor.heartbeat_path == '/data/logs/ops/url-monitor.heartbeat'


def test_heartbeat_path_reads_from_env(make_monitor, monkeypatch):
    monkeypatch.setenv('HEARTBEAT_FILE', '/data/logs/ops/custom-url-monitor.heartbeat')
    monitor = make_monitor()
    assert monitor.heartbeat_path == '/data/logs/ops/custom-url-monitor.heartbeat'


# ---------------------------------------------------------------------------
# signal handler
# ---------------------------------------------------------------------------

def test_signal_handler_stops_running_instance(mocker):
    instance = mocker.Mock()
    mocker.patch.object(url_monitor, '_monitor_instance', instance)
    mock_exit = mocker.patch('url_monitor.sys.exit')

    signal_handler(15, None)

    instance.stop.assert_called_once()
    mock_exit.assert_called_once_with(0)


def test_signal_handler_no_instance_does_not_crash(mocker):
    mocker.patch.object(url_monitor, '_monitor_instance', None)
    mock_exit = mocker.patch('url_monitor.sys.exit')

    signal_handler(2, None)

    mock_exit.assert_called_once_with(0)
