---
name: alert-manager
description: >-
  Integrates with a self-hosted Alert Manager service (an HTTPS JSON-alert
  ingestion/routing gateway -- this repo's own deployment example is
  alert.d-t.pw) for sending alerts and managing access to it. Use when the
  user asks to send an alert, notify them when something fails or completes,
  hook up exception/error reporting for an app, set up host or Docker health
  monitoring on a Linux box, install or use the Alert Manager Python SDK,
  troubleshoot a POST /alert 401/403/400/500/503 or a "my alert never
  arrived" report, or provision/rotate Alert Manager credentials (users,
  roles, scopes, tokens, clients). Covers the payload contract (required
  fields, level/category enums, the client field's authorization rules, the
  two-stage validation gap where HTTP 200 does not mean the alert was
  delivered), HTTP Basic/Bearer auth, the turnkey Linux host-monitoring
  agent, and raw curl/Python usage when the SDK isn't an option. Not for
  generic OS-level reminders, calendar alarms, or notification systems
  unrelated to this specific service.
allowed-tools: Read, Write, Edit, Grep, Bash
---

# Alert Manager

A technical framework for sending alerts to, and managing access to, a
self-hosted Alert Manager instance. The service accepts a flat JSON payload
over HTTPS and routes it to file logs, email, LED signs, a database, and/or
console output based on server-side config. This repo's own example
deployment is `https://alert.d-t.pw`; if you're integrating with a
*different* Alert Manager instance, the wire contract below still applies —
just swap the base URL.

**`POST /alert` is the sole ingestion path.** Earlier versions of this
project also accepted MQTT and SNMPv3 traps; both are dead ends now (MQTT
survives only as a device bus for LED signs, not alert intake). If you land
on an old example that publishes an alert to an MQTT topic, or on
`docs/ALERT_PAYLOAD_SPEC.md` in this repo (it's **stale** — still documents
MQTT ingestion and a working `routing.exclude`, neither of which is true —
`docs/CLIENTNODE.md` supersedes it), don't follow it.

## Which tool for this task

| Situation | Use |
|---|---|
| One-off alert, right now, from a shell | `curl` (example below) or `scripts/send-alert.sh` |
| Building/maintaining a Python app or service | The Python SDK — see `references/sdk-python.md` |
| Automatic reporting of uncaught exceptions in a Python app | The SDK's `install_exception_hooks()` specifically — not a hand-rolled `logging.Handler` (see gotchas) |
| "Monitor this Linux box / its Docker containers" | Deploy the turnkey `clients/linux-agent` bash agent — don't write custom polling code. See `references/linux-agent.md` |
| Sender is in a language with no SDK | Raw HTTP per `references/payload-reference.md` |
| Something's already failing (401/403/400/500/503, or an alert that never showed up) | Skip to the debugging ladder below |

## Top gotchas

These cause the vast majority of "it said success but nothing happened" and
"why did this get rejected" reports. Read all of them before writing a
sender — most are silent failures with no error to grep for.

1. **`HTTP 200` is not delivery confirmation.** `POST /alert` only checks
   that the six required field *names* are present, then returns `200`
   immediately. Real validation (type checks, `level`/`category` enum
   checks) happens asynchronously afterward, inside the routing layer. If it
   fails there, the alert is **silently dropped** — no error, no callback,
   nothing in the HTTP response. Validate `level`/`category`/`timestamp`
   yourself before sending (the SDK and `scripts/send-alert.sh` both do
   this for you).

2. **`timestamp` must be an integer, in seconds — and this has two separate
   failure modes.** A string or float timestamp is caught by the async
   validation above and silently dropped — annoying, but at least it's a
   known failure. Worse: a **millisecond** timestamp (e.g. a straight port
   of JavaScript's `Date.now()`) is a perfectly valid integer. It passes
   every validation stage, everywhere, and produces an alert with a
   timestamp roughly 1000x too large — a garbage date far in the future —
   with **no error at any layer**. `1732982400` is right; `1732982400123`
   is wrong and nothing will tell you so. Watch for this specifically when
   porting alerting logic from a non-Python source.

3. **`client` is an authorization subject, not a free-text label.** The
   effective client (payload `client`, else the account's configured
   default, else clientless) must be covered by an `alerts:write` scope on
   your credentials, or you get `403`. Separately, a non-empty client must
   also be a **pre-registered, active client name** — registered through a
   master_admin-only web UI (`/admin/clients`); **there is no CLI for
   this**. If you hit `400 "Unknown client"` and don't have master_admin web
   access yourself, stop and tell the user exactly what needs to happen
   (register the client name, or use one that's already registered) —
   don't try to work around it.

4. **Never use a session cookie for the API.** Some docs for this service
   show a browser-login curl flow (`-c cookies.txt` against `/login`) right
   next to the API auth docs. That flow is for the browser dashboard only.
   `/alert`, `/test`, `/health`, and friends take HTTP Basic or a Bearer
   token — never a cookie.

5. **Don't wrap `POST /alert` in a naive retry-on-timeout loop.** A
   connection failure/timeout is genuinely ambiguous — the server may have
   already processed the (non-idempotent) POST. Retrying blindly risks a
   duplicate alert. Only `500` (broker-side, transient) is documented as
   safe to retry; `400`/`401`/`403` won't be fixed by retrying, and a
   timeout/connection error is a "tell the caller, don't auto-retry" case.

## Getting credentials

Ask the user for a base URL plus either a username/password or a Bearer
token before doing anything else. Check the current project for an existing
convention first — a `.env`/`.env.example` with `ALERT_MANAGER_URL` (or the
older `ALERT_URL`/`ALERT_USER` variants — see the env-var table in
`references/auth-and-provisioning.md`) usually means credentials already
exist somewhere and just need locating, not creating.

Only attempt server-side provisioning (creating a new account/scope/token)
if you already have operator/deploy access to the Alert Manager host itself
— see `references/auth-and-provisioning.md` for the exact commands. If you
don't have that access, don't guess or fabricate credentials: stop and ask.

## Debugging ladder

Work through these in order — each one rules out a whole class of problem
before you touch the next:

1. `GET /health` — no auth needed. If this fails, the service itself is
   down or unreachable; nothing below matters yet.
2. `GET /api/whoami` with your credentials — confirms they're valid at all,
   and shows you `default_client`/`is_master_admin`/scopes.
3. `POST /test` — exercises a real end-to-end send without needing
   `alerts:write` for a specific client. If this works but a real alert
   doesn't, the problem is your payload or your client authorization, not
   connectivity or credentials.
4. A real `POST /alert` with your actual payload.

## Quick reference

**Required fields**: `timestamp` (int, Unix epoch **seconds**), `level`,
`category`, `title` (string), `message` (string), `source` (string).

**Levels**: `critical` (aliases `error`/`err`/`crit`), `warning` (alias
`warn`), `notice` (no alias), `info` (alias `inf`), `debug` (alias `dbg`).

**Categories**: `security`, `system`, `weather`, `automation`, `personal`,
`network`, `application`, `news`, `business`, `hardware`, `environmental`.

**Auth headers**: `Authorization: Basic <base64 user:pass>` (or curl's
`-u user:pass`) or `Authorization: Bearer amt_...`. Never a session cookie.

**Response codes** (quick glance — full body shapes in
`references/payload-reference.md`, auth-flavored troubleshooting in
`references/auth-and-provisioning.md`):

| Code | Meaning |
|---|---|
| `200` | Accepted for routing — **not** delivery-confirmed (see gotcha 1) |
| `400` | Bad request: wrong content-type, invalid/empty JSON, missing required field, or an unregistered `client` |
| `401` | Bad or missing credentials, or the account is locked out |
| `403` | Authenticated, but no `alerts:write` scope for the effective client |
| `500` | Broker publish failed — transient, safe to retry once |
| `503` | Credential verification couldn't complete (DB hiccup) — retry shortly |

## Examples

**curl:**

```bash
curl -X POST "${ALERT_MANAGER_URL:-https://alert.d-t.pw}/alert" \
  -u "$ALERT_MANAGER_USERNAME:$ALERT_MANAGER_PASSWORD" \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'"$(date +%s)"',"level":"warning","category":"system","title":"Alert","message":"Test","source":"curl"}'
```

**Python (SDK — recommended for anything beyond a one-off):**

```python
from alertmanager_sdk import AlertClient

client = AlertClient(
    "https://alert.d-t.pw", username="my-app", password="...",
    default_source="my-app",
)
client.send(level="info", category="application", title="Status", message="Task completed")
```

## Staying current

This service has no live self-describing endpoint (no `openapi.json`
equivalent), so the freshest wire contract lives in two places depending on
whether you have credentials yet:

- **No credentials yet**: `https://raw.githubusercontent.com/jimidarke/alertManager/main/docs/CLIENTNODE.md`
- **Have credentials**: `GET /docs` on the actual deployment — an
  authenticated route serving the *actually-deployed* contract doc, which
  can differ from GitHub `main` if that instance is pinned to an older
  release or is someone's fork.

## Reference files

- `references/payload-reference.md` — full field tables, LED display config,
  routing overrides, the complete validation/response-code tables. Read
  when you need more than the quick reference above.
- `references/auth-and-provisioning.md` — auth model, the `client` field's
  authorization rules in depth, how to provision a new sender account,
  env-var naming conventions, full troubleshooting table. Read when
  something's `401`/`403`/`400`-unknown-client, or you need to create
  credentials.
- `references/sdk-python.md` — SDK install/upgrade, full method surface,
  exception hierarchy, `install_exception_hooks()`. Read before writing any
  non-trivial Python sender.
- `references/linux-agent.md` — when/how to deploy the turnkey host-health
  bash agent instead of writing custom monitoring code.
- `scripts/send-alert.sh` — a portable sender with the enum/type validation
  from gotcha 1 built in, for when you're in a shell with no SDK available.
  Run it with no arguments for usage.

If you're working inside this repo's own checkout, `tools/send-http-alert.sh`
and `sdk/python/scripts/smoke_test.py` also exist and do similar jobs, but
neither travels with you to another repo — `scripts/send-alert.sh` in this
skill does, since it's installed alongside these instructions.
