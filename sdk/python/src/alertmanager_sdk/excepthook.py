"""
Alert Manager SDK - automatic exception reporting via sys.excepthook /
threading.excepthook.

Design principle settled on for this SDK: hooks report what nobody
anticipated (a genuinely uncaught exception), explicit `client.send()`/
level-shortcut calls report what you already know about. There is
deliberately no `logging.Handler` integration here -- combining one would
risk double-reporting the same failure (once via a `logger.critical(...,
exc_info=True)` call, again via the hook catching the eventual crash), and
this module's whole point is to be the ONE automatic channel, not one of
several that could overlap.

Two separate hooks are installed, not one, because they cover genuinely
different gaps:
- `sys.excepthook` fires for an exception that propagates uncaught to the
  top of the main thread.
- `threading.excepthook` (Python 3.8+) fires for an exception uncaught in
  any OTHER thread -- `sys.excepthook` does NOT see these at all; an
  uncaught exception in a background thread just prints to stderr and
  silently kills that thread otherwise.

Both hooks CHAIN whatever was previously installed rather than replacing
it outright -- another tool (a debugger, an IDE, a different monitoring
SDK) may already depend on the existing hook still running.

Both hooks are skipped entirely in interactive/REPL sessions
(`hasattr(sys, "ps1")`), matching Sentry's own SDK behavior on this exact
point -- an interactive session generates far too many "exceptions" that
are just typos being corrected, not a program crashing.

A failure inside the reporting call itself (e.g. `client.send()` raising)
is caught, logged to stderr, and swallowed -- it must never mask or
replace the user's REAL exception/traceback. This is a deliberate, narrow
exception to this project's "no failsafes" rule: a reporting hook that
throws and replaces the actual crash would be actively harmful, not merely
inconsistent.
"""

import sys
import threading
import traceback
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Iterator, Optional

from .client import AlertClient

_previous_sys_excepthook: Optional[Callable[..., None]] = None
_previous_threading_excepthook: Optional[Callable[..., None]] = None
_installed = False


def _is_interactive() -> bool:
    return hasattr(sys, "ps1")


def _report(
    client: AlertClient,
    *,
    source: Optional[str],
    category: str,
    level: str,
    exc_type: type,
    exc_value: BaseException,
    exc_tb: Any,
    thread_name: Optional[str] = None,
) -> None:
    """
    Best-effort report of one uncaught exception. Never raises -- any
    failure here is caught and logged to stderr, never allowed to mask the
    real exception this was called about. See module docstring.
    """
    try:
        tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        title = f"Unhandled exception: {exc_type.__name__}"
        if thread_name:
            title += f" (thread: {thread_name})"
        effective_source = source or client.default_source or "unknown"
        client.send(
            level=level,
            category=category,
            title=title,
            message=tb_text,
            source=effective_source,
        )
    except Exception as report_error:  # noqa: BLE001 -- deliberate, see module docstring
        sys.stderr.write(
            f"alertmanager_sdk: failed to report uncaught exception ({report_error!r}); "
            "original traceback follows unmodified below.\n"
        )


def install_exception_hooks(
    client: AlertClient,
    *,
    source: Optional[str] = None,
    category: str = "application",
    level: str = "critical",
) -> None:
    """
    Install both sys.excepthook and threading.excepthook, chaining
    whatever was previously installed. No-ops (does nothing, does not
    raise) in interactive/REPL sessions. Calling this twice replaces the
    SDK's own hook with a new one configured for the new `client`/
    `source`/`category`/`level`, still chaining whatever was installed
    before the FIRST call -- it does not stack two SDK-owned hooks.
    """
    global _previous_sys_excepthook, _previous_threading_excepthook, _installed

    if _is_interactive():
        return

    if not _installed:
        _previous_sys_excepthook = sys.excepthook
        _previous_threading_excepthook = threading.excepthook

    def _sys_hook(exc_type: type, exc_value: BaseException, exc_tb: Any) -> None:
        _report(
            client,
            source=source,
            category=category,
            level=level,
            exc_type=exc_type,
            exc_value=exc_value,
            exc_tb=exc_tb,
        )
        chained = _previous_sys_excepthook or sys.__excepthook__
        chained(exc_type, exc_value, exc_tb)

    def _threading_hook(args: Any) -> None:
        _report(
            client,
            source=source,
            category=category,
            level=level,
            exc_type=args.exc_type,
            exc_value=args.exc_value,
            exc_tb=args.exc_traceback,
            thread_name=getattr(args.thread, "name", None),
        )
        chained = _previous_threading_excepthook or threading.__excepthook__
        chained(args)

    sys.excepthook = _sys_hook
    threading.excepthook = _threading_hook
    _installed = True


def uninstall_exception_hooks() -> None:
    """Restores whatever sys.excepthook/threading.excepthook were installed before install_exception_hooks() last ran."""
    global _previous_sys_excepthook, _previous_threading_excepthook, _installed

    if not _installed:
        return

    sys.excepthook = _previous_sys_excepthook or sys.__excepthook__
    threading.excepthook = _previous_threading_excepthook or threading.__excepthook__
    _previous_sys_excepthook = None
    _previous_threading_excepthook = None
    _installed = False


@contextmanager
def exception_hook_scope(client: AlertClient, **kwargs: Any) -> Iterator[None]:
    """
    Scoped alternative to install_exception_hooks()/uninstall_exception_hooks()
    for callers who don't want a global interpreter-state side effect
    outside a specific block:

        with exception_hook_scope(client):
            main()
    """
    install_exception_hooks(client, **kwargs)
    try:
        yield
    finally:
        uninstall_exception_hooks()


def report_exceptions(client: AlertClient, **hook_kwargs: Any) -> Callable[[Callable], Callable]:
    """
    Decorator alternative for wrapping a single entry point (e.g. a cron
    job's main()) without touching global interpreter state at all.
    Catches, reports via client.send(), then ALWAYS re-raises -- this
    decorator observes, it never swallows a failure from its caller's
    perspective.
    """

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception:
                exc_type, exc_value, exc_tb = sys.exc_info()
                _report(
                    client,
                    source=hook_kwargs.get("source"),
                    category=hook_kwargs.get("category", "application"),
                    level=hook_kwargs.get("level", "critical"),
                    exc_type=exc_type,
                    exc_value=exc_value,
                    exc_tb=exc_tb,
                )
                raise

        return wrapped

    return decorator
