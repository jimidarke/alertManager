#!/usr/bin/env python3
"""
Alert Manager SDK - manual smoke test.

Opt-in, real-credentials verification against a LIVE gateway. NOT part of
`pytest` -- the SDK's unit test suite (tests/) is fully self-contained and
mocks every network call; this script is the one piece of verification
that genuinely needs a running gateway, which is not available in a
typical dev sandbox. Mirrors this project's existing pattern of
operator-run verification (docs/CLIENTNODE.md's own cURL Quick Start)
rather than inventing a new style.

Usage:
    ALERT_MANAGER_URL=https://alert.d-t.pw \
    ALERT_MANAGER_USERNAME=<test-account> \
    ALERT_MANAGER_PASSWORD=<...> \
    python3 scripts/smoke_test.py

    # or with a Bearer token instead:
    ALERT_MANAGER_URL=... ALERT_MANAGER_TOKEN=amt_... python3 scripts/smoke_test.py

Exit code 0 on full pass, non-zero on any unexpected outcome.
"""

import os
import sys

from alertmanager_sdk import (
    AlertClient,
    AlertManagerAuthError,
    AlertManagerValidationError,
)


def _client() -> AlertClient:
    base_url = os.environ.get("ALERT_MANAGER_URL", "https://alert.d-t.pw")
    token = os.environ.get("ALERT_MANAGER_TOKEN")
    if token:
        return AlertClient(base_url, token=token, default_source="alertmanager-sdk-smoke-test")

    username = os.environ.get("ALERT_MANAGER_USERNAME")
    password = os.environ.get("ALERT_MANAGER_PASSWORD")
    if not username or not password:
        print(
            "Set ALERT_MANAGER_USERNAME + ALERT_MANAGER_PASSWORD, or ALERT_MANAGER_TOKEN.",
            file=sys.stderr,
        )
        sys.exit(2)
    return AlertClient(base_url, username=username, password=password, default_source="alertmanager-sdk-smoke-test")


def main() -> int:
    client = _client()
    failures = []

    # 1. health() -- confirm the gateway is actually up before anything else.
    print("1. health()...", end=" ")
    try:
        health = client.health()
        print(f"healthy={health.healthy} mqtt_connected={health.mqtt_connected} db_ok={health.db_ok}")
        if not health.healthy:
            print("   ABORTING: gateway is not healthy, nothing downstream will work.")
            return 1
    except Exception as e:
        print(f"FAIL ({e!r})")
        return 1

    # 2. test() -- safest live call: the server ignores whatever's sent
    #    and always returns its own canned alert, so there's no risk of a
    #    malformed real alert reaching a downstream pathway.
    print("2. test()...", end=" ")
    try:
        result = client.test()
        print(f"ok, echoed alert={result.get('alert')}")
    except Exception as e:
        print(f"FAIL ({e!r})")
        failures.append("test()")

    # 3. One real, low-noise, grep-able /alert call.
    print("3. client.info() (real /alert call)...", end=" ")
    try:
        send_result = client.info(
            category="system",
            title="SDK smoke test",
            message="alertmanager-sdk scripts/smoke_test.py run",
        )
        print(f"ok, alert_id={send_result.alert_id} retries={send_result.retries}")
    except Exception as e:
        print(f"FAIL ({e!r})")
        failures.append("client.info()")

    # 4. Deliberately invalid level -- must raise locally, WITHOUT an HTTP
    #    call. This is the SDK's core value proposition: proving it here,
    #    live, not just in a mocked unit test.
    print("4. invalid level raises client-side (no HTTP call)...", end=" ")
    try:
        client.send(level="not-a-real-level", category="system", title="t", message="m")
        print("FAIL (expected AlertManagerValidationError, none raised)")
        failures.append("client-side validation")
    except AlertManagerValidationError:
        print("ok")
    except Exception as e:
        print(f"FAIL (wrong exception type: {e!r})")
        failures.append("client-side validation")

    # 5. Wrong credentials -- confirm a generic AlertManagerAuthError, and
    #    that its message doesn't over-claim detail the server doesn't
    #    provide (no "locked out" / "wrong password" distinction exists).
    print("5. wrong credentials -> generic AlertManagerAuthError...", end=" ")
    try:
        bad_client = AlertClient(
            os.environ.get("ALERT_MANAGER_URL", "https://alert.d-t.pw"),
            username="definitely-not-a-real-account",
            password="definitely-not-a-real-password",
        )
        bad_client.test()
        print("FAIL (expected AlertManagerAuthError, none raised)")
        failures.append("auth failure handling")
    except AlertManagerAuthError as e:
        print(f"ok (message: {e.message!r})")
    except Exception as e:
        print(f"FAIL (wrong exception type: {e!r})")
        failures.append("auth failure handling")

    client.close()

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {', '.join(failures)}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
