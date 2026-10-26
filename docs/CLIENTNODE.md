# Alert Manager Client Integration Reference

**Version:** 3.4.1
**Last Updated:** 2026-10-26

Wire-level reference for **sending alerts** to the Alert Manager. Audience: senior developers
and LLM code assistants building a sender. This page is the single, authoritative payload
contract — there is no other payload spec. It is public: a running gateway serves it
unauthenticated at `GET /docs`, and it is also on GitHub at
<https://github.com/jimidarke/alertManager/blob/main/docs/CLIENTNODE.md>.

**The three things that most often go wrong:**

1. `HTTP 200` does **not** mean your alert was accepted. The gateway checks only that the six
   required fields are *present*; invalid values are rejected later, asynchronously, and dropped
   silently. See [Validation happens in two stages](#validation-happens-in-two-stages).
2. `timestamp` is a Unix epoch **integer in seconds**. An ISO-8601 string or a float is dropped
   downstream; a millisecond value is routed with a date tens of thousands of years in the future
   and never reaches the database. See [The `timestamp` field](#the-timestamp-field).
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
| `POST` | `/test` | Basic or Bearer | Connectivity check. Ignores your request body, generates its own sample alert (`info`/`system`, source `http-gateway-test`), routes it for real. No `alerts:write` needed |
| `GET` | `/health` | None | Liveness + `db`/`mqtt` status detail |
| `GET` | `/docs` | None | This document, as `text/markdown` |
| `GET` | `/api/whoami` | Basic, Bearer, or session | Identity introspection — use after rotating a credential to confirm it works |
| `GET` | `/api/logs/list` \| `/tail` \| `/chunk` | Basic, Bearer, or session | Read back log files you have `logs:read` scope for |

Authenticate with **HTTP Basic** (`-u user:pass`) or a **Bearer token**
(`Authorization: Bearer amt_...`). Never a session cookie — those are for browser pages
(`/login`, `/dashboard`, `/admin/*`), which are out of scope here. See `docs/HTTP_GATEWAY.md`
for the full endpoint list and `docs/AUTH.md` for the auth model.

Credentials are issued by an operator (`tools/user-manager.py`, or the `master_admin` web UI).
Bearer tokens are shown exactly once at creation and are unrecoverable afterwards. After any
rotation, confirm with `GET /api/whoami` before considering the change complete. Five
consecutive failed logins lock an account for 900 seconds (Basic auth counts).

---

## Payload schema

### Required fields

All six must be **present** or the gateway returns `400`.

| Field | Type | Constraints |
|-------|------|-------------|
| `timestamp` | integer | Unix epoch **seconds**. No decimals, not ISO-8601, not milliseconds |
| `level` | string | `critical`, `warning`, `notice`, `info`, `debug` (aliases below) |
| `category` | string | `security`, `system`, `weather`, `automation`, `personal`, `network`, `application`, `news`, `business`, `hardware`, `environmental` |
| `title` | string | Short title (keep it under ~100 chars) |
| `message` | string | Detail. Multiline allowed — see [Message sanitization](#message-sanitization-file-logs-only) |
| `source` | string | Source identifier (system/device/service name). Must be a string, not an object |

`title` and `message` are not type-checked anywhere — send strings anyway.

### Optional fields

| Field | Type | Description |
|-------|------|-------------|
| `client` | string | Log-routing key **and** authorization subject — see [The `client` field](#the-client-field) |
| `metadata` | object | Arbitrary key-value data. Stored and shown in logs/email; routing rules may match on it |
| `routing` | object | `{"override": true, "pathways": [...]}` — bypass configured routing rules. See [Routing](#routing) |
| `pathway_config` | object | Per-pathway overrides keyed by pathway name — see [Per-pathway overrides](#per-pathway-overrides-pathway_config) |
| `display_config` | object | **Stored only** (database column). Not read by the LED sign pathway — use `pathway_config` instead |

Any other top-level keys are passed through and stored, but nothing acts on them. Two keys are
overwritten by the gateway: `client` (set to the normalized effective client) and `_gateway`
(see [Gateway-added fields](#gateway-added-fields)).

### Levels

| Level | Aliases | Use for |
|-------|---------|---------|
| `critical` | `error`, `err`, `crit` | Emergency conditions requiring immediate attention — system down, security breach, data loss. Use sparingly |
| `warning` | `warn` | Conditions that may need investigation — high resource usage, failed attempts, degraded performance |
| `notice` | *(none)* | Normal but significant events — scheduled task completed, reboot, successful operation |
| `info` | `inf` | General informational messages — status updates, routine operations |
| `debug` | `dbg` | Troubleshooting detail |

### Categories

| Category | Use for |
|----------|---------|
| `security` | Authentication, authorization, intrusion detection, access control |
| `system` | Boot, shutdown, OS and infrastructure events |
| `weather` | Weather alerts |
| `automation` | Home/building automation |
| `personal` | Personal reminders and notifications |
| `network` | Connectivity, devices, traffic |
| `application` | Application errors, status, performance |
| `news` | News and information feeds |
| `business` | Business operations — audits, imports, config changes, CRUD events |
| `hardware` | Physical infrastructure — device status, battery, disk health, sensors |
| `environmental` | Temperature, humidity, air quality, CO2 |

### Normalization

`level`, `category`, and `client` are whitespace-trimmed and lowercased before use; `level`
additionally resolves aliases. `" ERROR "` → `critical`. `" SYSTEM "` → `system`.
`"  PROD-Web  "` → `prod-web`.

**Send canonical values anyway.** Routing normalizes, but the database store (which feeds the
dashboard) records the payload as you sent it and only accepts the canonical lowercase
`level`/`category` values. An alert sent as `"level": "warn"` or `" ERROR "` is routed and
logged normally but never appears in the database or on the dashboard.

### The `timestamp` field

Routing checks only "is it an integer"; there is no range check at the gateway or router. The
database store additionally requires a time between 2020-01-01 and one day in the future. So:

- **Wrong type** (string, float): rejected in stage 2, silently dropped.
- **Wrong unit** (milliseconds): `1732982400123` is a valid integer, so it is routed to file,
  email, and LED with a date tens of thousands of years from now, and the database rejects it
  (it never shows on the dashboard). Nothing reports an error to you. This is the easiest
  mistake when porting from JavaScript (`Date.now()`) or Java (`System.currentTimeMillis()`) —
  divide by 1000 and truncate.
- **Stale or future-dated** (before 2020, or more than a day ahead): routed, but not stored in
  the database.
- JSON `true`/`false` also pass the server's integer check (Python `bool` is an `int`). The
  Python SDK rejects them; other senders should too.

Correct: `1732982400`. Shell: `$(date +%s)`. Python: `int(time.time())`.

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
recompute it after the check, and it overwrites the payload's `client` with it (lowercased).

### Authorization

- You must hold an `alerts:write` scope covering the effective client (or a `('*', …)` wildcard
  scope). Otherwise: **`403`**.
- The clientless case (`""`) additionally requires the **`master_admin`** role. A regular account
  cannot send a fully clientless alert, even with a wildcard scope.
- A non-empty effective client must also be a **registered, active client** (the master_admin-only
  Clients page). Otherwise: **`400`**, with an explicit `{"error": "Unknown client", ...}` body —
  not a silent drop, and not the generic `403 forbidden` a missing-scope failure returns. This
  check runs *after* the scope check above, so an unauthorized caller never learns whether a
  client name is registered. The clientless case (`""`) is never checked against this registry —
  it's a distinct, valid state, not a missing client name.

Practically: omit `client` and let your account's `default_client` apply, unless you are a
multi-tenant sender that legitimately writes for several clients — in which case set it
explicitly and have the operator register each client and grant a scope per client.

A `403` here means a missing scope, not a bad password. Ask the operator to run
`user-manager.py add-scope <role> <client> alerts:write`.

---

## Validation happens in two stages

This is the highest-value thing to understand, because the two stages disagree and only the
first one talks back to you.

**Stage 1 — the gateway (`POST /alert`), synchronous.** In order: authenticates you, authorizes
the effective client, checks the client is registered, then checks `Content-Type:
application/json`, that the body parses as JSON, that it is non-empty, and that the six required
field *names* are present. It does **not** check types or enum values. Then it publishes to the
internal broker and returns `200`.

**Stage 2 — the alert router, asynchronous, after you already got your `200`.** Enforces the
real schema: `timestamp` is an integer, `source`/`level`/`category`/`client` are strings,
level/category enums, aliases, normalization. A failure here is logged to
`data/logs/ops/alert-router.{date}.log`, increments an error counter, and the alert is
**dropped**. No retry, no callback, no trace in your HTTP response.

Verified behaviour:

| Payload | Gateway | Router | Net result |
|---------|---------|--------|------------|
| fully valid | `200 success` | accept | delivered |
| `"level": " ERROR "` | `200 success` | accept → normalized to `critical` | delivered, but **not stored in the database** (see [Normalization](#normalization)) |
| `"level": "bogus"` | `200 success` | **reject** | **silently dropped** |
| `"category": "test"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": "2024-01-15T10:30:00Z"` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": 1732982400.123` | `200 success` | **reject** | **silently dropped** |
| `"timestamp": 1732982400123` (milliseconds) | `200 success` | **accept** | **delivered with a far-future date; not stored in the database** |
| `"source": {"name": "x"}` | `200 success` | **reject** | **silently dropped** |

**Implication for senders:** validate `level`, `category`, and `timestamp` client-side. Do not
treat `200` as delivery confirmation. Either use the Python SDK (which does this for you) or
hard-code the enum lists above. If alerts are "sent successfully" but never appear in logs,
stage 2 is where they died — ask the operator to check the router's ops log.

---

## Responses

Success and server errors are keyed on `status`; client errors are keyed on `error`. Handle both.
Every body also carries a `timestamp` (ISO-8601 string — the only place this API uses ISO time).

| Code | Condition | Body |
|------|-----------|------|
| `200` | Accepted for routing (**not** delivery-confirmed) | `{"status":"success","message":"Alert forwarded to MQTT broker","alert_id":"<source>-<epoch>","timestamp":"<ISO>"}` |
| `400` | Effective `client` is non-empty but not a registered, active client | `{"error":"Unknown client","detail":"Client '<name>' is not a registered client.","client":"<name>"}` |
| `400` | Wrong `Content-Type` | `{"error":"Content-Type must be application/json","received_content_type":…}` |
| `400` | Body is not valid JSON | `{"error":"Invalid JSON format","detail":…,"json_error":…,"hint":…}` |
| `400` | Empty body, **including `{}`** | `{"error":"Empty JSON payload","detail":"Request body cannot be empty"}` |
| `400` | Missing required field(s) | `{"error":"Missing required fields: …","required_fields":[…]}` |
| `401` | Bad or missing credentials, or account locked | `{"status":"error","error":"authentication required"}` |
| `403` | No `alerts:write` scope for the effective client, or clientless without `master_admin` | `{"status":"error","error":"forbidden"}` |
| `500` | Broker publish failed | `{"status":"error","message":"Failed to forward alert to MQTT broker"}` |
| `500` | Unexpected server error | `{"status":"error","message":"Internal server error","detail":…}` |
| `503` | Credential verification couldn't complete (transient database issue) | `{"status":"error","error":"Credential verification temporarily unavailable"}` |

Checks run in the order of [stage 1](#validation-happens-in-two-stages), so auth and client
errors (`401`/`403`/`503`/unknown-client `400`) come before any body-format `400`. If the body
isn't parseable JSON, the effective client falls back to your account's `default_client` for the
authorization step.

`{}` returns "Empty JSON payload" rather than "missing required fields" — an empty dict is falsy,
so that check fires first. Retry `500`/`503` (transient); do not retry `400`/`401`/`403`.

---

## Examples

### Minimal alert (required fields only)

```json
{
  "timestamp": 1729213847,
  "level": "info",
  "category": "system",
  "title": "Service Started",
  "message": "Application server started successfully",
  "source": "app-server-01"
}
```

### Alert with metadata and an explicit client

```json
{
  "timestamp": 1729213847,
  "level": "critical",
  "category": "application",
  "title": "Database Connection Failed",
  "message": "Unable to connect to primary database server, switching to replica",
  "source": "api-server-03",
  "client": "acme-co",
  "metadata": {
    "error_code": "ECONNREFUSED",
    "database_host": "db-primary.local",
    "retry_count": 3,
    "environment": "production"
  }
}
```

Use `metadata` for machine-readable data and `message` for the human-readable description.

### cURL

```bash
curl -X POST https://alert.d-t.pw/alert \
  -u username:password \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'$(date +%s)',"level":"warning","category":"system","title":"Alert","message":"Test","source":"curl"}'
```

### Bearer token instead of Basic

```bash
curl -X POST https://alert.d-t.pw/alert \
  -H "Authorization: Bearer amt_..." \
  -H "Content-Type: application/json" \
  -d '{...}'
```

### Python — SDK (recommended)

**Install** — the SDK lives in this repository under [`sdk/python/`](../sdk/python/) and can be installed straight from GitHub:

```bash
pip install "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"
```

**Check for updates** — releases are tagged `vX.Y.Z` and SDK-specific changes are listed in [`sdk/python/CHANGELOG.md`](../sdk/python/CHANGELOG.md). To pick up the latest version:

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

Fields without a dedicated parameter (`routing`, `pathway_config`) pass through as keyword
arguments: `client.send(..., routing={"override": True, "pathways": ["file_log"]})`.

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

### Linux host health agent (turn-key)

For plain "is this box and its containers alive and healthy" reporting, use the ready-made
agent at [`clients/linux-agent/`](../clients/linux-agent/) instead of writing your own sender.
It is a single bash script (deps: `jq`, `curl`) for a 30-minute root cron/systemd timer that
sends proof-of-life snapshots (host telemetry + Docker container summary) every run, plus
debounced threshold alerts for disk/load/memory, container state changes, and new kernel
warnings/errors. Configuration is one dotenv file (`ALERT_URL`, `ALERT_CLIENT`, `ALERT_TOKEN`);
see its [README](../clients/linux-agent/README.md) for install and provisioning.

### Claude Code skill (turn-key, for LLM agents)

If an LLM coding agent (e.g. Claude Code) is doing the integration work,
point it at [`skills/alert-manager/`](../skills/alert-manager/) instead of
this document directly. It's a packaged Agent Skill covering everything on
this page plus the SDK and the linux health agent above, with the gotchas
that most often produce a silent failure called out explicitly. It ships
this document as its payload reference.
`skills/alert-manager/install.sh` copies it into `~/.claude/skills/` (every
Claude Code session on the machine) or a single project's `.claude/skills/`.

---

## What happens to your alert

### Routing

Which pathways an alert reaches is decided by the operator's routing rules, unless you
override it.

- **Rules** are evaluated in order; the **first match wins** and its pathway list is used. If no
  rule matches, the alert is delivered **nowhere** (not even the file log).
- A rule's conditions are lists of accepted values for any of `level`, `category`, `source`,
  `client`, and `metadata.<key>`; every condition present must match. Values are compared
  *after* normalization, so match against lowercase canonical values. (A `metadata` condition
  is skipped entirely when the alert has no `metadata` object.)
- **Override**: `"routing": {"override": true, "pathways": [...]}` replaces the rules outright —
  the listed pathway names are used verbatim. An override with an empty or missing `pathways`
  list routes the alert nowhere. Pathway names are deployment-specific; ask the operator (the
  reference config uses `file_log`, `console_alerts`, `led_sign`, `email_critical`). Use
  overrides only when you have to — let the rules handle routing.
- `routing.exclude` is **not implemented** — only `override` and `pathways` are read. Sending it
  has no effect.

Each pathway can also filter on its own (`level_filter`, `category_filter`, `source_filter`,
`client_filter` in the operator's config), so a pathway named by a rule or override may still
skip an alert.

```json
{
  "timestamp": 1732982400,
  "level": "critical",
  "category": "security",
  "title": "Security Alert",
  "message": "Unauthorized access detected",
  "source": "security-monitor",
  "client": "acme-co",
  "routing": {"override": true, "pathways": ["email_critical", "led_sign"]}
}
```

### Pathways

| Pathway | Destination |
|---------|-------------|
| `file` | `/data/logs/file/{client}/{date}.log`, date-rotated (clientless alerts go to a flat, admin-only `/data/logs/file/{date}.log`) |
| `email` | O365 Graph API, HTML templates, optional storm aggregation (bursts of critical alerts within a few seconds become one summary email) |
| `led_sign` | BetaBrite signs over MQTT, per zone — see [LED signs](#led-signs) |
| `console` | stdout of the router |
| `mqtt` | An arbitrary internal MQTT topic |

Separately from routing, every alert is also written to PostgreSQL (which feeds the dashboard)
by the database-logger service, regardless of routing rules — as sent, without normalization,
and only if `level`/`category` are canonical and `timestamp` is in range (see
[Normalization](#normalization) and [The `timestamp` field](#the-timestamp-field)). Rows the
database rejects are logged server-side and dropped.

### Gateway-added fields

The gateway attaches a `_gateway` object before publishing (any `_gateway` you send is replaced):

```json
{
  "_gateway": {
    "source": "http-gateway",
    "received_at": 1729213847,
    "client_ip": "192.0.2.10",
    "user_agent": "curl/8.5.0",
    "authenticated_user": "my-app",
    "user_type": "user"
  }
}
```

`user_type` is `master_admin` or `user`.

### Message sanitization (file logs only)

File pathways collapse alerts to one line: `\r\n`/`\r`/`\n` → ` ↵ `, tabs → space, runs of
spaces collapsed (applied to `title`, `message`, `source`), and `message` truncated at 500 chars
with `...`. Email, database, and console keep the full multiline text. This affects only how
your message *renders* in file logs — send multiline freely.

```
1735577400 [critical] [application] source [acme-co]: Title - SQL Error: ↵ Column 'date' cannot be null ↵ Retrying...
```

### LED signs

Only relevant if the alert routes to an LED sign pathway. By default the **server** decides
everything: which zones show the alert (each zone's level/category/source filters, falling back
to the default zone), and how it looks (presets by level and category — e.g. `critical` and
`security` alerts interrupt the rotation, `news` alerts get a fixed preset).

`metadata.led_zones` and a top-level `display_config` are **not read** by the LED pathway. To
influence a sign, use [`pathway_config`](#per-pathway-overrides-pathway_config) keyed by the LED
pathway's name.

`display_config` values (unknown names silently fall back to the default):

| Field | Values |
|-------|--------|
| `mode` | `rotate`, `hold`, `flash`, `scroll`, `auto`, `rollup`, `rolldown`, `rollleft`, `rollright`, `rollin`, `rollout`, `wipeup`, `wipedown`, `wipeleft`, `wiperight`, `wipein`, `wipeout`, `compressedrotate`, `explode`, `clock`, `special`, `newsflash` |
| `special_effect` | `twinkle`, `sparkle`, `snow`, `interlock`, `switch`, `slide`, `spray`, `starburst`, `welcome`, `slots`, `newsflash`, `trumpet`, `cyclecolors`, `thankyou`, `nosmoke`, `nodrive`, `fishimal`, `fireworks`, `turballoon`, `bomb` |
| `color` | `red`, `green`, `amber`, `dimred`, `dimgreen`, `brown`, `orange`, `yellow`, `rainbow1`, `rainbow2`, `colormix`, `autocolor` |
| `character_set` | `5high`, `5stroke`, `7high`, `7stroke`, `7highfancy`, `10high`, `7shadow`, `fhighfancy`, `fhigh`, `7shadowfancy`, `5wide`, `7wide`, `7widefancy`, `5widestroke` |
| `position` | `topline`, `midline`, `botline`, `fill`, `left`, `right` |
| `speed` | `1`–`5` (slowest to fastest) |
| `priority` | boolean — interrupt normal rotation |
| `duration` | seconds, for priority messages (the server may scale it by category) |

See `docs/BETABRITE.md` for what each effect looks like on the hardware.

### Per-pathway overrides (`pathway_config`)

`pathway_config` is an object keyed by **pathway name** (not type). Each pathway reads only its
own entry, and only these keys:

| Pathway type | Key | Effect |
|--------------|-----|--------|
| `led_sign` | `zones` | String or array of zone names — replaces the server's zone selection |
| `led_sign` | `display_config` | Object (values above) merged over the server's presets |
| `file` | `format` | Replaces the log line format (`{timestamp}`, `{level}`, `{category}`, `{source}`, `{client}`, `{title}`, `{message}`) |

Other pathway types ignore `pathway_config`. The entry only matters if that pathway is actually
selected by a rule or override.

```json
{
  "timestamp": 1729213847,
  "level": "warning",
  "category": "weather",
  "title": "Severe Thunderstorm Warning",
  "message": "Severe thunderstorm warning in effect until 8:00 PM",
  "source": "weather-api",
  "metadata": {"region": "Edmonton, AB", "expires_at": 1729220400},
  "pathway_config": {
    "led_sign": {
      "zones": ["kitchen", "living_room"],
      "display_config": {"mode": "flash", "color": "amber", "speed": 3, "priority": true, "duration": 30}
    }
  }
}
```

---

## Best practices

1. **Timestamps**: Unix epoch integer seconds, every time.
2. **Sources**: clear, stable identifiers (`web-server-01`, not `srv1`) — the dashboard groups by
   client and then source.
3. **Titles**: concise but descriptive; put detail in `message`.
4. **Levels**: reserve `critical` for real emergencies — it typically pages someone by email.
5. **Categories**: use them consistently so routing rules can rely on them.
6. **Client**: omit it unless you write for several clients.
7. **Don't trust `200`**: validate client-side, and spot-check that alerts actually land.

---

## Not an ingestion path

**MQTT** (`alert.d-t.pw:42690`) is a device bus for ESP32 BetaBrite LED signs only — display push
and telemetry. The internal `alerts/input` topic is service-to-service plumbing and is not ACL'd
for external publish, so you cannot submit alerts over it. See `docs/ESP32_BETABRITE_IMPLEMENTATION.md`
if you are building a sign, not a sender.

**SNMPv3** trap ingestion and the **syslog** gateway are deprecated and unsupported. If you find
an old example that publishes an alert to an MQTT topic, sends a trap, or uses `routing.exclude`,
don't follow it.

---

## Related documentation

| Document | Content |
|----------|---------|
| `CLAUDE.md` | System-wide technical reference |
| `HTTP_GATEWAY.md` | Full HTTP endpoint reference |
| `AUTH.md` | Auth model: sessions, RBAC roles/scopes, tokens, lockout |
| `BETABRITE.md` | LED sign hardware capabilities |
| [`clients/linux-agent/README.md`](../clients/linux-agent/README.md) | Turn-key Linux host/Docker health agent |
| [`skills/alert-manager/`](../skills/alert-manager/) | Claude Code Agent Skill for LLM agents integrating with this service |
