"""
Alert Manager - gateway.api.routes

`api_bp`: `GET /health`, `POST /alert`, `POST /test`, `GET /docs`,
`GET /docs/<filename>` -- ports src/http_gateway.py's routes of the same
name (read verbatim before writing this module) onto this package's
`GatewayConfig`/`MqttPublisher`/`UserStore`/`Principal` plumbing (Tasks
18-19) instead of the old JSON-config-driven `HTTPGateway` class. This
blueprint is Basic/Bearer only (`gateway.principal.API_ONLY`) -- it never
accepts a session cookie, so it is CSRF-exempted (see `api/__init__.py`)
and its auth failures are always a 401/403, never a 302 login redirect.

Payload validation for `POST /alert` (Content-Type check, JSON parsing,
required-fields check: timestamp/level/category/title/message/source) is
ported byte-for-byte from the original -- note that http_gateway.py itself
does NOT validate level/category *values* (aliasing/normalizing those is
src/alert_router.py's job, the actual MQTT consumer, confirmed by reading
both files); this route still doesn't either, for exact parity.

--------------------------------------------------------------------------
1. The breaking `POST /alert` authorization change (docs/OVERHAUL_PLAN.md
   4.4, `_require_alert_permission`/`_alert_effective_client` below)
--------------------------------------------------------------------------
Old behavior: any authenticated user could submit any `client` string
verbatim (or inherit their own `clientname` if omitted) -- no check that
the caller was actually allowed to write alerts *as* that client. New
behavior, exactly per the plan and `gateway.principal`'s own worked
example for this exact route:

    effective_client = payload['client'] or principal.default_client or ''
    requires principal.has_permission('alerts:write', effective_client)
    effective_client == '' (clientless/global) additionally requires
        principal.is_master_admin() -- has_permission() alone is not
        sufficient for the clientless case, because a non-master_admin
        role could in principle be granted an explicit client=='' scope
        (e.g. a misconfigured `add_scope(role, client='', ...)`) without
        being master_admin; the clientless case is deliberately locked to
        the one role the plan trusts with unscoped writes.

This closes "any node can spoof any client" -- a node's write access is
now bounded by its own role_scopes rows, not merely by being authenticated
at all.

--------------------------------------------------------------------------
2. CredentialCache composition (Task 19's cache.py "Integration note for
   Task 20"; `_resolve_basic_cached`/`_resolve_bearer_cached`/
   `_require_cached_alert_auth` below)
--------------------------------------------------------------------------
`POST /alert` is the one high-frequency route this blueprint serves (node
devices posting on every sensor tick) -- exactly what
`gateway/auth/cache.py`'s `CredentialCache` exists to front, per its own
docstring and docs/OVERHAUL_PLAN.md 4.4. That module deliberately does NOT
wire itself into `gateway.principal._resolve_basic`/`_resolve_bearer`,
because those are shared by Task 21's `logs` blueprint, which was never
asked to be cache-fronted -- wiring it in there would silently change
`logs`'s behavior as a side effect of this task.

Two options were sketched for Task 20 to resolve this (cache.py's
docstring, this task's brief): (a) a small `_resolve_basic_cached`-style
wrapper local to this route, adapting the cache's result/exception into
the `Optional[Principal]` shape `require_auth`/`require_permission`
expect, or (b) something cleaner. What's implemented below is a variant of
(a), refined on one point the brief itself flags as unresolved: a
`CredentialCacheUnavailable` exception has no representation in the
`Optional[Principal]` contract (`None` already means "not authenticated" ->
401, a different thing than "could not determine" -> 503), so trying to
"adapt" it into that contract would conflate the two. Instead:

- `_resolve_basic_cached()`/`_resolve_bearer_cached()` mirror
  `gateway.principal._resolve_basic`/`_resolve_bearer` exactly, except the
  DB call is wrapped in `CredentialCache.check()` (per cache.py's
  documented `key`/`credential`/`verify` shape) instead of calling
  `UserStore.verify_login`/`verify_token` directly. `CredentialCacheUnavailable`
  is deliberately NOT caught here -- it propagates to...
- `_require_cached_alert_auth`, a `require_auth`-shaped decorator (own
  local copy, not reusing `gateway.principal.require_auth`, since that
  decorator has no seam for a third "auth infrastructure is unavailable"
  outcome) that tries basic-then-bearer (this blueprint has no session
  method to consider) and turns:
    - a resolved `Principal` -> `g.principal` set, view called (200 path);
    - `None` (bad/absent credentials) -> the same 401 challenge shape
      `_auth_failure_response` would produce for `API_ONLY` (reconstructed
      locally rather than imported -- see the module docstring section 3
      below for why importing that private helper wasn't necessary or
      desirable here);
    - `CredentialCacheUnavailable` -> 503 (docs/OVERHAUL_PLAN.md 4.4:
      "beyond -> 503 on /alert"), fail-CLOSED on write auth, distinct from
      `/health`'s fail-OPEN DB check below.

This is NOT a bypass of `require_auth`/`require_permission` (the thing
this task's brief specifically warns against hand-rolling) -- it is a
same-shaped, independently-testable sibling decorator, confined to this
one module, that composes with its own `_require_alert_permission` in
exactly the same nested-decorator style `gateway.principal.require_permission`
composes with `require_auth`. `logs` (Task 21) still calls the real,
uncached `_resolve_basic`/`_resolve_bearer` via `require_auth`/
`require_permission` -- nothing here touches those.

`POST /test` (low-frequency, human-triggered) deliberately uses the plain,
uncached `require_auth(methods=API_ONLY)` from `gateway.principal` --
docs/OVERHAUL_PLAN.md 4.4 frames the cache as existing specifically for
`/alert`'s node traffic, and there is no reason to give `/test` a stale-read
window it doesn't need.

--------------------------------------------------------------------------
3. 401/403 JSON-ification (both "known gaps" carried forward from Task
   18/19's review; `_jsonify_auth_errors` below)
--------------------------------------------------------------------------
First, on the redirect half of the originally-flagged concern:
`gateway.principal._auth_failure_response` only ever redirects when
`"session" in methods` -- and `"session"` is never in `API_ONLY`
(`("basic", "bearer")`), this blueprint's only auth policy. So the
redirect-vs-401 ambiguity that motivated the original review comment
**does not apply to this blueprint at all** -- confirmed by reading
`_auth_failure_response`'s body, not assumed. That half of the gap was
either a general note for whichever blueprint ends up mixing session +
API auth (that's `logs`, Task 21, via `SESSION_OR_API`) or simply
precautionary; either way, `api` never redirects, with or without any
further change.

What DOES apply here: this blueprint's 401/403 bodies, as `require_auth`/
`require_permission` produce them today, are plain-text/HTML, while every
other response this blueprint returns is JSON. Fixing that by making
`gateway.principal._auth_failure_response` raise (via `abort()`/a custom
`HTTPException`) instead of `return`ing a `Response`, so a blueprint-level
`@api_bp.errorhandler(401)` could intercept it, was considered and
verified to work in isolation -- but it changes `require_auth`/
`require_permission`'s return-vs-raise contract for every caller, not just
this blueprint, and breaks several of Task 18's existing direct-call unit
tests in tests/test_gateway_principal.py (which call the decorated view
function directly inside a `test_request_context` and assert on a
returned `Response`, exactly the way the existing 403/`abort()` path is
NOT tested there -- see `test_require_permission_403_when_scope_does_not_match`,
which already uses `pytest.raises(Forbidden)` because `abort()` already
raises). That is a bigger, farther-reaching change than this one
blueprint's formatting preference warrants.

Instead, this module uses the OTHER path the brief itself sketches:
"blueprint/path-aware content negotiation at the response-construction
site" -- `_jsonify_auth_errors`, an `@api_bp.after_request` hook. This was
verified empirically (not assumed) to fire for BOTH shapes of failure this
blueprint can produce: a plain `Response` object returned directly (the
401 case, ours and `require_auth`'s alike) AND the response Flask derives
from an `abort()`-raised `HTTPException` like `Forbidden` (the 403 case) --
Flask treats a raised `HTTPException` as a normal, "handled" error and
still runs blueprint-scoped `after_request` functions on the response it
produces, confirmed with a standalone reproduction before writing this.
Being registered on `api_bp` specifically (not `app`) means it ONLY ever
runs for requests dispatched to this blueprint's own routes --
`logs`/`dashboard`/`admin`/`auth` keep their exact current behavior,
unmodified, since `gateway.principal` itself is untouched by this task.
"""

import json
import logging
import os
import time
from functools import wraps
from typing import Any, Dict, Optional, Tuple

from flask import Blueprint, Response, abort, current_app, g, jsonify, request, send_from_directory

from gateway.auth.cache import CredentialCache, CredentialCacheUnavailable
from gateway.db import DBUnavailable, iso_now, pooled_cursor
from gateway.principal import API_ONLY, Principal, require_auth
from version import __version__

logger = logging.getLogger("gateway.api")

api_bp = Blueprint("api", __name__)

_REQUIRED_ALERT_FIELDS = ["timestamp", "level", "category", "title", "message", "source"]
_ALERT_WRITE_PERMISSION = "alerts:write"


# ---------------------------------------------------------------------------
# app.extensions accessors
# ---------------------------------------------------------------------------

def _user_store():
    return current_app.extensions["user_store"]


def _credential_cache() -> CredentialCache:
    return current_app.extensions["credential_cache"]


def _mqtt_publisher():
    return current_app.extensions["mqtt_publisher"]


def _db_pool():
    return current_app.extensions["db_pool"]


# ---------------------------------------------------------------------------
# /health -- DB probe. Port of http_gateway.py's `_check_db_health`, adapted
# to use this package's real connection pool (gateway/db.py, Task 18)
# instead of the original's ad hoc one-off psycopg2.connect(): the original
# service had no hard Postgres dependency at all (no POSTGRES_* env vars
# wired up for it in compose), so it either skipped the probe entirely
# ('not_configured') or opened a throwaway connection; this package's
# Postgres pool is a hard, fail-fast-at-startup dependency (gateway/db.py),
# so borrowing a pooled connection is the natural, already-available check
# rather than re-deriving DSN parameters from the environment a second time.
# Exceptions (including pool exhaustion) are caught and reported as
# 'status': 'error', never raised -- /health must stay 200 on a DB blip
# (fail-OPEN on health status; see the route below). The getconn/cursor/
# putconn boilerplate itself is `gateway.db.pooled_cursor` (shared with
# `dashboard/routes.py` and `admin/routes.py`) -- only the fail-open
# dict shape below is specific to this route.
# ---------------------------------------------------------------------------

def _check_db_health() -> Dict[str, Any]:
    try:
        with pooled_cursor(_db_pool()) as cur:
            cur.execute("SELECT 1")
        return {"status": "ok"}
    except DBUnavailable as exc:
        logger.warning("Health check DB probe failed: %s", exc)
        return {"status": "error", "detail": str(exc)}


@api_bp.route("/health", methods=["GET"])
def health():
    """
    Port of http_gateway.py's `/health` (already carrying the Task 4
    `db`/`mqtt` detail fields in the CURRENT file -- confirmed by reading
    it, not re-derived). Only MQTT/process-level trouble fails the health
    check (503); a Postgres blip is reported in the `db` object but never
    changes the HTTP status (docs/OVERHAUL_PLAN.md 1.4/1.5: "the gateway
    must not restart-loop when PG hiccups").
    """
    mqtt_connected = _mqtt_publisher().is_connected()
    db_health = _check_db_health()

    http_status = 200 if mqtt_connected else 503

    return (
        jsonify(
            {
                "status": "healthy" if mqtt_connected else "unhealthy",
                "service": "http-gateway",
                "version": __version__,
                "timestamp": iso_now(),
                "mqtt_connected": mqtt_connected,
                "mqtt": {"connected": mqtt_connected},
                "db": db_health,
            }
        ),
        http_status,
    )


# ---------------------------------------------------------------------------
# CredentialCache-fronted Basic/Bearer resolution -- POST /alert only. See
# module docstring section 2. Deliberately NOT touching
# gateway.principal._resolve_basic/_resolve_bearer (shared by Task 21's
# `logs` blueprint).
# ---------------------------------------------------------------------------

def _resolve_basic_cached() -> Optional[Principal]:
    auth = request.authorization
    if auth is None or auth.type != "basic":
        return None

    store = _user_store()
    data = _credential_cache().check(
        key=f"basic:{auth.username}",
        credential=auth.password,
        verify=lambda: store.verify_login(auth.username, auth.password, source_ip=request.remote_addr),
    )
    if data is None:
        return None
    return Principal(data, auth_method="basic")


def _resolve_bearer_cached() -> Optional[Principal]:
    auth = request.authorization
    if auth is None or auth.type != "bearer":
        return None

    store = _user_store()
    data = _credential_cache().check(
        key=f"bearer:{auth.token}",
        credential=auth.token,
        verify=lambda: store.verify_token(auth.token),
    )
    if data is None:
        return None
    return Principal(data, auth_method="bearer")


def _resolve_cached_principal() -> Optional[Principal]:
    """Try basic then bearer (API_ONLY order; no session method exists for this blueprint).

    If either resolver raises `CredentialCacheUnavailable` it propagates
    immediately -- there is at most one `Authorization` header per request,
    so whichever resolver actually matches its `auth.type` is the only one
    that can ever call `CredentialCache.check()` (and so the only one that
    can ever raise); the other returns `None` without touching the cache at
    all, exactly like `gateway.principal.resolve_principal`'s own dispatch.
    """
    for resolver in (_resolve_basic_cached, _resolve_bearer_cached):
        principal = resolver()
        if principal is not None:
            return principal
    return None


def _basic_challenge_response() -> Response:
    """
    Same 401 challenge shape `gateway.principal._auth_failure_response`
    produces for `API_ONLY` (always this shape for that policy -- see
    module docstring section 3). Reconstructed locally rather than
    importing that private helper: it keeps this cache-fronted path fully
    self-contained, and the shape is a two-line literal, not meaningfully
    at risk of drifting out of sync.
    """
    return Response("Authentication required\n", 401, {"WWW-Authenticate": 'Basic realm="Alert Gateway"'})


def _require_cached_alert_auth(fn):
    """`require_auth(methods=API_ONLY)`-shaped, but cache-fronted and 503-capable. See module docstring section 2."""

    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            principal = _resolve_cached_principal()
        except CredentialCacheUnavailable:
            logger.warning("CredentialCache unavailable for POST /alert from %s", request.remote_addr)
            return (
                jsonify(
                    {
                        "status": "error",
                        "error": "Credential verification temporarily unavailable",
                        "timestamp": iso_now(),
                    }
                ),
                503,
            )
        if principal is None:
            return _basic_challenge_response()
        g.principal = principal
        return fn(*args, **kwargs)

    return wrapped


def _alert_effective_client() -> str:
    """
    Body-based client resolution: payload `client` else
    `principal.default_client` else `''`. Not fully normalized here --
    `_require_alert_permission` does the final `.strip().lower()`, matching
    `require_permission`'s own established convention -- but the raw
    payload value IS pre-stripped before the `or` fallback decision below,
    which deliberately deviates from `gateway.principal`'s own docstring
    worked example (`payload.get("client") or g.principal.default_client or
    ""`, with no pre-strip). A bare `or` chain treats a whitespace-only
    payload `client` (e.g. `"   "`) as a *present* value (non-empty string
    -- Python truthiness), so it would never fall back to
    `default_client`; deferring normalization to the caller, as the
    docstring's example does, would then authorize (and publish) an alert
    against the wrong effective client for a merely-blank field. The
    original `http_gateway.py:469` inheritance check
    (`alert_data.get('client', '').strip() == ''`) treats missing, empty,
    AND whitespace-only the same way -- this pre-strip restores that exact
    parity.
    """
    payload = request.get_json(silent=True) or {}
    raw_client = payload.get("client")
    if isinstance(raw_client, str):
        raw_client = raw_client.strip()
    return raw_client or g.principal.default_client or ""


def _unknown_client_response(client: str) -> Tuple[Response, int]:
    """
    400, not 403/422: matches this route's own established vocabulary for
    "the payload's field values are unacceptable" (Content-Type/JSON-parse/
    empty-body/missing-fields all return a bespoke 400 body keyed on
    "error", no "status" key). Distinct from the generic
    {"status":"error","error":"forbidden"} body `_jsonify_auth_errors`
    would otherwise stamp onto a bare `abort(403)` -- returning real
    `application/json` here directly means that hook (which only rewrites
    401/403 responses) leaves this response untouched, so the caller gets
    THIS specific message, not a generic "forbidden" indistinguishable
    from a missing-scope 403.
    """
    return (
        jsonify(
            {
                "error": "Unknown client",
                "detail": f"Client {client!r} is not a registered client.",
                "client": client,
                "timestamp": iso_now(),
            }
        ),
        400,
    )


def _require_alert_permission(fn):
    """
    Wraps `_require_cached_alert_auth` (auth) with this route's specific
    authorization rule (module docstring section 1): `has_permission`
    for the effective client, AND clientless additionally requires
    `is_master_admin()`. On success, stashes the already-normalized
    effective client on `g.alert_client` so the view can reuse the EXACT
    value that was authorized, instead of recomputing (and risking it
    silently drifting from what was actually checked).

    A non-empty effective client must also be a registered, active row in
    the `clients` table (sql/migrations/0004_clients.sql) -- checked
    AFTER authorization, not before: `has_permission()` is a pure
    in-memory lookup on `principal.scopes` (no DB call), so an
    unauthorized caller is already rejected by the existing 403 without
    ever touching the registry or learning whether the client name is
    real. Checking existence first would let any authenticated principal,
    regardless of scope, probe which client names exist. `client == ""`
    (clientless) is never checked against the registry -- it's a
    distinct, already-authorized valid state, not a missing client name.
    """

    @wraps(fn)
    def permission_checked(*args, **kwargs):
        client = (_alert_effective_client() or "").strip().lower()
        principal = g.principal

        if not principal.has_permission(_ALERT_WRITE_PERMISSION, client):
            abort(403)
        if client == "" and not principal.is_master_admin():
            abort(403)

        if client and not _user_store().client_exists(client):
            return _unknown_client_response(client)

        g.alert_client = client
        return fn(*args, **kwargs)

    return _require_cached_alert_auth(permission_checked)


@api_bp.route("/alert", methods=["POST"])
@_require_alert_permission
def send_alert():
    """
    Main alert endpoint. Payload validation (Content-Type, JSON parsing,
    required fields) is ported byte-for-byte from http_gateway.py's
    `send_alert` -- only the authorization logic (`_require_alert_permission`
    above, run before this view body even starts) and the client
    resolution/`_gateway` metadata block below have changed.
    """
    try:
        logger.info(f"Received alert request from {request.remote_addr}")
        logger.debug(f"Content-Type: {request.content_type}")

        raw_data = request.get_data(as_text=True)
        logger.debug(f"Request data: {raw_data}")

        if not request.is_json:
            logger.warning(f"Invalid Content-Type from {request.remote_addr}: {request.content_type}")
            return (
                jsonify(
                    {
                        "error": "Content-Type must be application/json",
                        "received_content_type": request.content_type,
                        "timestamp": iso_now(),
                    }
                ),
                400,
            )

        try:
            alert_data = request.get_json()
        except Exception as json_error:
            logger.warning(f"Invalid JSON from {request.remote_addr}: {str(json_error)}")
            logger.debug(f"Raw data: {raw_data[:200]}")
            return (
                jsonify(
                    {
                        "error": "Invalid JSON format",
                        "detail": "Request body must be valid JSON. Received invalid or malformed JSON.",
                        "json_error": str(json_error),
                        "hint": 'Ensure request body contains valid JSON like: {"timestamp": 1234567890, "level": "info", ...}',
                        "timestamp": iso_now(),
                    }
                ),
                400,
            )

        if not alert_data:
            logger.warning(f"Empty JSON payload from {request.remote_addr}")
            return (
                jsonify(
                    {
                        "error": "Empty JSON payload",
                        "detail": "Request body cannot be empty",
                        "timestamp": iso_now(),
                    }
                ),
                400,
            )

        logger.debug(f"Parsed alert data: {json.dumps(alert_data, indent=2)}")

        missing_fields = [field for field in _REQUIRED_ALERT_FIELDS if field not in alert_data]
        if missing_fields:
            return (
                jsonify(
                    {
                        "error": f"Missing required fields: {', '.join(missing_fields)}",
                        "required_fields": _REQUIRED_ALERT_FIELDS,
                        "timestamp": iso_now(),
                    }
                ),
                400,
            )

        # Breaking authorization change (module docstring section 1):
        # `g.alert_client` was already computed AND permission-checked by
        # `_require_alert_permission` -- reuse it verbatim so the alert
        # actually published is guaranteed to be the one that was
        # authorized, rather than a value recomputed here that could
        # (in principle) drift from it.
        alert_data["client"] = g.alert_client

        alert_data["_gateway"] = {
            "source": "http-gateway",
            "received_at": int(time.time()),
            "client_ip": request.remote_addr,
            "user_agent": request.headers.get("User-Agent", "unknown"),
            "authenticated_user": g.principal.username,
            "user_type": "master_admin" if g.principal.is_master_admin() else "user",
        }

        success = _mqtt_publisher().forward_to_mqtt(alert_data)

        if success:
            source_id = alert_data.get("source", "unknown")
            return (
                jsonify(
                    {
                        "status": "success",
                        "message": "Alert forwarded to MQTT broker",
                        "timestamp": iso_now(),
                        "alert_id": f"{source_id}-{int(time.time())}",
                    }
                ),
                200,
            )
        else:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Failed to forward alert to MQTT broker",
                        "timestamp": iso_now(),
                    }
                ),
                500,
            )

    except Exception as e:
        logger.error(f"Unexpected error processing alert from {request.remote_addr}: {str(e)}", exc_info=True)
        try:
            if "alert_data" in locals():
                logger.error(f"Alert data: {json.dumps(alert_data, indent=2)}")
        except Exception:
            pass
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Internal server error",
                    "detail": "An unexpected error occurred while processing the alert",
                    "timestamp": iso_now(),
                }
            ),
            500,
        )


@api_bp.route("/test", methods=["POST"])
@require_auth(methods=API_ONLY)
def test_alert():
    """
    Port of http_gateway.py's `/test`: any authenticated Basic/Bearer
    principal may trigger a sample alert (no `alerts:write`/client
    authorization -- that rule is specific to `/alert`'s breaking change,
    per the task brief; `/test` is unchanged). Uncached (see module
    docstring section 2: this is a low-frequency, human-triggered route,
    not the high-frequency node traffic CredentialCache exists for).

    `metadata.authenticated_user` is `g.principal.username` (a plain
    string) rather than the original's `request.authenticated_user` (which
    embedded the ENTIRE legacy user dict, including its raw
    `password_hash`, into the alert payload published to MQTT and then
    logged/emailed downstream) -- `Principal` has no such field to leak,
    and this is a security-motivated deviation from a literal port, not an
    oversight.

    `category` is `"system"`, not the original's `"test"` -- CLAUDE.md's
    fixed category list has no `"test"` value, so the original's sample
    payload would itself have been rejected by `alert_router.py`'s
    downstream `validate_alert()`. A trivial, unrelated-to-authorization
    one-line fix, made drive-by since it's otherwise a self-test route
    that silently never worked end-to-end.
    """
    test_payload = {
        "timestamp": int(time.time()),
        "level": "info",
        "category": "system",
        "title": "HTTP Gateway Test",
        "message": "Test alert from HTTP gateway",
        "source": "http-gateway-test",
        "metadata": {
            "test": True,
            "client_ip": request.remote_addr,
            "authenticated_user": g.principal.username,
        },
    }

    success = _mqtt_publisher().forward_to_mqtt(test_payload)

    if success:
        return (
            jsonify(
                {
                    "status": "success",
                    "message": "Test alert sent successfully",
                    "alert": test_payload,
                    "timestamp": iso_now(),
                }
            ),
            200,
        )
    else:
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to send test alert",
                    "timestamp": iso_now(),
                }
            ),
            500,
        )


@api_bp.route("/docs", methods=["GET"])
def get_docs():
    """Serves CLIENTNODE.md as text/markdown, unauthenticated: it's the public sender contract (also on GitHub)."""
    try:
        doc_paths = [
            "/data/docs/CLIENTNODE.md",
            "./docs/CLIENTNODE.md",
            "./CLIENTNODE.md",
            "../CLIENTNODE.md",
        ]
        doc_path = next((p for p in doc_paths if os.path.exists(p)), None)

        if not doc_path:
            return (
                jsonify(
                    {
                        "status": "error",
                        "message": "Documentation not found",
                        "timestamp": iso_now(),
                    }
                ),
                404,
            )

        with open(doc_path, "r", encoding="utf-8") as f:
            content = f.read()

        return Response(content, mimetype="text/markdown")

    except Exception as e:
        logger.error(f"Error serving documentation: {e}", exc_info=True)
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Failed to serve documentation",
                    "error_detail": str(e),
                    "timestamp": iso_now(),
                }
            ),
            500,
        )


@api_bp.route("/docs/<path:filename>", methods=["GET"])
def serve_docs(filename):
    """
    Port of http_gateway.py's `/docs/<filename>` -- deliberately WITHOUT
    auth, exactly matching the original (its own docstring calls this out
    explicitly: "without authentication"), same as `/docs` above.
    """
    docs_dir = "/data/docs"

    logger.info(f"Docs request for: {filename}")

    if not filename or filename.strip() == "":
        logger.warning("Empty filename requested")
        return jsonify({"error": "Filename required"}), 400

    if ".." in filename or filename.startswith("/"):
        logger.warning(f"Directory traversal attempt: {filename}")
        return jsonify({"error": "Invalid filename"}), 400

    file_path = os.path.join(docs_dir, filename)

    real_file_path = os.path.realpath(file_path)
    real_docs_dir = os.path.realpath(docs_dir)

    if not real_file_path.startswith(real_docs_dir):
        logger.warning(f"Path outside docs directory: {real_file_path}")
        return jsonify({"error": "Invalid filename"}), 400

    if not os.path.exists(file_path):
        logger.warning(f"File not found: {file_path}")
        return jsonify({"error": "File not found"}), 404

    if os.path.isdir(file_path):
        logger.warning(f"Directory requested: {file_path}")
        return jsonify({"error": "Directory browsing not allowed"}), 403

    try:
        return send_from_directory(docs_dir, filename)
    except Exception as e:
        logger.error(f"Error serving docs file {filename}: {e}", exc_info=True)
        return jsonify({"error": "Failed to serve file"}), 500


# ---------------------------------------------------------------------------
# 401/403 JSON-ification -- see module docstring section 3.
# ---------------------------------------------------------------------------

@api_bp.after_request
def _jsonify_auth_errors(response):
    if response.status_code in (401, 403) and response.mimetype != "application/json":
        body = {
            "status": "error",
            "error": "authentication required" if response.status_code == 401 else "forbidden",
            "timestamp": iso_now(),
        }
        new_response = jsonify(body)
        new_response.status_code = response.status_code
        for header, value in response.headers.items():
            if header.lower() not in ("content-type", "content-length"):
                new_response.headers[header] = value
        return new_response
    return response
