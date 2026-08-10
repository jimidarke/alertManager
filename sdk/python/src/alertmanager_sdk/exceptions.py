"""
Alert Manager SDK - exception hierarchy.

Every exception this SDK raises derives from `AlertManagerError`. Beyond
that, the hierarchy mirrors exactly what the gateway's HTTP API can
actually tell a caller -- verified directly against
src/gateway/api/routes.py, not guessed at:

- 401/403/500/503 bodies are inconsistent in shape from the 400 bodies
  (`{"error": ...}` vs `{"status": "error", "error"|"message": ...}`) --
  `AlertManagerHTTPError.message` normalizes both behind one string so
  callers never need to know which shape a given status code used.
- The 403 body carries ZERO detail about which client/permission was
  missing (`{"status":"error","error":"forbidden",...}` for every possible
  cause). `AlertManagerForbiddenError`'s message can only ever restate what
  the SDK itself attempted to send -- never anything sourced from the
  response, because the response has nothing to source.
- A locked-out account, a disabled account, a wrong password, and a
  nonexistent username are ALL indistinguishable from the gateway's
  response: `UserStore.verify_login()` returns `None` uniformly for every
  one of those causes, and there is no `Retry-After`/`X-RateLimit-*`
  header anywhere in the codebase to key off of either. This is why there
  is deliberately no `AlertManagerLockedError` or similar -- inventing one
  would present false precision the API does not provide.
"""

from typing import Any, Dict, Optional


class AlertManagerError(Exception):
    """Base class for every exception this SDK raises."""


class AlertManagerValidationError(AlertManagerError, ValueError):
    """
    Client-side validation failed before any network call was made --
    mirrors src/alert_router.py's validate_alert() (see validation.py).

    Also a ValueError, so callers who only catch the common Python idiom
    "you gave me bad input" -> ValueError still catch this without needing
    to know about the SDK's own exception hierarchy.
    """


class AlertManagerConnectionError(AlertManagerError):
    """
    The request never completed -- a requests.RequestException (timeout,
    DNS failure, connection refused, etc.). Never auto-retried by this SDK:
    it's ambiguous whether the server actually processed a non-idempotent
    POST before the connection failed, and retrying risks a duplicate
    alert. Mirrors pathways/email_pathway.py's _post_sendmail, the one
    existing retry precedent in this codebase, which makes the identical
    choice for the identical reason.
    """


class AlertManagerHTTPError(AlertManagerError):
    """
    Base class for any non-2xx HTTP response actually received from the
    gateway. Carries the parsed status/body so callers who need more than
    the message can get at it, but normalizes the two genuinely
    inconsistent server error-body shapes behind `.message`.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        response_body: Optional[Dict[str, Any]] = None,
        retries: int = 0,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.response_body = response_body
        self.retries = retries

    @staticmethod
    def message_from_body(body: Optional[Dict[str, Any]], fallback: str) -> str:
        """
        Extract a human-readable message from either known server error-body
        shape: `{"error": "..."}` (400s) or `{"status": "error", "error"|
        "message": "..."}` (401/403/500/503). Falls back to `fallback` if
        neither key is present or the body couldn't be parsed as JSON at all
        (e.g. a proxy-generated error page).
        """
        if not body:
            return fallback
        return body.get("error") or body.get("message") or fallback


class AlertManagerBadRequestError(AlertManagerHTTPError):
    """
    HTTP 400 -- the gateway rejected the payload (bad Content-Type, invalid
    JSON, empty payload, or missing required fields). Should be rare if
    client-side validation ran first (see validation.py) -- reachable if
    validation was bypassed or the server's own rules ever diverge from
    what this SDK mirrors.
    """


class AlertManagerAuthError(AlertManagerHTTPError):
    """
    HTTP 401 -- credentials missing, wrong, or otherwise rejected. Per this
    module's docstring: the gateway cannot and does not distinguish "wrong
    password" from "account locked" from "account disabled" from
    "nonexistent user" in this response -- treat this uniformly as "fix
    your credentials," not as a signal to branch on a more specific cause.
    """


class AlertManagerForbiddenError(AlertManagerHTTPError):
    """
    HTTP 403 -- authenticated, but not authorized for the effective client
    (or a clientless submission attempted by a non-master_admin account).
    The response body carries no detail about which client/permission was
    missing; `.message` can only reflect what the SDK itself attempted to
    send, never anything the server reported.
    """

    def __init__(
        self,
        message: str,
        *,
        attempted_client: Optional[str] = None,
        status_code: Optional[int] = None,
        response_body: Optional[Dict[str, Any]] = None,
        retries: int = 0,
    ) -> None:
        super().__init__(message, status_code=status_code, response_body=response_body, retries=retries)
        self.attempted_client = attempted_client


class AlertManagerServiceUnavailableError(AlertManagerHTTPError):
    """
    HTTP 503 -- only possible from POST /alert (never /test): the
    gateway's CredentialCache couldn't verify credentials because both the
    fresh cache and the live DB check failed (a Postgres blip). This is a
    "retry later" condition, not "your fault" -- distinct from the
    permanent 400/401/403 cases.
    """


class AlertManagerServerError(AlertManagerHTTPError):
    """
    HTTP 500 -- either the MQTT forward to the broker failed, or the
    gateway hit an unexpected exception. Not necessarily permanent, but
    this SDK does not auto-retry 500s beyond the bounded matrix in
    _http.py (429/5xx, capped at 2 retries) -- see that module.
    """
