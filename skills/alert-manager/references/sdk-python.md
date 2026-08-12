# Python SDK

Read this before writing any non-trivial Python sender. The SDK runs the
same validation the server eventually runs, but does it client-side, before
the network call — closing the "HTTP 200 but silently dropped" gap
described in `SKILL.md`'s gotcha 1.

## Install and upgrade

```bash
pip install "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
```

Pin a tag instead of floating on `main` for anything you want reproducible:

```bash
pip install "git+https://github.com/jimidarke/alertManager.git@v3.1.2#subdirectory=sdk/python"
```

Upgrade later with:

```bash
pip install --upgrade --force-reinstall "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
```

Install into a virtualenv, not globally — this is a general good practice
and specifically matters here since the package isn't on PyPI, so a global
install is easy to lose track of.

**Working from a full checkout of the main repo?** `pip install -e "sdk/python[dev]"`
gives you an editable install plus `sdk/python/scripts/smoke_test.py` and
`sdk/python/examples/quickstart.py` — neither of those ships via the pip-from-git
install above (`pyproject.toml` only packages `src/alertmanager_sdk`). If
you installed via pip and want a quick connectivity/send check with no repo
checkout, use this skill's own `scripts/send-alert.sh` instead.

## Quickstart

```python
from alertmanager_sdk import AlertClient

client = AlertClient(
    "https://alert.d-t.pw",
    username="my-service",
    password="...",       # or: token="amt_..."
    default_source="my-service",
)

client.critical(category="security", title="Unauthorized access detected", message="...")
```

## Method surface

- `AlertClient(base_url, *, username=None, password=None, token=None, default_source=None, ...)`
  — construct once, reuse. Use `username`+`password` OR `token`, not both.
- `client.send(level=, category=, title=, message=, source=None, client=None, metadata=None, routing=None, display_config=None)`
  — general-purpose send; `source` falls back to `default_source` if omitted.
- `client.critical(...)` / `.warning(...)` / `.notice(...)` / `.info(...)` /
  `.debug(...)` — shorthands for `send()` with `level` pre-filled.
- `client.test()` — hits `POST /test`, no `alerts:write` scope required.
- `client.health()` — hits `GET /health`, returns a parsed `HealthStatus`
  (note: `db_ok=False` while `healthy=True` is a real, expected combination
  — the server's own database check is fail-open and doesn't affect overall
  health, so don't treat it as a contradiction).
- `client.close()` — also usable as a context manager (`with AlertClient(...) as client:`).

All of `send()`/the level shorthands return a `SendResult` on success; any
non-success outcome (client-side validation failure, non-2xx response,
connection error) **raises** instead of returning a result you could
forget to check.

## Exception hierarchy

Every exception this SDK raises derives from `AlertManagerError`.

| Exception | Meaning | Notes |
|---|---|---|
| `AlertManagerValidationError` | Client-side validation failed before any network call | Also a `ValueError`, so `except ValueError` catches it too |
| `AlertManagerConnectionError` | Request never completed (timeout, DNS failure, connection refused) | **Never auto-retried** — ambiguous whether the server already processed a non-idempotent POST; see `SKILL.md` gotcha 5 |
| `AlertManagerBadRequestError` | HTTP `400` | Should be rare if client-side validation ran first |
| `AlertManagerAuthError` | HTTP `401` | The server genuinely cannot distinguish wrong password / locked account / disabled account / nonexistent user in this response — don't try to branch on a more specific cause, there isn't one available |
| `AlertManagerForbiddenError` | HTTP `403` | The response body carries **zero** detail about which client/permission was missing; `.attempted_client` reflects only what the SDK itself sent, never anything server-reported |
| `AlertManagerServiceUnavailableError` | HTTP `503` | Only possible from `POST /alert` — a "retry later" condition, not a permanent failure |
| `AlertManagerServerError` | HTTP `500` | Broker publish failed, or an unexpected server exception — not auto-retried beyond the SDK's own small internal retry budget |

## Automatic exception reporting

```python
from alertmanager_sdk import AlertClient, install_exception_hooks

client = AlertClient("https://alert.d-t.pw", token="amt_...", default_source="my-service")
install_exception_hooks(client)  # any uncaught exception, main thread or background thread, gets reported

main()
```

This is deliberately the **only** automatic reporting path built into the
SDK. **Don't also wire up a `logging.Handler`** that forwards every `ERROR`
log line to the Alert Manager — that's a real anti-pattern here, not just a
style preference: it double-reports the same failure through two channels
(the exception hook and the log handler both fire for the same crash), and
during an actual incident it turns "one alert" into an alert storm as every
downstream ERROR log line generates its own alert. Anything you already
know about and are explicitly handling (i.e. not an uncaught exception)
should be an explicit `client.send()`/`client.critical()`/etc. call instead.
