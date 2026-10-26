# Alert Manager Client Integration Reference

**Version:** 3.4.1
**Last Updated:** 2026-10-26

How to send alerts to the Alert Manager from your own systems. This is the complete payload
contract. A running gateway serves it unauthenticated at `GET /docs`; it is also on
[GitHub](https://github.com/jimidarke/alertManager/blob/main/docs/CLIENTNODE.md).

**The three things that most often go wrong:**

1. **`HTTP 200` does not mean your alert was accepted.** The gateway only checks that the
   required fields are present. Bad values are rejected later and dropped silently, so validate
   before you send. See [Validation](#validation).
2. **`timestamp` is a Unix epoch integer in seconds.** Not ISO-8601, not a float, not
   milliseconds (`Date.now()` is wrong; divide by 1000).
3. **`client` is authorization-checked.** Sending a client your account has no scope for returns
   `403`. Usually you should omit it. See [The `client` field](#the-client-field).

For Python, use the [SDK](#python--sdk-recommended): it validates client-side before sending.

---

## Endpoint & authentication

Base URL: `https://alert.d-t.pw` (HTTPS only).

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| `POST` | `/alert` | Basic or Bearer | Submit an alert |
| `POST` | `/test` | Basic or Bearer | Connectivity check: ignores your body and sends a sample `info` alert |
| `GET` | `/api/whoami` | Basic or Bearer | Confirm a credential works |
| `GET` | `/health` | None | Liveness |
| `GET` | `/docs` | None | This document |

Authenticate with HTTP Basic (`-u user:pass`) or a Bearer token (`Authorization: Bearer amt_...`).
Credentials come from the service operator. Bearer tokens are shown once at creation and can't
be recovered. Five failed logins in a row lock the account for 15 minutes.

---

## Payload

### Required fields

| Field | Type | Rules |
|-------|------|-------|
| `timestamp` | integer | Unix epoch **seconds** |
| `level` | string | `critical`, `warning`, `notice`, `info`, `debug` |
| `category` | string | See [Categories](#categories) |
| `title` | string | Short summary |
| `message` | string | Detail. Multiline is fine |
| `source` | string | Stable identifier for the sending system, e.g. `web-server-01`. Must be a string, not an object |

### Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `client` | string | Which tenant the alert belongs to. Usually omit — see [The `client` field](#the-client-field) |
| `metadata` | object | Any machine-readable key-value data |
| `routing` | object | Force specific destinations — see [Routing override](#routing-override) |
| `pathway_config` | object | Per-destination options, mainly LED signs — see [LED signs](#led-signs) |

### Levels

| Level | Use for |
|-------|---------|
| `critical` | Needs immediate attention: outage, security breach, data loss. Usually emails someone — use sparingly |
| `warning` | May need investigation: high resource use, failed attempts, degraded service |
| `notice` | Normal but significant: job completed, reboot |
| `info` | Routine status |
| `debug` | Troubleshooting detail |

Aliases (`error`/`err`/`crit` → `critical`, `warn` → `warning`, `inf` → `info`, `dbg` → `debug`)
and stray case/whitespace are tolerated, but **send the canonical lowercase value**. Non-canonical
values are delivered but don't appear on the dashboard.

### Categories

| Category | Use for |
|----------|---------|
| `security` | Authentication, authorization, intrusion |
| `system` | OS and infrastructure |
| `application` | Application errors, status, performance |
| `network` | Connectivity, devices, traffic |
| `hardware` | Device status, battery, disk health, sensors |
| `business` | Audits, imports, config changes, CRUD events |
| `automation` | Home/building automation |
| `environmental` | Temperature, humidity, air quality |
| `weather` | Weather alerts |
| `news` | News and information feeds |
| `personal` | Personal reminders |

### Example

```json
{
  "timestamp": 1729213847,
  "level": "critical",
  "category": "application",
  "title": "Database Connection Failed",
  "message": "Unable to connect to primary database, switching to replica",
  "source": "api-server-03",
  "metadata": {"error_code": "ECONNREFUSED", "retry_count": 3}
}
```

---

## The `client` field

`client` decides which tenant's log and dashboard the alert lands in, and whether you may send
it at all. The effective client is:

1. the payload's `client` (blank counts as absent), else
2. your account's default client, else
3. none.

Your account needs an `alerts:write` scope for the effective client, or you get `403`. The
client must also be registered with the service, or you get `400 Unknown client`. Sending with
no client at all requires an admin account.

**Omit `client`** and your account's default applies. Set it only if you send on behalf of
several tenants, and ask the operator to register and grant each one.

---

## Validation

Validation happens in two places:

- **The gateway, before it responds.** It checks auth, the client, `Content-Type:
  application/json`, valid non-empty JSON, and that the six required fields are present.
- **Downstream, after you've already received `200`.** It checks types and the
  level/category values. Failures are **dropped silently**: no error and no retry.

| Payload | Response | Result |
|---------|----------|--------|
| valid | `200` | delivered |
| `"level": "bogus"` or `"category": "test"` | `200` | **dropped** |
| `"timestamp": "2024-01-15T10:30:00Z"` or `1732982400.123` | `200` | **dropped** |
| `"source": {"name": "x"}` | `200` | **dropped** |
| `"timestamp": 1732982400123` (milliseconds) | `200` | delivered with a far-future date, not on the dashboard |

So check `level`, `category`, and `timestamp` yourself before sending (the SDK does this for
you). If alerts "succeed" but never show up, this is almost always why.

---

## Responses

| Code | Meaning | Retry? |
|------|---------|--------|
| `200` | Accepted for routing — not a delivery confirmation | — |
| `400` | Bad request: wrong `Content-Type`, invalid or empty JSON (including `{}`), missing required field, or `Unknown client` | No |
| `401` | Bad or missing credentials, or account locked | No |
| `403` | No `alerts:write` scope for the effective client | No |
| `500` | Server failed to queue the alert | Yes, with backoff |
| `503` | Credential check temporarily unavailable | Yes, shortly |

Client errors carry an `error` key; success and server errors carry `status`. Example `200`:

```json
{"status": "success", "message": "Alert forwarded to MQTT broker", "alert_id": "api-server-03-1729213847", "timestamp": "2026-10-26T10:30:00Z"}
```

Don't let alerting failures break the caller: use a short timeout (~10 s) and swallow or log
errors rather than raising them into your application's own error path.

---

## Sending

### cURL

```bash
curl -X POST https://alert.d-t.pw/alert \
  -u username:password \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'$(date +%s)',"level":"warning","category":"system","title":"Disk low","message":"/var at 92%","source":"web-01"}'
```

With a token, replace `-u ...` with `-H "Authorization: Bearer amt_..."`.

### Python — SDK (recommended)

```bash
pip install "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
# update: add --upgrade --force-reinstall
```

```python
from alertmanager_sdk import AlertClient

client = AlertClient("https://alert.d-t.pw", username="my-app", password="...",
                     default_source="my-app")
client.send(level="info", category="application", title="Status", message="Task completed")
```

The SDK validates before sending and raises typed exceptions. Fields without a dedicated
parameter pass through as keyword arguments (`routing=...`, `pathway_config=...`). See
[`sdk/python/`](../sdk/python/) for exception hooks and the full API.

### Python — raw

```python
import requests, time

requests.post(
    "https://alert.d-t.pw/alert",
    auth=("username", "password"),
    json={
        "timestamp": int(time.time()),
        "level": "info",
        "category": "application",
        "title": "Status",
        "message": "Task completed",
        "source": "my-app",
    },
    timeout=10,
).raise_for_status()
```

### Ready-made integrations

- **Linux host/Docker health:** [`clients/linux-agent/`](../clients/linux-agent/) is a bash
  agent for a cron or systemd timer. It reports host telemetry and container state, and alerts
  on disk, load, memory, and kernel errors. Use it instead of writing your own.
- **LLM coding agents:** [`skills/alert-manager/`](../skills/alert-manager/) is a Claude Code
  skill that bundles this document, the SDK, and the common pitfalls. Install it with
  `skills/alert-manager/install.sh`.

---

## Routing override

The operator's rules choose where an alert goes (log file, email, LED sign), mostly by `level`
and `category`. To force specific destinations instead:

```json
"routing": {"override": true, "pathways": ["file_log", "email_critical"]}
```

This replaces the rules entirely, and an empty `pathways` list sends the alert nowhere.
Destination names are specific to each deployment, so ask the operator. Only use this when you
have a reason to. There is no `routing.exclude`.

---

## LED signs

Optional. By default the server picks which sign zones show an alert and how it looks. To
override, use `pathway_config` keyed by the LED destination's name (`led_sign` in the
reference setup):

```json
"pathway_config": {
  "led_sign": {
    "zones": ["kitchen"],
    "display_config": {"mode": "flash", "color": "amber", "speed": 3, "priority": true, "duration": 30}
  }
}
```

This only takes effect if the alert is actually routed to that sign. `metadata.led_zones` and a
top-level `display_config` are **not** read. Unknown values fall back to the defaults.

| `display_config` field | Values |
|------------------------|--------|
| `mode` | `rotate`, `hold`, `flash`, `scroll`, `auto`, `rollup`, `rolldown`, `rollleft`, `rollright`, `rollin`, `rollout`, `wipeup`, `wipedown`, `wipeleft`, `wiperight`, `wipein`, `wipeout`, `compressedrotate`, `explode`, `clock`, `special`, `newsflash` |
| `special_effect` | `twinkle`, `sparkle`, `snow`, `interlock`, `switch`, `slide`, `spray`, `starburst`, `welcome`, `slots`, `newsflash`, `trumpet`, `cyclecolors`, `thankyou`, `nosmoke`, `nodrive`, `fishimal`, `fireworks`, `turballoon`, `bomb` |
| `color` | `red`, `green`, `amber`, `dimred`, `dimgreen`, `brown`, `orange`, `yellow`, `rainbow1`, `rainbow2`, `colormix`, `autocolor` |
| `character_set` | `5high`, `5stroke`, `7high`, `7stroke`, `7highfancy`, `10high`, `7shadow`, `fhighfancy`, `fhigh`, `7shadowfancy`, `5wide`, `7wide`, `7widefancy`, `5widestroke` |
| `position` | `topline`, `midline`, `botline`, `fill`, `left`, `right` |
| `speed` | `1`–`5` (slowest to fastest) |
| `priority` | boolean: interrupt the normal rotation |
| `duration` | seconds, for priority messages |

---

`POST /alert` is the only way to submit alerts. MQTT exists only for LED sign hardware, and
SNMP and syslog ingestion are discontinued. Ignore any old example that uses them.
