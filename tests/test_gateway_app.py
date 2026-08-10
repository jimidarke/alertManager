"""
Tests for src/gateway/__init__.py (Task 18): create_app() returns a
working Flask app with GatewayConfig applied and db/mqtt extensions
wired -- no live Postgres/MQTT available in this environment, so both are
mocked at the same seams tests/test_gateway_db.py and
tests/test_gateway_mqtt.py already establish.
"""

import sys
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gateway import create_app  # noqa: E402
from gateway.config import GatewayConfig  # noqa: E402


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "e" * 32)
    monkeypatch.setenv("POSTGRES_HOST", "test-postgres")
    monkeypatch.setenv("MQTT_BROKER", "test-broker")
    return GatewayConfig.from_env()


@pytest.fixture(autouse=True)
def _mock_db_pool(mocker):
    mocker.patch("gateway.db.psycopg2_pool.ThreadedConnectionPool", return_value=mocker.MagicMock())


@pytest.fixture(autouse=True)
def _mock_user_store(mocker):
    mocker.patch("gateway.db.UserStore.from_env", return_value=mocker.MagicMock())


def test_create_app_returns_a_flask_app(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    assert isinstance(app, Flask)


def test_create_app_applies_secret_key_and_session_lifetime(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    assert app.config["SECRET_KEY"] == config.SECRET_KEY
    assert app.config["PERMANENT_SESSION_LIFETIME"] == config.SESSION_LIFETIME_SECONDS


def test_create_app_wires_db_pool_user_store_and_mqtt_onto_extensions(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    assert "db_pool" in app.extensions
    assert "user_store" in app.extensions
    assert "mqtt_publisher" in app.extensions
    assert app.extensions["gateway_config"] is config


def test_create_app_registers_all_five_blueprints(config, mocker):
    # Task 19 registers `auth` (/login, /logout, /api/whoami); Task 20 adds
    # `api` (/health, /alert, /test, /docs, /docs/<f>); Task 21 adds `logs`
    # (/logs, /api/logs/list, /api/logs/tail, /api/logs/chunk); Task 22 adds
    # `dashboard` (/dashboard, /api/dashboard/summary, /api/dashboard/critical);
    # Task 23 adds `admin` (/admin/users*); Task 24 adds `GET /` (redirects to
    # /dashboard or /login depending on session state -- see gateway/__init__.py).
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    user_rules = {r.rule for r in app.url_map.iter_rules() if r.endpoint != "static"}
    assert user_rules == {
        "/",
        "/login",
        "/logout",
        "/change-password",
        "/api/whoami",
        "/health",
        "/alert",
        "/test",
        "/docs",
        "/docs/<path:filename>",
        "/logs",
        "/api/logs/list",
        "/api/logs/tail",
        "/api/logs/chunk",
        "/dashboard",
        "/api/dashboard/summary",
        "/api/dashboard/critical",
        "/admin/users",
        "/admin/users/new",
        "/admin/users/<username>/edit",
        "/admin/users/<username>/disable",
        "/admin/users/<username>/enable",
        "/admin/users/<username>/reset-password",
        "/admin/users/<username>/tokens/new",
        "/admin/users/<username>/tokens/<int:token_id>/revoke",
    }


def test_create_app_wires_credential_cache_and_ip_throttle(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    assert "credential_cache" in app.extensions
    assert "ip_throttle" in app.extensions


def test_create_app_sets_secure_session_cookie_flags(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app(config)

    assert app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert app.config["SESSION_COOKIE_SECURE"] is True
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_create_app_exits_when_mqtt_connect_fails(config, mocker):
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=False)

    with pytest.raises(SystemExit) as exc_info:
        create_app(config)

    assert exc_info.value.code == 1


def test_create_app_defaults_to_config_from_env(monkeypatch, mocker):
    monkeypatch.setenv("GATEWAY_SECRET_KEY", "f" * 32)
    mocker.patch("gateway.mqtt.MqttPublisher.connect_mqtt", return_value=True)

    app = create_app()

    assert app.config["SECRET_KEY"] == "f" * 32
