#!/usr/bin/env python3
"""
URL Monitor Service
Polls a server-configured list of URLs (headers-only GET), tracks per-URL
health as an OK/DEGRADED/DOWN state machine, and publishes alerts to the
alerts/input MQTT topic for routing -- an alert producer, not a pathway,
modeled on src/feed_aggregator.py.
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from utils.mqtt_publisher import MQTTPublisher
from utils.payload_builder import build_alert_payload
from utils import heartbeat
from version import __version__

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

_DEFAULT_SETTINGS = {
    'interval_seconds': 60,
    'timeout_seconds': 5,
    'max_response_ms': 200,
    'expected_status': 200,
    'follow_redirects': False,
    'reminder_interval_seconds': 43200,
}


def load_config(config_path: str) -> dict:
    """Load and validate the url_monitor section of a service config file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, 'r') as f:
        config = json.load(f)

    if 'url_monitor' not in config:
        raise ValueError("Configuration missing 'url_monitor' section")

    um_config = config['url_monitor']
    required_keys = ['enabled', 'mqtt', 'urls']
    for key in required_keys:
        if key not in um_config:
            raise ValueError(f"url_monitor config missing required key: {key}")

    names = []
    for idx, url_cfg in enumerate(um_config['urls']):
        for req in ('name', 'url', 'client'):
            if req not in url_cfg:
                raise ValueError(f"url_monitor.urls[{idx}] missing required key: {req}")
        names.append(url_cfg['name'])

    if len(names) != len(set(names)):
        raise ValueError("url_monitor.urls contains duplicate 'name' values")

    if not um_config['enabled']:
        logger.warning("URL monitor is disabled in configuration")

    logger.info(f"Configuration loaded from {config_path}")
    return config


def resolve_url_settings(url_cfg: dict, defaults: dict) -> dict:
    """Merge global defaults with per-URL overrides into one settings dict."""
    merged = dict(_DEFAULT_SETTINGS)
    merged.update(defaults)
    for key in _DEFAULT_SETTINGS:
        if key in url_cfg:
            merged[key] = url_cfg[key]
    merged['name'] = url_cfg['name']
    merged['url'] = url_cfg['url']
    merged['client'] = url_cfg['client']
    return merged


class CheckResult:
    """Outcome of a single check_url() poll."""

    __slots__ = ('state', 'status_code', 'response_ms', 'failure', 'detail')

    def __init__(self, state, status_code=None, response_ms=None, failure=None, detail=None):
        self.state = state          # 'OK' | 'DEGRADED' | 'DOWN'
        self.status_code = status_code
        self.response_ms = response_ms
        self.failure = failure      # None | 'timeout' | 'dns' | 'refused' | 'tls' | 'connection'
        self.detail = detail


def _classify_connection_error(exc: requests.exceptions.ConnectionError) -> str:
    reason = None
    if exc.args and hasattr(exc.args[0], 'reason'):
        reason = exc.args[0].reason
    reason_name = type(reason).__name__ if reason is not None else ''

    if 'NameResolution' in reason_name:
        return 'dns'
    if 'NewConnectionError' in reason_name or 'refused' in str(exc).lower():
        return 'refused'
    return 'connection'


def check_url(settings: dict) -> CheckResult:
    """Headers-only GET against a URL; body is never read (stream=True + close())."""
    url = settings['url']
    timeout = settings['timeout_seconds']
    headers = {"User-Agent": f"alertManager-url-monitor/{__version__}"}

    try:
        r = requests.get(
            url,
            timeout=timeout,
            allow_redirects=settings['follow_redirects'],
            stream=True,
            headers=headers,
        )
        elapsed_ms = r.elapsed.total_seconds() * 1000
        status = r.status_code
        r.close()
    except requests.exceptions.Timeout:
        return CheckResult('DOWN', failure='timeout', detail=f"Timed out after {timeout}s")
    except requests.exceptions.SSLError as e:
        # Must be caught before ConnectionError: SSLError is a subclass of it,
        # so a swapped order would silently misclassify TLS failures.
        return CheckResult('DOWN', failure='tls', detail=str(e)[:200])
    except requests.exceptions.ConnectionError as e:
        return CheckResult('DOWN', failure=_classify_connection_error(e), detail=str(e)[:200])
    except requests.exceptions.RequestException as e:
        return CheckResult('DOWN', failure='connection', detail=str(e)[:200])

    if status != settings['expected_status']:
        return CheckResult(
            'DEGRADED', status_code=status, response_ms=elapsed_ms,
            detail=f"expected {settings['expected_status']}, got {status}"
        )
    if elapsed_ms > settings['max_response_ms']:
        return CheckResult(
            'DEGRADED', status_code=status, response_ms=elapsed_ms,
            detail=f"{elapsed_ms:.0f}ms > {settings['max_response_ms']}ms threshold"
        )
    return CheckResult('OK', status_code=status, response_ms=elapsed_ms)


class UrlState:
    """In-memory health state for one monitored URL. Lost on restart by design."""

    __slots__ = ('state', 'since', 'last_alert_at')

    def __init__(self):
        self.state = None
        self.since = None
        self.last_alert_at = None


_LEVEL_FOR_STATE = {'DOWN': 'critical', 'DEGRADED': 'warning', 'OK': 'debug'}


def _clean_metadata(metadata: dict) -> dict:
    return {k: v for k, v in metadata.items() if v is not None}


def _build_title(name: str, settings: dict, result: CheckResult, is_recovery: bool, reminder_due: bool) -> str:
    if is_recovery:
        return f"{name} recovered"
    if result.state == 'DOWN':
        return f"{name} STILL DOWN" if reminder_due else f"{name} is DOWN"
    if result.state == 'DEGRADED':
        if reminder_due:
            return f"{name} STILL DEGRADED"
        if result.status_code != settings['expected_status']:
            return f"{name} degraded (status {result.status_code})"
        return f"{name} slow response ({result.response_ms:.0f}ms)"
    return f"{name} OK"


def _build_message(name: str, result: CheckResult, is_recovery: bool, duration_seconds: Optional[int]) -> str:
    if is_recovery:
        if duration_seconds is not None:
            return f"{name} recovered after {duration_seconds}s of downtime"
        return f"{name} recovered"
    if result.state == 'DOWN':
        return f"{name} check failed: {result.detail}"
    if result.state == 'DEGRADED':
        return f"{name} check degraded: {result.detail}"
    return f"{name} responding normally ({result.response_ms:.0f}ms)"


class UrlMonitor:
    """Main URL monitor service with scheduler."""

    def __init__(self, config_path: str):
        self.config = load_config(config_path)
        um_config = self.config['url_monitor']
        self.mqtt_publisher = MQTTPublisher(um_config['mqtt'])
        self.scheduler = BackgroundScheduler()
        self.running = False
        self.states = {}

        defaults = um_config.get('defaults', {})
        self.url_settings = {
            u['name']: resolve_url_settings(u, defaults)
            for u in um_config['urls']
        }

        self.heartbeat_path = os.getenv('HEARTBEAT_FILE', '/data/logs/ops/url-monitor.heartbeat')

        logger.info(f"Initialized URL monitor with {len(self.url_settings)} URL(s)")

    def _build_payload(self, name, settings, result, previous_state, st, is_recovery, reminder_due, now):
        duration_seconds = None
        if (is_recovery or reminder_due) and st.since is not None:
            duration_seconds = int(now - st.since)

        title = _build_title(name, settings, result, is_recovery, reminder_due)
        message = _build_message(name, result, is_recovery, duration_seconds)
        level = 'notice' if is_recovery else _LEVEL_FOR_STATE[result.state]

        metadata = _clean_metadata({
            'url': settings['url'],
            'status_code': result.status_code,
            'response_ms': round(result.response_ms, 1) if result.response_ms is not None else None,
            'threshold_ms': settings['max_response_ms'],
            'expected_status': settings['expected_status'],
            'state': result.state,
            'previous_state': previous_state,
            'failure': result.failure,
            'duration_seconds': duration_seconds,
            'down_since': int(st.since) if reminder_due and st.since is not None else None,
        })

        return build_alert_payload(
            level=level,
            category='application',
            title=title,
            message=message,
            source=f"url-monitor/{name}",
            client=settings['client'],
            metadata=metadata,
        )

    def check_and_alert(self, name: str):
        """Poll one URL, update its state machine, and publish an alert if warranted."""
        settings = self.url_settings[name]
        result = check_url(settings)
        now = time.time()
        st = self.states.setdefault(name, UrlState())
        previous_state = st.state

        transitioned = previous_state is not None and previous_state != result.state
        is_recovery = transitioned and previous_state != 'OK' and result.state == 'OK'
        reminder_due = (
            not transitioned and result.state != 'OK' and st.last_alert_at is not None
            and (now - st.last_alert_at) >= settings['reminder_interval_seconds']
        )

        should_alert = (
            previous_state is None
            or transitioned
            or result.state == 'OK'
            or reminder_due
        )

        if should_alert:
            payload = self._build_payload(
                name, settings, result, previous_state, st, is_recovery, reminder_due, now
            )
            self.mqtt_publisher.publish_alert(payload)
            st.last_alert_at = now

        if transitioned or previous_state is None:
            st.since = now
        st.state = result.state

    def start(self):
        """Start the scheduler and run an initial sweep."""
        logger.info("Starting URL Monitor Service")
        self.running = True

        self.mqtt_publisher.connect()

        logger.info("Running initial URL sweep")
        for name in self.url_settings:
            try:
                self.check_and_alert(name)
            except Exception as e:
                logger.error(f"Initial check failed for {name}: {e}")

        for name, settings in self.url_settings.items():
            self.scheduler.add_job(
                func=self.check_and_alert,
                args=[name],
                trigger=IntervalTrigger(seconds=settings['interval_seconds']),
                id=f'url_{name}',
                name=f'Check {name}',
                replace_existing=True,
                max_instances=1,
                coalesce=True,
            )
            logger.info(f"Scheduled {name} to run every {settings['interval_seconds']}s")

        self.scheduler.start()
        logger.info("Scheduler started successfully")

        try:
            while self.running:
                heartbeat.beat(self.heartbeat_path)
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
            self.stop()

    def stop(self):
        """Stop the scheduler and disconnect MQTT."""
        logger.info("Stopping URL Monitor Service")
        self.running = False

        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
            logger.info("Scheduler stopped")

        self.mqtt_publisher.disconnect()
        logger.info("MQTT disconnected")


_monitor_instance: Optional['UrlMonitor'] = None


def signal_handler(sig, frame):
    """Handle SIGTERM and SIGINT with a graceful shutdown (unlike feed_aggregator's
    bare sys.exit(0), which bypasses MQTT disconnect/scheduler shutdown)."""
    logger.info(f"Received signal {sig}")
    if _monitor_instance is not None:
        _monitor_instance.stop()
    sys.exit(0)


def main():
    global _monitor_instance

    parser = argparse.ArgumentParser(description='URL Monitor Service')
    parser.add_argument('--config', required=True, help='Path to configuration file')
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        monitor = UrlMonitor(args.config)
        _monitor_instance = monitor
        monitor.start()
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
