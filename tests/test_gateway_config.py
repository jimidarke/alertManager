"""
Tests for src/gateway/config.py (Task 18): GatewayConfig.from_env()'s
fail-fast GATEWAY_SECRET_KEY behavior and its env-var-driven defaults.

Pure-Python / pure-`os.environ` tests -- no filesystem, no network, no
live services needed.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway.config import (  # noqa: E402
    DEFAULT_LOG_POLL_INTERVAL_MS,
    DEFAULT_POSTGRES_MAX_OVERFLOW,
    DEFAULT_POSTGRES_POOL_SIZE,
    DEFAULT_SESSION_LIFETIME_SECONDS,
    MIN_SECRET_KEY_LENGTH,
    GatewayConfig,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every gateway-related env var starts unset for each test, so defaults are exercised deterministically."""
    for name in (
        "GATEWAY_SECRET_KEY",
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_POOL_SIZE",
        "POSTGRES_MAX_OVERFLOW",
        "MQTT_BROKER",
        "MQTT_PORT",
        "MQTT_INPUT_TOPIC",
        "LOG_POLL_INTERVAL_MS",
        "GATEWAY_SESSION_LIFETIME_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# Fail-fast SECRET_KEY behavior
# ---------------------------------------------------------------------------

def test_from_env_exits_when_secret_key_unset(capsys):
    with pytest.raises(SystemExit) as exc_info:
        GatewayConfig.from_env()

    assert exc_info.value.code == 1
    assert "GATEWAY_SECRET_KEY" in capsys.readouterr().err


def test_from_env_exits_when_secret_key_too_short(monkeypatch, capsys):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "x" * (MIN_SECRET_KEY_LENGTH - 1))

    with pytest.raises(SystemExit) as exc_info:
        GatewayConfig.from_env()

    assert exc_info.value.code == 1
    assert "too short" in capsys.readouterr().err


def test_from_env_succeeds_at_exactly_the_minimum_length(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "x" * MIN_SECRET_KEY_LENGTH)

    config = GatewayConfig.from_env()

    assert config.SECRET_KEY == "x" * MIN_SECRET_KEY_LENGTH


def test_from_env_succeeds_with_a_long_secret_key(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "y" * 64)

    config = GatewayConfig.from_env()

    assert config.SECRET_KEY == "y" * 64


# ---------------------------------------------------------------------------
# Defaults, once SECRET_KEY is valid
# ---------------------------------------------------------------------------

def test_from_env_applies_documented_defaults(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "z" * 32)

    config = GatewayConfig.from_env()

    assert config.POSTGRES_HOST == "localhost"
    assert config.POSTGRES_PORT == 5432
    assert config.POSTGRES_DB == "alertmanager"
    assert config.POSTGRES_USER == "alertmanager"
    assert config.POSTGRES_PASSWORD == ""
    assert config.POSTGRES_POOL_MIN == DEFAULT_POSTGRES_POOL_SIZE
    assert config.POSTGRES_POOL_MAX_OVERFLOW == DEFAULT_POSTGRES_MAX_OVERFLOW
    assert config.MQTT_BROKER == "mqtt-broker"
    assert config.MQTT_PORT == 1883
    assert config.MQTT_INPUT_TOPIC == "alerts/input"
    assert config.LOG_POLL_INTERVAL_MS == DEFAULT_LOG_POLL_INTERVAL_MS
    assert config.SESSION_LIFETIME_SECONDS == DEFAULT_SESSION_LIFETIME_SECONDS
    assert config.SESSION_LIFETIME_SECONDS == 12 * 60 * 60


def test_from_env_reads_every_field_from_its_env_var(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "a" * 40)
    monkeypatch.setenv("POSTGRES_HOST", "test-postgres")
    monkeypatch.setenv("POSTGRES_PORT", "5433")
    monkeypatch.setenv("POSTGRES_DB", "testdb")
    monkeypatch.setenv("POSTGRES_USER", "testuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "hunter2")
    monkeypatch.setenv("POSTGRES_POOL_SIZE", "3")
    monkeypatch.setenv("POSTGRES_MAX_OVERFLOW", "7")
    monkeypatch.setenv("MQTT_BROKER", "test-broker")
    monkeypatch.setenv("MQTT_PORT", "1884")
    monkeypatch.setenv("MQTT_INPUT_TOPIC", "alerts/test-input")
    monkeypatch.setenv("LOG_POLL_INTERVAL_MS", "5000")
    monkeypatch.setenv("GATEWAY_SESSION_LIFETIME_SECONDS", "3600")

    config = GatewayConfig.from_env()

    assert config.POSTGRES_HOST == "test-postgres"
    assert config.POSTGRES_PORT == 5433
    assert config.POSTGRES_DB == "testdb"
    assert config.POSTGRES_USER == "testuser"
    assert config.POSTGRES_PASSWORD == "hunter2"
    assert config.POSTGRES_POOL_MIN == 3
    assert config.POSTGRES_POOL_MAX_OVERFLOW == 7
    assert config.MQTT_BROKER == "test-broker"
    assert config.MQTT_PORT == 1884
    assert config.MQTT_INPUT_TOPIC == "alerts/test-input"
    assert config.LOG_POLL_INTERVAL_MS == 5000
    assert config.SESSION_LIFETIME_SECONDS == 3600


def test_gateway_config_is_immutable(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "b" * 32)
    config = GatewayConfig.from_env()

    with pytest.raises(Exception):
        config.SECRET_KEY = "different"
