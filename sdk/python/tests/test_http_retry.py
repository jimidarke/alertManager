"""
tests/test_http_retry.py -- post_with_retry()'s decision matrix.

Mirrors tests/test_email_pathway.py's own structure for `_post_sendmail`
(the precedent this module's retry logic is copied from): flat
`def test_...` functions, grouped by comment-banner sections rather than
classes, one behavior per test.
"""

import requests

from alertmanager_sdk._http import post_with_retry


# ---------------------------------------------------------------------------
# 2xx success -- no retry
# ---------------------------------------------------------------------------


def test_200_returns_success_immediately(fake_session, make_response):
    fake_session.post.return_value = make_response(200, json_body={"status": "success"})

    result = post_with_retry(fake_session, "https://example.test/alert", json_body={"a": 1})

    assert result.ok is True
    assert result.http_status == 200
    assert result.response_body == {"status": "success"}
    assert result.retries == 0
    fake_session.post.assert_called_once()


def test_timeout_tuple_passed_through(fake_session, make_response):
    fake_session.post.return_value = make_response(200, json_body={})

    post_with_retry(fake_session, "https://example.test/alert", timeout=(5, 30))

    _, kwargs = fake_session.post.call_args
    assert kwargs["timeout"] == (5, 30)


# ---------------------------------------------------------------------------
# 429 / 5xx -- retryable
# ---------------------------------------------------------------------------


def test_429_retries_then_succeeds(fake_session, make_response):
    fake_session.post.side_effect = [
        make_response(429, headers={}),
        make_response(200, json_body={"status": "success"}),
    ]

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is True
    assert result.retries == 1
    assert fake_session.post.call_count == 2


def test_500_retries_with_fixed_backoff_schedule(fake_session, make_response, mocker):
    sleep = mocker.patch("alertmanager_sdk._http.time.sleep")
    fake_session.post.side_effect = [
        make_response(500),
        make_response(502),
        make_response(200, json_body={}),
    ]

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is True
    assert result.retries == 2
    sleep.assert_has_calls([mocker.call(2), mocker.call(8)])


def test_retry_after_header_honored_and_capped(fake_session, make_response, mocker):
    sleep = mocker.patch("alertmanager_sdk._http.time.sleep")
    fake_session.post.side_effect = [
        make_response(429, headers={"Retry-After": "999"}),  # far above the 30s cap
        make_response(200, json_body={}),
    ]

    post_with_retry(fake_session, "https://example.test/alert")

    sleep.assert_called_once_with(30)


def test_malformed_retry_after_falls_back_to_fixed_schedule(fake_session, make_response, mocker):
    sleep = mocker.patch("alertmanager_sdk._http.time.sleep")
    fake_session.post.side_effect = [
        make_response(429, headers={"Retry-After": "not-a-number"}),
        make_response(200, json_body={}),
    ]

    post_with_retry(fake_session, "https://example.test/alert")

    sleep.assert_called_once_with(2)


def test_retries_capped_at_two(fake_session, make_response):
    fake_session.post.side_effect = [
        make_response(500),
        make_response(500),
        make_response(500),
    ]

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status == 500
    assert result.retries == 2
    assert fake_session.post.call_count == 3


# ---------------------------------------------------------------------------
# other 4xx -- never retried
# ---------------------------------------------------------------------------


def test_400_never_retries(fake_session, make_response):
    fake_session.post.return_value = make_response(400, json_body={"error": "bad request"})

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status == 400
    assert result.retries == 0
    fake_session.post.assert_called_once()


def test_401_never_retries(fake_session, make_response):
    fake_session.post.return_value = make_response(401, json_body={"status": "error", "error": "authentication required"})

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status == 401
    fake_session.post.assert_called_once()


def test_403_never_retries(fake_session, make_response):
    fake_session.post.return_value = make_response(403, json_body={"status": "error", "error": "forbidden"})

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status == 403
    fake_session.post.assert_called_once()


# ---------------------------------------------------------------------------
# requests.RequestException -- never retried (ambiguous outcome)
# ---------------------------------------------------------------------------


def test_connection_error_never_retried(fake_session):
    fake_session.post.side_effect = requests.ConnectionError("connection refused")

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status is None
    assert result.error_text == "connection refused"
    fake_session.post.assert_called_once()


def test_timeout_exception_never_retried(fake_session):
    fake_session.post.side_effect = requests.Timeout("timed out")

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.http_status is None
    fake_session.post.assert_called_once()


# ---------------------------------------------------------------------------
# response body parsing
# ---------------------------------------------------------------------------


def test_non_json_body_degrades_to_none_not_a_raise(fake_session, make_response):
    fake_session.post.return_value = make_response(500, text="<html>proxy error</html>")

    result = post_with_retry(fake_session, "https://example.test/alert")

    assert result.ok is False
    assert result.response_body is None
    assert result.raw_text == "<html>proxy error</html>"
