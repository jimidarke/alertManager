"""
Alert Manager SDK - client-side payload validation.

Mirrors src/alert_router.py's validate_alert() field-for-field. This exists
because of a real, verified gap in the gateway's own HTTP API: POST /alert
only validates field PRESENCE, never field VALUES -- an invalid level,
invalid category, non-int timestamp, or non-string source gets HTTP 200
back immediately. The actual value validation happens later, asynchronously,
on the MQTT-consumer side (the same validate_alert() this module mirrors),
AFTER the HTTP caller has already been told "success." If it fails there,
it's silently dropped: a bare `except Exception`, a stats counter
increment, nothing ever reaches the original caller.

Running this validator client-side, before the network call, is this SDK's
core value proposition -- it is the only way an SDK user finds out about
one of these mistakes at all.

Two deliberate differences from the server-side original, both intentional
and documented here rather than silent divergences:

1. Returns a NEW dict rather than mutating the input in place. The server's
   validate_alert() mutates its argument; an SDK caller's own dict should
   not be silently altered as a side effect of calling send().
2. The timestamp check is `isinstance(x, int) and not isinstance(x, bool)`,
   not just `isinstance(x, int)`. In Python, `bool` is a subclass of `int`,
   so the server's own naive check would technically accept True/False as
   a "valid" timestamp. This is a deliberate improvement, not a mismatch:
   rejecting it client-side is strictly safer than relying on the server's
   looser check.
"""

from typing import Any, Dict

from .exceptions import AlertManagerValidationError

REQUIRED_FIELDS = ["timestamp", "level", "category", "title", "message", "source"]

LEVEL_ALIASES = {
    "error": "critical",
    "err": "critical",
    "crit": "critical",
    "warn": "warning",
    "inf": "info",
    "dbg": "debug",
}

VALID_LEVELS = ["critical", "warning", "notice", "info", "debug"]

VALID_CATEGORIES = [
    "security",
    "system",
    "weather",
    "automation",
    "personal",
    "network",
    "application",
    "news",
    "business",
    "hardware",
    "environmental",
]


def validate_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and normalize an alert payload dict, mirroring the server's
    validate_alert() exactly (same required-field list, same alias table,
    same valid-value lists, same field validation order -- so error
    messages match what would eventually appear in the gateway's own logs,
    grep-able across both sides).

    Returns a new dict with `level`/`category`/`client` normalized
    (stripped, lowercased, aliases resolved) -- the input dict is never
    mutated. `title`/`message` are passed through with zero validation,
    matching the server exactly: neither side validates their content.

    Raises AlertManagerValidationError (also a ValueError) on the first
    violation found, in the same field order the server checks them.
    """
    for field in REQUIRED_FIELDS:
        if field not in alert:
            raise AlertManagerValidationError(f"Missing required field: {field}")

    normalized = dict(alert)

    timestamp = normalized["timestamp"]
    if not isinstance(timestamp, int) or isinstance(timestamp, bool):
        raise AlertManagerValidationError("timestamp must be Unix epoch integer")

    source = normalized["source"]
    if not isinstance(source, str):
        raise AlertManagerValidationError("source must be string")

    level = normalized["level"]
    if not isinstance(level, str):
        raise AlertManagerValidationError("level must be string")
    normalized_level = level.strip().lower()
    normalized_level = LEVEL_ALIASES.get(normalized_level, normalized_level)
    if normalized_level not in VALID_LEVELS:
        raise AlertManagerValidationError(
            f"Invalid alert level: '{level}'. Valid levels: {', '.join(VALID_LEVELS)}"
        )
    normalized["level"] = normalized_level

    category = normalized["category"]
    if not isinstance(category, str):
        raise AlertManagerValidationError("category must be string")
    normalized_category = category.strip().lower()
    if normalized_category not in VALID_CATEGORIES:
        raise AlertManagerValidationError(
            f"Invalid alert category: '{category}'. Valid categories: {', '.join(VALID_CATEGORIES)}"
        )
    normalized["category"] = normalized_category

    if "client" in normalized and normalized["client"] is not None:
        client = normalized["client"]
        if not isinstance(client, str):
            raise AlertManagerValidationError("client must be string")
        normalized["client"] = client.strip().lower()

    return normalized


def is_valid_alert(alert: Dict[str, Any]) -> bool:
    """
    Non-raising variant of validate_alert(), for pre-flight/UI-style checks
    where a caller wants a bool rather than a try/except. Prefer
    validate_alert() when you actually want the normalized dict or the
    specific reason for a failure.
    """
    try:
        validate_alert(alert)
        return True
    except AlertManagerValidationError:
        return False
