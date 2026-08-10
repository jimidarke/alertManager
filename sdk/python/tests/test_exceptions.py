"""
tests/test_exceptions.py -- exception hierarchy behavior: the
status-code -> exception-class mapping (exercised through
AlertClient._raise_for_status, since that's the one place the mapping is
defined), attribute population, and the ValueError-compatibility of
AlertManagerValidationError.
"""

import pytest

from alertmanager_sdk._http import HttpResult
from alertmanager_sdk.client import AlertClient
from alertmanager_sdk.exceptions import (
    AlertManagerAuthError,
    AlertManagerBadRequestError,
    AlertManagerError,
    AlertManagerForbiddenError,
    AlertManagerHTTPError,
    AlertManagerServerError,
    AlertManagerServiceUnavailableError,
    AlertManagerValidationError,
)


def _client():
    return AlertClient("https://example.test", username="u", password="p")


def _result(status_code, body=None):
    return HttpResult(ok=False, http_status=status_code, response_body=body, raw_text="", duration_ms=1, retries=0)


@pytest.mark.parametrize(
    "status_code,expected_class",
    [
        (400, AlertManagerBadRequestError),
        (401, AlertManagerAuthError),
        (403, AlertManagerForbiddenError),
        (503, AlertManagerServiceUnavailableError),
        (500, AlertManagerServerError),
        (502, AlertManagerServerError),
        (418, AlertManagerHTTPError),  # unmapped status -> generic HTTP error, not silently swallowed
    ],
)
def test_status_code_maps_to_expected_exception_class(status_code, expected_class):
    client = _client()
    with pytest.raises(expected_class):
        client._raise_for_status(_result(status_code))


def test_validation_error_is_also_a_value_error():
    assert issubclass(AlertManagerValidationError, ValueError)
    assert issubclass(AlertManagerValidationError, AlertManagerError)

    with pytest.raises(ValueError):
        raise AlertManagerValidationError("bad input")


def test_http_error_carries_status_body_and_retries():
    result = HttpResult(
        ok=False,
        http_status=500,
        response_body={"status": "error", "message": "Internal server error"},
        raw_text='{"status": "error", "message": "Internal server error"}',
        duration_ms=42,
        retries=2,
    )
    client = _client()

    with pytest.raises(AlertManagerServerError) as exc_info:
        client._raise_for_status(result)

    err = exc_info.value
    assert err.status_code == 500
    assert err.response_body == {"status": "error", "message": "Internal server error"}
    assert err.retries == 2
    assert "Internal server error" in err.message


def test_forbidden_error_message_reflects_only_what_sdk_sent_not_server_detail():
    """
    The 403 body carries zero server-side detail about which client/
    permission was missing -- this test locks down that the exception's
    attempted_client comes from the SDK's own request, never parsed out
    of the (empty) response body.
    """
    result = _result(403, body={"status": "error", "error": "forbidden"})
    client = _client()

    with pytest.raises(AlertManagerForbiddenError) as exc_info:
        client._raise_for_status(result, attempted_client="acme-co")

    assert exc_info.value.attempted_client == "acme-co"
    assert exc_info.value.message == "forbidden"


def test_message_from_body_handles_400_shape():
    body = {"error": "Missing required fields: title, message", "required_fields": []}
    assert AlertManagerHTTPError.message_from_body(body, "fallback") == "Missing required fields: title, message"


def test_message_from_body_handles_500_shape():
    body = {"status": "error", "message": "Internal server error"}
    assert AlertManagerHTTPError.message_from_body(body, "fallback") == "Internal server error"


def test_message_from_body_falls_back_when_body_is_none():
    assert AlertManagerHTTPError.message_from_body(None, "fallback") == "fallback"


def test_message_from_body_falls_back_when_body_has_neither_key():
    assert AlertManagerHTTPError.message_from_body({"unexpected": "shape"}, "fallback") == "fallback"
