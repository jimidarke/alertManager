"""
Alert Manager SDK -- Python client for the Alert Manager HTTP gateway.

See README.md for a quick start, or docs/CLIENTNODE.md in the main
alertManager repo for the full wire-level API/payload reference this SDK
wraps.
"""

from ._version import __version__
from .client import AlertClient, HealthStatus, SendResult
from .excepthook import (
    exception_hook_scope,
    install_exception_hooks,
    report_exceptions,
    uninstall_exception_hooks,
)
from .exceptions import (
    AlertManagerAuthError,
    AlertManagerBadRequestError,
    AlertManagerConnectionError,
    AlertManagerError,
    AlertManagerForbiddenError,
    AlertManagerHTTPError,
    AlertManagerServerError,
    AlertManagerServiceUnavailableError,
    AlertManagerValidationError,
)
from .validation import is_valid_alert, validate_alert

__all__ = [
    "__version__",
    "AlertClient",
    "SendResult",
    "HealthStatus",
    "install_exception_hooks",
    "uninstall_exception_hooks",
    "exception_hook_scope",
    "report_exceptions",
    "validate_alert",
    "is_valid_alert",
    "AlertManagerError",
    "AlertManagerValidationError",
    "AlertManagerConnectionError",
    "AlertManagerHTTPError",
    "AlertManagerBadRequestError",
    "AlertManagerAuthError",
    "AlertManagerForbiddenError",
    "AlertManagerServiceUnavailableError",
    "AlertManagerServerError",
]
