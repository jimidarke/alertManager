"""
Alert Manager SDK - AlertClient.

This is a natural evolution of docs/CLIENTNODE.md's own documented
integration shape (`requests.post(url, auth=(user,pass), json={...})`),
not a reinvention of it -- the same base URL, the same Basic/Bearer auth
model, the same flat JSON payload -- but with client-side validation that
catches what the gateway's own HTTP API silently lets through (see
validation.py's module docstring for the exact gap this closes), typed
exceptions instead of raw status codes, and the same retry/backoff policy
this codebase already trusts for its one other outbound HTTP integration
(pathways/email_pathway.py's Graph API sender).
"""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

from ._http import DEFAULT_TIMEOUT, HttpResult, post_with_retry, _safe_json
from .exceptions import (
    AlertManagerAuthError,
    AlertManagerBadRequestError,
    AlertManagerConnectionError,
    AlertManagerForbiddenError,
    AlertManagerHTTPError,
    AlertManagerServerError,
    AlertManagerServiceUnavailableError,
    AlertManagerValidationError,
)
from .validation import validate_alert


@dataclass
class SendResult:
    """
    Success-only receipt from AlertClient.send()/level-shortcut methods.
    Any non-success outcome (client-side validation failure, non-2xx
    response, connection error) raises instead of returning a result with
    `ok=False` -- per this project's "no failsafes, throw and quit"
    convention, a caller should never be able to silently ignore a failed
    send by forgetting to check a boolean.
    """

    ok: bool
    http_status: Optional[int]
    alert_id: Optional[str]
    message: Optional[str]
    duration_ms: int
    retries: int


@dataclass
class HealthStatus:
    """
    Parsed GET /health response. `db_ok=False` while `healthy=True` is a
    real, expected combination -- the gateway's Postgres check is
    fail-open and never affects the top-level status (only MQTT
    connectivity does). There is no third "degraded" state: the server
    doesn't have one, so this doesn't invent one either.
    """

    healthy: bool
    mqtt_connected: bool
    db_ok: bool
    db_detail: Optional[str]
    version: Optional[str]
    raw: Dict[str, Any]


class AlertClient:
    """
    Client for the Alert Manager HTTP gateway (`POST /alert`, `POST /test`,
    `GET /health`). See docs/CLIENTNODE.md for the full API/payload
    reference this wraps.
    """

    def __init__(
        self,
        base_url: str,
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        token: Optional[str] = None,
        default_source: Optional[str] = None,
        default_client: Optional[str] = None,
        timeout: Tuple[float, float] = DEFAULT_TIMEOUT,
        session: Optional[requests.Session] = None,
    ) -> None:
        """
        Exactly one of (username, password) or token must be supplied --
        raises ValueError at construction if both or neither are given
        (fail fast on a half-configured client rather than deferring the
        failure to the first send() call).

        `default_client` is a client-SIDE convenience only (pre-fills
        `client` on every send() call unless overridden per-call) -- do
        not confuse it with the account's own server-configured
        `default_client`, which the gateway applies independently and
        which this SDK has no visibility into or control over.

        `session`, if given, lets a caller inject their own
        `requests.Session` (connection pooling config, custom adapters,
        test doubles) instead of the internally-constructed default -- also
        the one seam that would let a future async client reuse this
        class's shape without a redesign.
        """
        has_username = username is not None
        has_password = password is not None
        if has_username != has_password:
            raise ValueError("username and password must be provided together")
        has_basic = has_username and has_password
        has_token = token is not None
        if has_basic and has_token:
            raise ValueError("provide either username/password or token, not both")
        if not has_basic and not has_token:
            raise ValueError("must provide either username/password (Basic auth) or token (Bearer auth)")

        self.base_url = base_url.rstrip("/")
        self._auth: Optional[Tuple[str, str]] = (username, password) if has_basic else None  # type: ignore[assignment]
        self._token = token
        self.default_source = default_source
        self.default_client = default_client
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()

    def _headers(self) -> Dict[str, str]:
        if self._token is not None:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    # -- alert submission -----------------------------------------------

    def send(
        self,
        *,
        level: str,
        category: str,
        title: str,
        message: str,
        source: Optional[str] = None,
        timestamp: Optional[int] = None,
        client: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **extra: Any,
    ) -> SendResult:
        """
        Send an alert. Runs client-side validation (validation.py) before
        making any network call -- this is the SDK's core value: catching
        what POST /alert's own HTTP validation does NOT catch (see
        validation.py's module docstring). Raises on any non-success
        outcome; see SendResult's docstring for why there's no `ok=False`
        return path.

        `**extra` is a forward-compatible passthrough for payload fields
        this SDK doesn't have dedicated parameters for yet (e.g.
        `display_config`, `routing`) -- passed through to the payload
        unvalidated, exactly as the gateway itself treats them.
        """
        effective_source = source if source is not None else self.default_source
        if effective_source is None:
            raise AlertManagerValidationError(
                "source is required: pass source= to send(), or set default_source= on AlertClient"
            )
        effective_timestamp = timestamp if timestamp is not None else int(time.time())
        effective_client = client if client is not None else self.default_client

        payload: Dict[str, Any] = {
            "timestamp": effective_timestamp,
            "level": level,
            "category": category,
            "title": title,
            "message": message,
            "source": effective_source,
        }
        if effective_client is not None:
            payload["client"] = effective_client
        if metadata is not None:
            payload["metadata"] = metadata
        payload.update(extra)

        normalized = validate_alert(payload)

        result = post_with_retry(
            self._session,
            f"{self.base_url}/alert",
            auth=self._auth,
            headers=self._headers(),
            json_body=normalized,
            timeout=self.timeout,
        )
        return self._handle_send_result(result, attempted_client=normalized.get("client"))

    def critical(self, *, category: str, title: str, message: str, **kwargs: Any) -> SendResult:
        return self.send(level="critical", category=category, title=title, message=message, **kwargs)

    def warning(self, *, category: str, title: str, message: str, **kwargs: Any) -> SendResult:
        return self.send(level="warning", category=category, title=title, message=message, **kwargs)

    def notice(self, *, category: str, title: str, message: str, **kwargs: Any) -> SendResult:
        return self.send(level="notice", category=category, title=title, message=message, **kwargs)

    def info(self, *, category: str, title: str, message: str, **kwargs: Any) -> SendResult:
        return self.send(level="info", category=category, title=title, message=message, **kwargs)

    def debug(self, *, category: str, title: str, message: str, **kwargs: Any) -> SendResult:
        return self.send(level="debug", category=category, title=title, message=message, **kwargs)

    # -- connectivity / diagnostics ---------------------------------------

    def test(self) -> Dict[str, Any]:
        """
        POST /test -- no payload to send; the gateway builds and echoes
        back its own fixed canned alert. Useful as a low-risk live
        credential/connectivity check: unlike send(), the server ignores
        whatever is sent, so there's no way to submit a malformed real
        alert through this path.
        """
        result = post_with_retry(
            self._session,
            f"{self.base_url}/test",
            auth=self._auth,
            headers=self._headers(),
            json_body=None,
            timeout=self.timeout,
        )
        if result.http_status is None:
            raise AlertManagerConnectionError(result.error_text or "connection failed")
        if result.ok:
            return result.response_body or {}
        self._raise_for_status(result)
        raise AssertionError("unreachable")  # _raise_for_status always raises

    def health(self) -> HealthStatus:
        """
        GET /health -- unauthenticated. Does NOT raise on a 503: that's a
        meaningful `healthy=False` result here (MQTT is down), not an
        error condition. Only a genuine connection failure (the request
        never completed at all) raises.
        """
        try:
            response = self._session.get(f"{self.base_url}/health", timeout=self.timeout)
        except requests.RequestException as e:
            raise AlertManagerConnectionError(str(e)) from e

        body = _safe_json(response) or {}
        db = body.get("db") or {}
        return HealthStatus(
            healthy=body.get("status") == "healthy",
            mqtt_connected=bool(body.get("mqtt_connected", False)),
            db_ok=db.get("status") == "ok",
            db_detail=db.get("detail"),
            version=body.get("version"),
            raw=body,
        )

    # -- result/error translation ------------------------------------------

    def _handle_send_result(self, result: HttpResult, *, attempted_client: Optional[str]) -> SendResult:
        if result.http_status is None:
            raise AlertManagerConnectionError(result.error_text or "connection failed")
        if result.ok:
            body = result.response_body or {}
            return SendResult(
                ok=True,
                http_status=result.http_status,
                alert_id=body.get("alert_id"),
                message=body.get("message"),
                duration_ms=result.duration_ms,
                retries=result.retries,
            )
        self._raise_for_status(result, attempted_client=attempted_client)
        raise AssertionError("unreachable")  # _raise_for_status always raises

    def _raise_for_status(self, result: HttpResult, *, attempted_client: Optional[str] = None) -> None:
        status = result.http_status
        message = AlertManagerHTTPError.message_from_body(
            result.response_body, result.raw_text or f"HTTP {status}"
        )
        if status == 400:
            raise AlertManagerBadRequestError(
                message, status_code=status, response_body=result.response_body, retries=result.retries
            )
        if status == 401:
            raise AlertManagerAuthError(
                message, status_code=status, response_body=result.response_body, retries=result.retries
            )
        if status == 403:
            raise AlertManagerForbiddenError(
                message,
                attempted_client=attempted_client,
                status_code=status,
                response_body=result.response_body,
                retries=result.retries,
            )
        if status == 503:
            raise AlertManagerServiceUnavailableError(
                message, status_code=status, response_body=result.response_body, retries=result.retries
            )
        if status is not None and status >= 500:
            raise AlertManagerServerError(
                message, status_code=status, response_body=result.response_body, retries=result.retries
            )
        raise AlertManagerHTTPError(
            message, status_code=status, response_body=result.response_body, retries=result.retries
        )

    # -- resource management ------------------------------------------------

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "AlertClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
