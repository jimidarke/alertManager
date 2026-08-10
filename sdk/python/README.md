# Alert Manager SDK (Python)

A Python client for the [Alert Manager](../../README.md) HTTP gateway. Evolves the raw `requests.post(...)` snippet from [`docs/CLIENTNODE.md`](../../docs/CLIENTNODE.md) into a client with typed exceptions, a tested retry policy, and client-side payload validation.

## Why the validation matters

`POST /alert` only validates that the required fields are *present* — it does not check that `level`/`category` are valid values, that `timestamp` is really an integer, or that `source` is a string. An invalid `level` gets an immediate HTTP 200 back, and is only actually checked later, asynchronously, on the internal MQTT-consumer side. If it fails there, it's silently dropped — no error ever reaches the original caller.

This SDK runs the same validation the server eventually runs, *before* making the network call, so a mistake shows up as an immediate, local, typed exception instead of a silently vanished alert.

## Install

```bash
pip install -e "sdk/python[dev]"   # from a checkout of this repo
```

## Quick start

```python
from alertmanager_sdk import AlertClient

client = AlertClient(
    "https://alert.d-t.pw",
    username="my-service",
    password="...",
    default_source="my-service",
)

client.critical(
    category="security",
    title="Unauthorized access detected",
    message="...",
)
```

## Automatic exception reporting (optional)

```python
from alertmanager_sdk import AlertClient, install_exception_hooks

client = AlertClient("https://alert.d-t.pw", token="amt_...", default_source="my-service")
install_exception_hooks(client)  # any uncaught exception, main thread or background thread, gets reported

main()
```

This is deliberately the *only* automatic reporting path — there's no `logging.Handler` integration, to avoid double-reporting the same failure through two channels. Anything you already know about (not an exception) should be an explicit `client.send()`/`client.critical()`/etc. call.

See `examples/` for more, and [`docs/CLIENTNODE.md`](../../docs/CLIENTNODE.md) in the main repo for the full wire-level API reference this SDK wraps.

## Exceptions

Every exception raised by this SDK derives from `AlertManagerError`. See `src/alertmanager_sdk/exceptions.py` for the full hierarchy and, importantly, its documented limits: the gateway cannot distinguish a wrong password from a locked-out account from a disabled account (they're all a generic `AlertManagerAuthError`), and a `403`'s body carries no detail about which permission was missing (`AlertManagerForbiddenError.attempted_client` reflects only what the SDK itself sent, never anything the server reported).

## Testing

```bash
pip install -e "sdk/python[dev]"
pytest sdk/python/tests -v
```

No live Postgres/gateway is available in a typical dev sandbox — the test suite is fully self-contained (mocks `requests` directly, no live network). For an end-to-end check against a real gateway, see `scripts/smoke_test.py`.
