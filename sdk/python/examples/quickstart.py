"""
Direct evolution of docs/CLIENTNODE.md's own Python Quick Start snippet --
same base URL, same Basic-auth model, same payload shape, but with
validation and typed exceptions instead of a bare `requests.post()` call.

Run with real credentials (never commit them):
    ALERT_MANAGER_URL=https://alert.d-t.pw \
    ALERT_MANAGER_USERNAME=my-service \
    ALERT_MANAGER_PASSWORD=... \
    python3 examples/quickstart.py
"""

import os

from alertmanager_sdk import AlertClient, AlertManagerError


def main() -> None:
    client = AlertClient(
        os.environ.get("ALERT_MANAGER_URL", "https://alert.d-t.pw"),
        username=os.environ["ALERT_MANAGER_USERNAME"],
        password=os.environ["ALERT_MANAGER_PASSWORD"],
        default_source="quickstart-example",
    )

    try:
        result = client.info(
            category="application",
            title="Status",
            message="Task completed",
        )
        print(f"sent: alert_id={result.alert_id} retries={result.retries}")
    except AlertManagerError as e:
        print(f"failed to send: {e}")
        raise
    finally:
        client.close()


if __name__ == "__main__":
    main()
