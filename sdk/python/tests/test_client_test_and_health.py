"""
tests/test_client_test_and_health.py -- AlertClient.test() and .health().
"""

import pytest
import requests

from alertmanager_sdk.client import AlertClient, HealthStatus
from alertmanager_sdk.exceptions import (
    AlertManagerAuthError,
    AlertManagerConnectionError,
    AlertManagerForbiddenError,
    AlertManagerServerError,
    AlertManagerServiceUnavailableError,
)


def _client(session):
    return AlertClient("https://example.test", username="u", password="p", session=session)


class TestTest:
    def test_success_returns_full_parsed_body(self, fake_session, make_response):
        body = {"status": "success", "message": "Test alert sent successfully", "alert": {"level": "info"}}
        fake_session.post.return_value = make_response(200, json_body=body)
        client = _client(fake_session)

        result = client.test()

        assert result == body

    def test_no_payload_sent(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = _client(fake_session)

        client.test()

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"] is None

    def test_401_raises_auth_error(self, fake_session, make_response):
        fake_session.post.return_value = make_response(401, json_body={"status": "error", "error": "authentication required"})
        client = _client(fake_session)

        with pytest.raises(AlertManagerAuthError):
            client.test()

    def test_500_raises_server_error(self, fake_session, make_response):
        fake_session.post.return_value = make_response(500, json_body={"status": "error", "message": "Failed to send test alert"})
        client = _client(fake_session)

        with pytest.raises(AlertManagerServerError):
            client.test()

    def test_403_and_503_are_impossible_per_ground_truth_but_mapping_would_still_handle_them(
        self, fake_session, make_response
    ):
        """
        /test has no permission check and no cache-fronted auth path, so
        the gateway itself never returns 403/503 here -- but this proves
        the shared status-mapping code doesn't silently swallow them if it
        ever somehow did.
        """
        fake_session.post.return_value = make_response(403, json_body={"status": "error", "error": "forbidden"})
        client = _client(fake_session)

        with pytest.raises(AlertManagerForbiddenError):
            client.test()

        fake_session.post.return_value = make_response(503, json_body={"status": "error", "error": "unavailable"})
        with pytest.raises(AlertManagerServiceUnavailableError):
            client.test()


class TestHealth:
    def test_healthy_response(self, fake_session, make_response):
        fake_session.get.return_value = make_response(
            200,
            json_body={
                "status": "healthy",
                "service": "http-gateway",
                "version": "2.0.2",
                "mqtt_connected": True,
                "mqtt": {"connected": True},
                "db": {"status": "ok"},
            },
        )
        client = _client(fake_session)

        health = client.health()

        assert isinstance(health, HealthStatus)
        assert health.healthy is True
        assert health.mqtt_connected is True
        assert health.db_ok is True
        assert health.db_detail is None
        assert health.version == "2.0.2"

    def test_unhealthy_503_does_not_raise(self, fake_session, make_response):
        """
        503 from /health is a meaningful result (MQTT down), not an error
        condition -- health() must return normally here, unlike send()/
        test() where a non-2xx raises.
        """
        fake_session.get.return_value = make_response(
            503,
            json_body={
                "status": "unhealthy",
                "mqtt_connected": False,
                "mqtt": {"connected": False},
                "db": {"status": "ok"},
            },
        )
        client = _client(fake_session)

        health = client.health()

        assert health.healthy is False
        assert health.mqtt_connected is False

    def test_db_fail_open_reported_separately_from_hard_unhealthy(self, fake_session, make_response):
        """
        DB status is fail-open server-side and never affects the top-level
        status/HTTP code -- this is a real, subtle asymmetry worth its own
        regression test.
        """
        fake_session.get.return_value = make_response(
            200,
            json_body={
                "status": "healthy",
                "mqtt_connected": True,
                "mqtt": {"connected": True},
                "db": {"status": "error", "detail": "connection refused"},
            },
        )
        client = _client(fake_session)

        health = client.health()

        assert health.healthy is True
        assert health.db_ok is False
        assert health.db_detail == "connection refused"

    def test_connection_failure_raises(self, fake_session):
        fake_session.get.side_effect = requests.ConnectionError("connection refused")
        client = _client(fake_session)

        with pytest.raises(AlertManagerConnectionError):
            client.health()

    def test_health_is_unauthenticated(self, fake_session, make_response):
        fake_session.get.return_value = make_response(200, json_body={"status": "healthy", "mqtt_connected": True})
        client = _client(fake_session)

        client.health()

        _, kwargs = fake_session.get.call_args
        assert "auth" not in kwargs or kwargs.get("auth") is None
