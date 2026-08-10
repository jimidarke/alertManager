"""
Global automatic-exception-reporting example: any exception that escapes
uncaught -- main thread or background thread -- gets reported as a
critical alert before the program crashes exactly as it would have anyway.

This is the ONE automatic reporting path this SDK provides -- there is no
logging.Handler integration (see README.md for why). Anything that ISN'T
an uncaught exception (a status update, a business event, a warning you
already know about) should be an explicit client.send()/client.warning()/
etc. call instead.

Run with real credentials (never commit them):
    ALERT_MANAGER_URL=https://alert.d-t.pw \
    ALERT_MANAGER_USERNAME=my-service \
    ALERT_MANAGER_PASSWORD=... \
    python3 examples/excepthook_global_install.py
"""

import os
import threading

from alertmanager_sdk import AlertClient, install_exception_hooks


def _background_work() -> None:
    raise RuntimeError("something broke in a background thread")


def main() -> None:
    client = AlertClient(
        os.environ.get("ALERT_MANAGER_URL", "https://alert.d-t.pw"),
        username=os.environ["ALERT_MANAGER_USERNAME"],
        password=os.environ["ALERT_MANAGER_PASSWORD"],
        default_source="excepthook-example",
    )

    # Chains any previously-installed sys.excepthook/threading.excepthook --
    # a debugger, pdb's post-mortem, another monitoring SDK's hook, etc.
    # still runs afterward. No-ops automatically in an interactive/REPL
    # session, so this is safe to call unconditionally at startup.
    install_exception_hooks(client)

    # Demonstrates the background-thread case: sys.excepthook alone would
    # never see this -- it's threading.excepthook's job.
    t = threading.Thread(target=_background_work, name="worker")
    t.start()
    t.join()

    # Demonstrates the main-thread case. Comment this out to see the
    # background-thread report alone.
    raise ValueError("something broke in the main thread")


if __name__ == "__main__":
    main()
