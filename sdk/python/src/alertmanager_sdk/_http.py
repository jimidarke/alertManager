"""
Alert Manager SDK - low-level HTTP POST with retry/backoff.

Mirrors pathways/email_pathway.py's `_post_sendmail`/`_retry_delay_seconds`
decision matrix exactly -- the one existing retry/backoff implementation in
the alertManager codebase, and the intended house precedent for this SDK to
follow rather than inventing a new policy from generic retry advice.

This deliberately disagrees with generic "retry on connection error" advice
found in most Python HTTP client guidance: a `requests.RequestException`
(timeout, connection refused, DNS failure) is NEVER retried here, because
it's ambiguous whether the server actually processed the (non-idempotent)
POST before the connection failed -- retrying risks a duplicate alert. This
is the same reasoning `_post_sendmail` applies to Graph API sends, carried
over verbatim.

This module returns results, it does not raise -- `client.py` is the layer
that translates an `HttpResult` into the public exception hierarchy. This
mirrors the existing split between `_post_sendmail` (returns `GraphResult`)
and its caller `_send_graph_email` (decides what a failed `GraphResult`
means) -- keeping this module a pure, low-level, independently-testable
POST helper.
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

DEFAULT_TIMEOUT: Tuple[float, float] = (5, 30)  # (connect, read) seconds
MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = [2, 8]  # fixed schedule for the 1st/2nd retry, used when no Retry-After header
RETRY_AFTER_CAP_SECONDS = 30


@dataclass
class HttpResult:
    """
    Outcome of one post_with_retry() call, success or failure -- the
    caller decides what a failure means (see module docstring).
    """

    ok: bool
    http_status: Optional[int]
    response_body: Optional[Dict[str, Any]]
    raw_text: str
    duration_ms: int
    retries: int
    error_text: Optional[str] = None


def _retry_delay_seconds(response: "requests.Response", retry_index: int) -> float:
    """
    Delay before the next retry: the response's Retry-After header (capped
    at RETRY_AFTER_CAP_SECONDS) when present and parseable as an int, else
    RETRY_BACKOFF_SECONDS[retry_index]. Mirrors
    pathways/email_pathway.py's _retry_delay_seconds exactly.
    """
    retry_after = response.headers.get("Retry-After")
    if retry_after is not None:
        try:
            return min(int(retry_after), RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            pass
    return RETRY_BACKOFF_SECONDS[retry_index]


def post_with_retry(
    session: requests.Session,
    url: str,
    *,
    auth: Optional[Tuple[str, str]] = None,
    headers: Optional[Dict[str, str]] = None,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
) -> HttpResult:
    """
    POST to `url`, retrying transient failures per the matrix below.
    Retries only HTTP 429 or 5xx responses, up to MAX_RETRIES times total
    (so at most MAX_RETRIES + 1 POSTs are made). Any 2xx returns
    immediately as success. Any other status (400, 401, 403, other 4xx) is
    treated as permanent and returned immediately without retry -- those
    won't succeed on a second attempt. A `requests.RequestException`
    (connection error, timeout, etc.) is an immediate failed result, never
    retried -- see module docstring for why.
    """
    start = time.monotonic()
    retries = 0

    while True:
        try:
            response = session.post(url, auth=auth, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as e:
            return HttpResult(
                ok=False,
                http_status=None,
                response_body=None,
                raw_text="",
                duration_ms=int((time.monotonic() - start) * 1000),
                retries=retries,
                error_text=str(e),
            )

        if 200 <= response.status_code < 300:
            body = _safe_json(response)
            return HttpResult(
                ok=True,
                http_status=response.status_code,
                response_body=body,
                raw_text=response.text,
                duration_ms=int((time.monotonic() - start) * 1000),
                retries=retries,
            )

        is_retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if not is_retryable or retries >= MAX_RETRIES:
            body = _safe_json(response)
            return HttpResult(
                ok=False,
                http_status=response.status_code,
                response_body=body,
                raw_text=response.text,
                duration_ms=int((time.monotonic() - start) * 1000),
                retries=retries,
            )

        time.sleep(_retry_delay_seconds(response, retries))
        retries += 1


def _safe_json(response: "requests.Response") -> Optional[Dict[str, Any]]:
    """
    Best-effort JSON parse -- a proxy-generated error page or a truncated
    body should degrade to `None`, not raise out of this module. Callers
    (client.py) fall back to a generic message via
    AlertManagerHTTPError.message_from_body() when this is None.
    """
    try:
        return response.json()
    except ValueError:
        return None
