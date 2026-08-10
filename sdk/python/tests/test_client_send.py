"""
tests/test_client_send.py -- AlertClient.send() and the level-shortcut
methods.
"""

import pytest

from alertmanager_sdk.client import AlertClient, SendResult
from alertmanager_sdk.exceptions import (
    AlertManagerAuthError,
    AlertManagerBadRequestError,
    AlertManagerForbiddenError,
    AlertManagerServerError,
    AlertManagerServiceUnavailableError,
    AlertManagerValidationError,
)


def _client(session):
    return AlertClient("https://example.test", username="u", password="p", session=session)


class TestConstruction:
    def test_basic_auth_accepted(self):
        AlertClient("https://example.test", username="u", password="p")

    def test_token_accepted(self):
        AlertClient("https://example.test", token="amt_abc123")

    def test_neither_raises(self):
        with pytest.raises(ValueError):
            AlertClient("https://example.test")

    def test_both_raises(self):
        with pytest.raises(ValueError):
            AlertClient("https://example.test", username="u", password="p", token="amt_abc123")

    def test_username_without_password_raises(self):
        with pytest.raises(ValueError):
            AlertClient("https://example.test", username="u")

    def test_password_without_username_raises(self):
        with pytest.raises(ValueError):
            AlertClient("https://example.test", password="p")

    def test_base_url_trailing_slash_stripped(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = AlertClient("https://example.test/", username="u", password="p", session=fake_session)

        client.send(level="info", category="system", title="t", message="m", source="s")

        args, _ = fake_session.post.call_args
        assert args[0] == "https://example.test/alert"

    def test_bearer_token_sets_authorization_header_not_basic_auth(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = AlertClient("https://example.test", token="amt_abc123", session=fake_session)

        client.send(level="info", category="system", title="t", message="m", source="s")

        _, kwargs = fake_session.post.call_args
        assert kwargs["auth"] is None
        assert kwargs["headers"]["Authorization"] == "Bearer amt_abc123"


class TestSendSuccess:
    def test_returns_send_result_with_expected_fields(self, fake_session, make_response):
        fake_session.post.return_value = make_response(
            200,
            json_body={
                "status": "success",
                "message": "Alert forwarded to MQTT broker",
                "alert_id": "my-app-1732982400",
            },
        )
        client = _client(fake_session)

        result = client.send(
            level="info", category="application", title="Status", message="OK", source="my-app"
        )

        assert isinstance(result, SendResult)
        assert result.ok is True
        assert result.http_status == 200
        assert result.alert_id == "my-app-1732982400"
        assert result.retries == 0

    def test_alert_id_is_not_asserted_as_meaningful_beyond_a_string(self, fake_session, make_response):
        # alert_id is a cosmetic ack token, not a durable/idempotency id --
        # this test exists to make sure nobody later adds a parsing
        # assumption about its shape.
        fake_session.post.return_value = make_response(200, json_body={"alert_id": "anything-goes-here"})
        client = _client(fake_session)

        result = client.send(level="info", category="system", title="t", message="m", source="s")

        assert isinstance(result.alert_id, str)

    def test_timestamp_auto_filled_when_omitted(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = _client(fake_session)

        client.send(level="info", category="system", title="t", message="m", source="s")

        _, kwargs = fake_session.post.call_args
        assert isinstance(kwargs["json"]["timestamp"], int)

    def test_explicit_timestamp_overrides_auto_fill(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = _client(fake_session)

        client.send(level="info", category="system", title="t", message="m", source="s", timestamp=123456789)

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"]["timestamp"] == 123456789


class TestClientSideValidationSavesTheRoundTrip:
    def test_invalid_level_raises_without_any_http_call(self, fake_session):
        client = _client(fake_session)

        with pytest.raises(AlertManagerValidationError):
            client.send(level="bogus", category="system", title="t", message="m", source="s")

        fake_session.post.assert_not_called()

    def test_missing_source_raises_without_any_http_call(self, fake_session):
        client = _client(fake_session)

        with pytest.raises(AlertManagerValidationError):
            client.send(level="info", category="system", title="t", message="m")

        fake_session.post.assert_not_called()


class TestSendErrorMapping:
    def test_400_raises_bad_request_with_error_key_message(self, fake_session, make_response):
        fake_session.post.return_value = make_response(
            400, json_body={"error": "Missing required fields: title", "required_fields": []}
        )
        client = _client(fake_session)

        with pytest.raises(AlertManagerBadRequestError, match="Missing required fields"):
            client.send(level="info", category="system", title="t", message="m", source="s")

    def test_401_raises_auth_error(self, fake_session, make_response):
        fake_session.post.return_value = make_response(
            401, json_body={"status": "error", "error": "authentication required"}
        )
        client = _client(fake_session)

        with pytest.raises(AlertManagerAuthError):
            client.send(level="info", category="system", title="t", message="m", source="s")

    def test_403_raises_forbidden_with_no_false_claim_of_server_detail(self, fake_session, make_response):
        fake_session.post.return_value = make_response(403, json_body={"status": "error", "error": "forbidden"})
        client = _client(fake_session)

        with pytest.raises(AlertManagerForbiddenError) as exc_info:
            client.send(level="info", category="system", title="t", message="m", source="s", client="acme-co")

        # The message must not claim to know WHY -- the server doesn't say.
        assert "acme-co" not in exc_info.value.message
        assert exc_info.value.attempted_client == "acme-co"

    def test_503_raises_service_unavailable(self, fake_session, make_response):
        fake_session.post.return_value = make_response(
            503, json_body={"status": "error", "error": "Credential verification temporarily unavailable"}
        )
        client = _client(fake_session)

        with pytest.raises(AlertManagerServiceUnavailableError):
            client.send(level="info", category="system", title="t", message="m", source="s")

    def test_500_raises_server_error(self, fake_session, make_response):
        fake_session.post.return_value = make_response(
            500, json_body={"status": "error", "message": "Internal server error", "detail": "..."}
        )
        client = _client(fake_session)

        with pytest.raises(AlertManagerServerError):
            client.send(level="info", category="system", title="t", message="m", source="s")


class TestDefaults:
    def test_default_source_used_when_call_site_omits_it(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = AlertClient("https://example.test", username="u", password="p", session=fake_session, default_source="default-app")

        client.send(level="info", category="system", title="t", message="m")

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"]["source"] == "default-app"

    def test_call_site_source_overrides_default(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = AlertClient("https://example.test", username="u", password="p", session=fake_session, default_source="default-app")

        client.send(level="info", category="system", title="t", message="m", source="explicit-app")

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"]["source"] == "explicit-app"

    def test_default_client_used_when_call_site_omits_it(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = AlertClient(
            "https://example.test", username="u", password="p", session=fake_session, default_client="acme-co"
        )

        client.send(level="info", category="system", title="t", message="m", source="s")

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"]["client"] == "acme-co"

    def test_no_client_sent_when_neither_call_site_nor_default_set(self, fake_session, make_response):
        fake_session.post.return_value = make_response(200, json_body={})
        client = _client(fake_session)

        client.send(level="info", category="system", title="t", message="m", source="s")

        _, kwargs = fake_session.post.call_args
        assert "client" not in kwargs["json"]


class TestLevelShortcuts:
    @pytest.mark.parametrize("method_name,expected_level", [
        ("critical", "critical"),
        ("warning", "warning"),
        ("notice", "notice"),
        ("info", "info"),
        ("debug", "debug"),
    ])
    def test_shortcut_delegates_to_send_with_correct_level(self, fake_session, make_response, method_name, expected_level):
        fake_session.post.return_value = make_response(200, json_body={})
        client = _client(fake_session)

        getattr(client, method_name)(category="system", title="t", message="m", source="s")

        _, kwargs = fake_session.post.call_args
        assert kwargs["json"]["level"] == expected_level


class TestRetryEndToEnd:
    def test_send_retries_transparently_and_reports_retry_count(self, fake_session, make_response):
        fake_session.post.side_effect = [
            make_response(500),
            make_response(200, json_body={"alert_id": "x"}),
        ]
        client = _client(fake_session)

        result = client.send(level="info", category="system", title="t", message="m", source="s")

        assert result.ok is True
        assert result.retries == 1
        assert fake_session.post.call_count == 2
