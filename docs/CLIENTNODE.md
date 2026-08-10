# Alert Manager Client Integration Reference

**Version:** 3.1.1
**Last Updated:** 2026-08-09

Wire-level reference for **sending alerts** to the Alert Manager. Audience: senior developers
and LLM code assistants building a sender. This page is the authoritative payload contract.

**The three things that most often go wrong:**

1. `HTTP 200` does **not** mean your alert was accepted. The gateway checks only that the six
   required fields are *present*; invalid values are rejected later, asynchronously, and dropped
   silently. See [Validation happens in two stages](#validation-happens-in-two-stages).
2. `timestamp` is a Unix epoch **integer**. An ISO-8601 string or a float is dropped downstream.
3. The `client` field is authorization-checked. Sending a client your account has no scope for
   is a `403`. See [The `client` field](#the-client-field).

For Python, use the SDK in [`sdk/python/`](../sdk/python/) — it validates `level`/`category`/
`timestamp` client-side *before* the network call, which is exactly the gap described in (1).

---

## Endpoint & authentication

HTTPS is the only alert-ingestion path. Everything goes through `nginx-proxy` at
`https://alert.d-t.pw` (port 443, TLS 1.2+); the gateway container publishes no host port, so
`http://<host>:8080` is not reachable.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/alert` | Basic or Bearer | **Submit an alert.** Requires an `alerts:write` scope for the effective client |
| `POST` | `/test` | Basic or Bearer | Connectivity check. Ignores your request body, generates its own sample alert, routes it for real. No `alerts:write` needed |
| `GET` | `/health` | None | Liveness + `db`/`mqtt` status detail |
| `GET` | `/api/whoami` | Basic, Bearer, or session | Identity introspection — use after rotating a credential to confirm it works |
| `GET` | `/api/logs/list` \| `/tail` \| `/chunk` | Basic, Bearer, or session | Read back log files you have `logs:read` scope for |

Authenticate with **HTTP Basic** (`-u user:pass`) or a **Bearer token**
(`Authorization: Bearer amt_...`). Never a session cookie — those are for browser pages
(`/login`, `/dashboard`, `/admin/*`), which are out of scope here. See `docs/HTTP_GATEWAY.md`
for the full endpoint list and `docs/AUTH.md` for the auth model.

Credentials are issued by an operator (`tools/user-manager.py`, or the `master_admin` web UI).
Bearer tokens are shown exactly once at creation and are unrecoverable afterwards. After any
rotation, confirm with `GET /api/whoami` before considering the change complete.

---

## Payload schema

### Required fields

All six must be **present** or the gateway returns `400`.

| Field | Type | Constraints |
|-------|------|-------------|
| `timestamp` | integer | Unix epoch **seconds**. No decimals, not ISO-8601 |
| `level` | string | `critical`, `warning`, `notice`, `info`, `debug` |
| `category` | string | `security`, `system`, `weather`, `automation`, `personal`, `network`, `application`, `news`, `business`, `hardware`, `environmental` |
| `title` | string | Short title |
| `message` | string | Detail. Multiline allowed — see [Message sanitization](#message-sanitization-file-logs-only) |
| `source` | string | Source identifier. Must be a string, not an object |

### Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `client` | string | Log-routing key **and** authorization subject — see [The `client` field](#the-client-field) |
| `metadata` | object | Arbitrary key-value data. `metadata.led_zones` (array) targets LED sign zones |
| `routing` | object | `{"override": true, "pathways": [...]}` — bypass configured routing rules |
| `display_config` | object | LED sign rendering — see [LED display config](#led-display-config) |

### Level aliases

Applied after trim + lowercase. `notice` has no alias.

| Canonical | Accepted aliases |
|-----------|------------------|
| `critical` | `error`, `err`, `crit` |
| `warning` | `warn` |
| `info` | `inf` |
| `debug` | `dbg` |

### Normalization

`level`, `category`, and `client` are whitespace-trimmed and lowercased before use; `level`
additionally resolves aliases. `" ERROR "` → `critical`. `"  PROD-Web  "` → `prod-web`.

---

## The `client` field

`client` determines **where the alert is logged** (`/data/logs/file/{client}/{date}.log`) and
**whether you are allowed to send it at all**. It is not a free-text tag.

### Resolution order

The *effective client* is resolved server-side, in this order:

1. The payload's `client`, after stripping whitespace — a whitespace-only value like `"   "`
   counts as absent and falls through
2. else the authenticated account's configured `default_client`
3. else clientless (`""`)

The value that gets authorized is the same value that gets published — the gateway does not
recompute it after the check.

### Authorization

- You must hold an `alerts:write` scope covering the effective client (or a `('*', …)` wildcard
  scope). Otherwise: **`403`**.
- The clientless case (`""`) additionally requires the **`master_admin`** role. A regular account
  cannot send a fully clientless alert, even with a wildcard scope.

Practically: omit `client` and let your account's `default_client` apply, unless you are a
multi-tenant sender that legitimately writes for several clients — in which case set it
explicitly and have the operator grant a scope per client.

A `403` here means a missing scope, not a bad password. Ask the operator to run
`user-manager.py add-scope <role> <client> alerts:write`.

---

## Validation happens in two stages

This is the highest-value thing to understand, because the two stages disagree and only the
first one talks back to you.

**Stage 1 — the gateway (`POST /alert`), synchronous.** Checks `Content-Type: application/json`,
that the body parses as JSON, that it is non-empty, and that the six required field *names* are
present. It does **not** check types or enum values. Then it authorizes the client, publishes to
the internal broker, and returns `200`.

**Stage 2 — the alert router, asynchronous, after you already got your `200`.** Enforces the
real schema: types, level/category enums, aliases, normalization. A failure here raises, is
logged to `data/logs/ops/alert-router.{date}.log`, increments an error counter, and the alert is
**dropped**. No retry, no callback, no trace in your HTTP response.

Verified behaviour:

| Payload | Gateway | Router | Net result |
|---------|---------|--------|------------|
| fully valid | `200 success` | accept | delivered |
| `"level": " ERROR "` | `200 success` | accept → normalized to `critical` | delivered |
| `"level": "bogus"` | `200 success` | **reject** | **silently dropped** |
| `"category": "test"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": "2024-01-15T10:30:00Z"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": 1732982400.123` | `200 success` | **reject** | **silently dropped** |
| `"source": {"name": "x"}` | `200 success` | **reject** | **silently dropped** |

**Implication for senders:** validate `level`, `category`, and `timestamp` client-side. Do not
treat `200` as delivery confirmation. Either use the Python SDK (which does this for you) or
hard-code the enum lists above. If alerts are "sent successfully" but never appear in logs,
stage 2 is where they died — check the ops log.

---

## Responses

Success and server errors are keyed on `status`; client errors are keyed on `error`. Handle both.

| Code | Condition | Body |
|------|-----------|------|
| `200` | Accepted for routing (**not** delivery-confirmed) | `{"status":"success","message":"Alert forwarded to MQTT broker","alert_id":"<source>-<epoch>","timestamp":"<ISO>"}` |
| `400` | Wrong `Content-Type` | `{"error":"Content-Type must be application/json","received_content_type":…}` |
| `400` | Body is not valid JSON | `{"error":"Invalid JSON format","detail":…,"json_error":…,"hint":…}` |
| `400` | Empty body, **including `{}`** | `{"error":"Empty JSON payload","detail":"Request body cannot be empty"}` |
| `400` | Missing required field(s) | `{"error":"Missing required fields: …","required_fields":[…]}` |
| `401` | Bad credentials, or account locked (5 failed logins → 900 s) | `{"status":"error","error":"authentication required"}` |
| `403` | No `alerts:write` scope for the effective client, or clientless without `master_admin` | `{"status":"error","error":"forbidden"}` |
| `500` | Broker publish failed | `{"status":"error","message":"Failed to forward alert to MQTT broker"}` |

`{}` returns "Empty JSON payload" rather than "missing required fields" — an empty dict is falsy,
so that check fires first. Retry `500` (broker-side, transient); do not retry `400`/`403`.

---

## Examples

### cURL

```bash
curl -X POST https://alert.d-t.pw/alert \
  -u username:password \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'$(date +%s)',"level":"warning","category":"system","title":"Alert","message":"Test","source":"curl"}'
```

### Python — SDK (recommended)

**Install** — the SDK lives in this repository under [`sdk/python/`](../sdk/python/) and can be installed straight from GitHub:

```bash
pip install "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
```

**Check for updates** — the repo is public at <https://github.com/jimidarke/alertManager>; releases are tagged `vX.Y.Z` and SDK-specific changes are listed in [`sdk/python/CHANGELOG.md`](../sdk/python/CHANGELOG.md). To pick up the latest version:

```bash
pip install --upgrade --force-reinstall "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
```

(Or clone the repo and `pip install -e sdk/python` for a checkout you can `git pull`.)

```python
from alertmanager_sdk import AlertClient

client = AlertClient("https://alert.d-t.pw", username="my-app", password="...",
                     default_source="my-app")
client.send(level="info", category="application", title="Status", message="Task completed")
```

### Python — raw

```python
import requests, time

requests.post(
    "https://alert.d-t.pw/alert",
    auth=("username", "password"),
    json={
        "timestamp": int(time.time()),   # epoch seconds, integer
        "level": "info",
        "category": "application",
        "title": "Status",
        "message": "Task completed",
        "source": "my-app",
    },
    timeout=10,
).raise_for_status()
```

### Bearer token instead of Basic

```bash
curl -X POST https://alert.d-t.pw/alert \
  -H "Authorization: Bearer amt_..." \
  -H "Content-Type: application/json" \
  -d '{...}'
```

### Routing override

```json
{
  "timestamp": 1732982400,
  "level": "critical",
  "category": "security",
  "title": "Security Alert",
  "message": "Unauthorized access detected",
  "source": "security-monitor",
  "client": "prod-security",
  "routing": {"override": true, "pathways": ["email_critical", "led_sign"]}
}
```

`routing.override: true` **replaces** the configured rules outright — the listed `pathways` are
used verbatim and no rule is consulted. An override with an empty or missing `pathways` list
routes the alert nowhere. Without an override, rules are evaluated in order and the first match
wins; if nothing matches, the alert is delivered nowhere.

`routing.exclude` is **not implemented** — only `override` and `pathways` are read. Sending it
has no effect.

---

## What happens to your alert

| Pathway | Destination |
|---------|-------------|
| `file` | `/data/logs/file/{client}/{date}.log`, date-rotated |
| `database` | PostgreSQL, weekly partitions (separate consumer service, not a router pathway) |
| `email` | O365 Graph API, HTML templates, optional storm aggregation |
| `led_sign` | BetaBrite signs over MQTT, per zone |
| `console` | stdout |

### Message sanitization (file logs only)

File pathways collapse alerts to one line: `\r\n`/`\r`/`\n` → ` ↵ `, tabs → space, runs of
spaces collapsed, and `message` truncated at 500 chars with `...`. Email, database, and console
keep the full multiline text. This affects only how your message *renders* in file logs — send
multiline freely.

```
1735577400 [critical] [application] source: Title - SQL Error: ↵ Column 'date' cannot be null ↵ Retrying...
```

### LED display config

Only relevant if the alert routes to an LED sign. Target zones with `metadata.led_zones`.

| Field | Values |
|-------|--------|
| `mode` | `rotate`, `hold`, `flash`, `scroll`, `rollup`, `rolldown`, `rollright`, `rollleft`, `explode`, `newsflash`, `welcome`, `compressed` |
| `color` | `red`, `green`, `amber`, `rainbow1`, `rainbow2`, `autocolor`, `color_mix` |
| `special_effect` | `twinkle`, `sparkle`, `snow`, `starburst`, `bomb`, `welcome`, `cyclecolors` |
| `character_set` | `5high`, `7high`, `10high`, `7shadow`, `7highfancy`, `7widestroke` |
| `speed` | `1`–`5` (slowest to fastest) |
| `priority` | boolean — interrupt normal rotation |
| `duration` | seconds, for priority messages |

---

## Not an ingestion path

**MQTT** (`alert.d-t.pw:42690`) is a device bus for ESP32 BetaBrite LED signs only — display push
and telemetry. The internal `alerts/input` topic is service-to-service plumbing and is not ACL'd
for external publish, so you cannot submit alerts over it. See `docs/ESP32_BETABRITE_IMPLEMENTATION.md`
if you are building a sign, not a sender.

**SNMPv3** trap ingestion is deprecated and unsupported. See `CLAUDE.md` for the historical reference.

---

## Related documentation

| Document | Content |
|----------|---------|
| `CLAUDE.md` | Authoritative system-wide technical reference |
| `HTTP_GATEWAY.md` | Full HTTP endpoint reference |
| `AUTH.md` | Auth model: sessions, RBAC roles/scopes, tokens, lockout |
| `ALERT_PAYLOAD_SPEC.md` | Extended payload specification |
