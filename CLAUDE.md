# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# Alert Manager - Technical Reference

**Version:** 3.4.1
**Last Updated:** 2026-10-26

## Project Purpose

The Alert Manager captures JSON alert payloads via HTTPS (`POST /alert`, the sole supported ingestion path as of v2.0.2) and routes them to pathways including files (used by HTTP gateway for log viewing), email, LED signs, and console. All alerts follow a standardized JSON payload structure with strict validation.

**As of v2.0.2**: MQTT and SNMPv3 are no longer alert-ingestion paths. MQTT remains in active use for LED sign device communication (display push + telemetry, see "MQTT Broker" below) but is not a way to submit an alert into the system -- the internal `alerts/input` topic is service-to-service plumbing (feed-aggregator -> alert-router), never a client-facing ingestion contract. SNMPv3 trap ingestion is deprecated outright (see "SNMP Gateway" below) -- it was built for potential SNMP-trap-only network hardware that never materialized in production.

## Public Repository

**As of 2026-08-09 this repo is PUBLIC** at <https://github.com/jimidarke/alertManager> (MIT license), with history purged to a single root commit ("Initial public release (v3.1.1)") — the old history contained leaked keys/hashes and must never be re-pushed. Rules that follow from this:

- **Never commit real personal data or credentials.** The tracked tree uses sanitized placeholders: emails are `yourdomain.com`/`example.com`, the example client is `acme-co`, SNMP doc passwords are `AUTH-PASSWORD-HERE`/`PRIV-PASSWORD-HERE`. Keep it that way in all new code, docs, and configs.
- **`data/config/docker.json` in git is a sanitized template.** The real production copy lives server-side only (`/opt/docker/alert-manager`) with real recipient emails and client names — never overwrite it with the repo copy on deploy, and never copy its real values back into git.
- `.claude/` is gitignored (local settings stay local).
- The Python SDK is public-installable: `pip install "git+https://github.com/jimidarke/alertManager.git#subdirectory=sdk/python"` (documented in `docs/CLIENTNODE.md`).
- **`skills/alert-manager/`** is a Claude Code Agent Skill (`SKILL.md` + `references/` + `scripts/send-alert.sh`) that teaches an LLM agent the payload contract, auth model, SDK, and `clients/linux-agent` so it integrates correctly on the first try. It's tracked source, not itself auto-loaded -- `skills/alert-manager/install.sh` copies it into `~/.claude/skills/` (or `$CLAUDE_SKILLS_DIR`) to activate it; re-running that script is also how it's updated (no auto-updater by design). A gitignored `.claude/skills/alert-manager` symlink in this repo points at the tracked source for local dev-loop testing.

## Core Architecture

### Pathway System
Pathways are loaded by `pathways/pathway_manager.py` (`PathwayManager`) and instantiated by `type`. The registered pathway types are **file, email, console, mqtt, led_sign** ONLY:
- **FilePathway**: Logs alerts to files with date-based rotation (`/data/logs/file/{client}.{date}.log`)
- **EmailPathway**: Sends alerts via Microsoft Graph API (O365) with MSAL authentication; supports Jinja2 templates and storm-prevention aggregation
- **LEDSignPathway**: Routes to BetaBrite LED signs with zone management
- **ConsolePathway**: Outputs alerts to console/stdout
- **MQTTPathway**: Publishes to arbitrary MQTT topics

**Note**: LEDSignPathway and MQTTPathway connect to internal MQTT broker only (localhost:1883). TLS for outbound pathway connections is not currently implemented.

**Database logging is NOT a router pathway.** It runs as a separate consumer service (`src/database_logger.py`) that subscribes to the MQTT `alerts/input` topic directly and writes to PostgreSQL with connection pooling and batch inserts.

### Feed Aggregator (Content Source, not a pathway)
`src/feed_aggregator.py` is a standalone service that *produces* alerts rather than consuming them. It uses APScheduler to periodically fetch external content, convert it to compliant alert payloads (`src/utils/payload_builder.py`), and publish to MQTT `alerts/input` for normal routing. Feed handlers live in `src/feeds/` and all subclass `BaseFeed`:
- **NewsFeed** (RSS, e.g. CBC/CTV Top Stories), **JokeFeed** (JokeAPI), **FactFeed** + **TriviaFeed** (API Ninjas — require `API_NINJAS_KEY`)
- Configured under the `feed_aggregator` key in the service config; client name defaults to `feed-updates`
- On fetch failure: retry once, then emit a warning alert and skip the cycle
- See `src/feeds/README.md` for adding new feeds

### URL Monitor (Content Source, not a pathway)
`src/url_monitor.py` is a standalone service that *produces* alerts rather than consuming them, modeled directly on `src/feed_aggregator.py`. It polls a server-configured list of URLs on independent APScheduler interval jobs -- a headers-only HTTP GET (`stream=True`, body never read) -- and tracks per-URL health as an in-memory OK/DEGRADED/DOWN state machine, publishing alerts (`category: application`) to MQTT `alerts/input` for normal routing:
- Healthy polls emit `debug` each cycle (routed to per-client file logs only, effectively invisible); a state change to DEGRADED (wrong status or slow response) emits `warning`; a state change to DOWN (timeout, DNS failure, connection refused, TLS error) emits `critical`; recovery (any unhealthy state -> OK) emits `notice` with the outage duration.
- While a URL stays unhealthy, a reminder alert re-fires every 12 hours (configurable) so long outages aren't forgotten, titled "STILL DOWN"/"STILL DEGRADED".
- Configured under the `url_monitor` key; global `defaults` (`interval_seconds`, `timeout_seconds`, `max_response_ms`, `expected_status`, `follow_redirects`, `reminder_interval_seconds`) are overridable per URL entry. Each URL entry requires `name`, `url`, `client`.
- State is in-memory only -- a restart forgets prior history and treats every URL as first-seen on its next poll.
- Deployment: Docker profile `secondary`, healthcheck via `src/utils/heartbeat.py` (`HEARTBEAT_FILE`), same pattern as `analytics-engine`. See `docs/URL_MONITOR_PLAN.md` for the full design.

### Routing Logic
- Configuration-driven routing in JSON config files
- Rules processed in order with first-match semantics
- Each rule contains conditions (level, category, source) and pathways (list of pathway names)
- Payload-level routing overrides supported via `routing.override` in alert messages

### MQTT Broker
**Not an alert-ingestion path** (as of v2.0.2) -- this is a device-communication bus (LED sign display push + telemetry), separate from how alerts get *into* the system. See "Project Purpose" above.
- **Internal**: `localhost:1883` (unauth, container network only)
- **External**: `alert.d-t.pw:42690` (TLS 1.2, username/password auth)
  - **Scope**: ESP32 LED sign devices ONLY
  - All other clients (scripts, services, integrations) use HTTP gateway
- **Input Topic**: `alerts/input` (internal routing only -- service-to-service, e.g. feed-aggregator -> alert-router; not reachable/ACL'd for external publish)
- **Status Topic**: `alerts/service/status` (service health, published every 60s)
- **LED Topics**: `ledSign/{zone}/message` (zone-specific messages)
- **LED Status Topics**: `ledSign/{zone}/rssi|ip|uptime|memory` (device telemetry)
- **Authentication**: Username/password ONLY (client certificates deprecated)
- **ACLs**: Zone-specific per mqttservernotes.txt (each device reads its zone only)
- **QoS Level**: QoS 1 (at-least-once delivery)
- **Management**: Use `tools/mqtt-auth-manager.sh` for user management
- **ESP32 Optimized**: TLSv1.2 with hardware-accelerated cipher suites, 2048-bit RSA

### HTTP Gateway
- **Host**: `alert.d-t.pw` (port 443, HTTPS only via `nginx-proxy`) -- as of v2.0.0 the `http-gateway` container publishes **no** host port; container-internal `:8080` is not reachable from outside the Docker host at all
- **Implementation**: `src/gateway/` (Flask app-factory package, `create_app()`; replaces the deleted `src/http_gateway.py`), served by gunicorn (`gateway.wsgi:app`, 1 worker x 8 gthread threads) -- organized into five blueprints, each with its own auth policy:
  - **`api`** (`gateway/api/`) -- `GET /health` (no auth), `POST /alert`, `POST /test`, `GET /docs` (serves `CLIENTNODE.md`) and `GET /docs/<f>` (both unauthenticated) -- Basic/Bearer only, never a session cookie
  - **`auth`** (`gateway/auth/`) -- `GET/POST /login`, `POST /logout`, `GET /api/whoami` (session, Basic, or Bearer)
  - **`logs`** (`gateway/logs/`) -- `GET /logs` (page), `GET /api/logs/list`, `GET /api/logs/tail`, `GET /api/logs/chunk` -- session **or** Basic/Bearer (keeps `curl -u` shell-test workflows working)
  - **`dashboard`** (`gateway/dashboard/`) -- `GET /dashboard` (page), `GET /api/dashboard/summary`, `GET /api/dashboard/groups` (alerts grouped client -> source, latest 30 per source at the selected `levels`; the level cards are multi-select filters, default critical) -- session only
  - **`admin`** (`gateway/admin/`) -- `GET /admin/users`, `GET/POST /admin/users/new`, `GET/POST /admin/users/<username>/edit`, `POST .../disable|enable|reset-password`, `POST .../tokens/new`, `POST .../tokens/<id>/revoke`, `POST /admin/users/<username>/delete`, `GET /admin/clients`, `GET/POST /admin/clients/new`, `POST /admin/clients/<name>/disable|enable|delete` -- session only, `master_admin` role required. Delete is a second step: only a *disabled* user/client can be deleted (and never yourself); it removes just the identity/registry rows (a user's tokens and personal `user:<name>` role go with them) and never touches alerts, logs or `auth_audit`
- **Auth**: session cookie (browser, 12h sliding, `HttpOnly`/`Secure`/`SameSite=Lax`, Flask-WTF CSRF-protected) for `dashboard`/`admin`/browser use of `logs`; HTTP Basic Auth or Bearer Token (argon2id password hash / SHA-256-hashed token, against the Postgres identity store) for `api` and script/device use of `logs`. See "User Access Control" below and `docs/AUTH.md` for the full model.

### SNMP Gateway (DEPRECATED)
- **Status**: Deprecated as of v2.0.2
- **Reason**: Built speculatively for potential SNMP-trap-only network hardware; no such device was ever connected in production. HTTPS (`POST /alert`) is the sole supported alert-ingestion path.
- **File**: `src/snmp_gateway.py` (retained for reference only)
- **Alternative**: Use HTTP gateway with authentication. If SNMP-trap-only hardware ever materializes, this service can be moved back out of the `deprecated` compose profile -- the code and its detailed protocol/OID/MIB reference (`docs/SNMP_INTEGRATION.md`, `docs/SNMPV3_SPECIFICATION.md`, `docs/SNMP_CLIENT_MAPPING.md`) are preserved, not deleted.

### Analytics Engine
- **Purpose**: Scheduled log analysis and professional email reporting
- **Scheduling**: Daily, weekly, or monthly analysis with cron-like configuration
- **Analyzers**: Pluggable analyzer modules for different analysis types
- **Email Reports**: Professional HTML email templates with tables and charts
- **Current Analyzers**:
  - **Barcode Not Found**: Tracks failed UPC/barcode lookups with detailed statistics
- **Configuration**: JSON-based in `analytics` section of config files
- **Deployment**: Docker profile `analytics` with read-only access to all logs
- **Use Cases**: Pattern detection, metric tracking, weekly summaries, compliance reports

### Syslog Gateway (DEPRECATED)
- **Status**: Deprecated as of v1.2.0
- **Reason**: Limited adoption, SNMPv3 provides superior device integration
- **File**: `src/syslog_gateway.py` (retained for reference only)
- **Alternative**: Use HTTP gateway with authentication for log forwarding

## Directory Structure

```
data/
├── logs/
│   ├── ops/                           # Operational logs (admin-only)
│   │   ├── alert-router.{date}.log
│   │   ├── alert-http-gateway.{date}.log
│   │   ├── alert-file-logger.{date}.log
│   │   ├── alert-snmp-gateway.{date}.log
│   │   ├── analytics-engine.{date}.log
│   │   └── mosquitto.log
│   ├── file/                          # Alert content logs (clientname matching)
│   │   ├── {date}.log                 # Default log
│   │   ├── {client}/{date}.log        # Per-client logs (weekly rotation)
│   │   └── unauthenticated/{date}.log # SNMP traps without valid auth
│   └── snmp-discovery/                # SNMP discovery mode logs
│       └── snmpFULL.{date}.jsonl      # Raw trap data with usernames
├── config/                            # Configuration files
│   ├── docker.json                    # Docker container config
│   ├── main.json                      # External/production config
│   ├── test.json                      # Local development config
│   └── snmp-gateway/                  # SNMP user management
│       ├── users.json                 # SNMPv3 user accounts
│       └── audit.log                  # SNMP auth audit trail
├── certs/                             # TLS certificates
└── mosquitto/                         # MQTT broker data
```

### Log Segregation
- **ops/**: Operational logs (INFO/WARNING/ERROR/CRITICAL) from containers - parsing errors, connection issues, service status
- **file/**: Alert message content from `alerts/input` topic - the actual alert data being processed
- **snmp-discovery/**: Raw SNMP trap data for analysis (includes authenticated usernames, assigned clients)
- **Access**: ops/ is admin-only, file/ requires clientname matching, snmp-discovery/ is admin-only

### Date-Based Rotation
- All log paths support `{date}` placeholder
- Behavior depends on `rotation_period` configuration:
  - **daily** (default): Replaced with current date in YYYY-MM-DD format (UTC)
  - **weekly**: Replaced with Monday of current week in YYYY-MM-DD format (UTC)
- Example (daily): `/data/logs/ops/alert-router.{date}.log` → `/data/logs/ops/alert-router.2025-11-06.log`
- Example (weekly): `/data/logs/file/{date}.log` → `/data/logs/file/2025-11-03.log` (for week Nov 3-9)

### Docker Volume Mounts (Least Privilege)
- **alert-router**: Read-only config/certs, write-only to logs/ops/
- **http-gateway**: Read-only config/certs/logs (for viewer), write-only to logs/ops/
- **file-logger**: Read-only config/certs, write-only to logs/ops/ + logs/file/ (EXCLUSIVE)

## Alert Payload Schema

**`docs/CLIENTNODE.md` is the only payload spec** -- required/optional fields, level/category enums and aliases, normalization, the `client` authorization rules, two-stage validation, response codes, routing overrides, `pathway_config`, and LED `display_config` values all live there and nowhere else (no copy here, in README, or in the skill: `skills/alert-manager/references/payload-reference.md` is a symlink to it). It is public: `GET /docs` serves it unauthenticated. Read it before touching payload handling, and update it with any payload change.

Gotchas verified against the code (2026-10-26), easy to reintroduce into docs:
- The LED pathway does **not** read top-level `display_config` or `metadata.led_zones`; only `pathway_config.<led pathway name>.zones` / `.display_config` steer a sign.
- `database_logger.py` stores the raw payload without normalization, and the `alerts` table CHECKs canonical `level`/`category` and a 2020-01-01..now+1d timestamp -- so aliased levels and millisecond timestamps are routed but never reach the DB/dashboard.
- `routing.exclude` is not implemented.

## User Access Control

**As of v2.0.0**, access is role-based (RBAC), backed by PostgreSQL (`sql/migrations/0002_identity.sql`: `users`, `roles`, `user_roles`, `role_scopes`, `auth_tokens`, `auth_audit`), not a fixed `admin`/`node`/`client` account **type**. Every account holds zero or more roles; every role carries zero or more scopes, each a `(client, permission)` pair where either side may be the wildcard `*`.

| Permission | Purpose |
|------------|---------|
| `logs:read` | View `/data/logs/file/{client}/**` for that client (or all clients if `client='*'`); drives dashboard/log-viewer visibility |
| `alerts:write` | Submit `POST /alert` as that client (or any client if `client='*'`) |
| `*` | Wildcard -- all permissions for the paired client |

One seeded system role, **`master_admin`**, holds a single `('*', '*')` scope: every permission, every client, including alerts/logs with a null/empty `client` (which no other role's `client='*'` scope can see). `ops/**` logs and flat (non-per-client) log files are `master_admin`-only regardless of any `logs:read` scope.

There is no per-account `rate_limit` field and no account expiry (`users.expires_at` was removed in v3.0.0 -- nothing could ever write it). Abuse-prevention is: **account lockout** (5 consecutive failed logins -> locked 900 seconds, applies to Basic auth too, cleared by `user-manager.py unlock`) and a **per-IP login throttle** (20 failed logins/10 minutes/IP -> `429`, independent of which username(s) were tried, and applied to `POST /login` only). See `docs/AUTH.md` for the full model and `tools/user-manager.py` for administration (`create-user`, `create-role`, `grant-role`, `add-scope`, `create-token`, `list-tokens`, `unlock`, ...).

**As of v3.0.0**, `must_change_password` is enforced: a browser session carrying the flag is redirected to `GET /change-password` until it is resolved (`src/gateway/auth/routes.py`). Basic/Bearer callers are never gated -- a service account rotates its credential via `tools/user-manager.py set-password`. `tools/reset-identity.py` destroys the entire identity store so it can be rebuilt from `db_migrate.py` + `init-admin`; it never touches `alerts` or `email_log`.

### Client Name Inheritance
- If a `POST /alert` payload doesn't include a `client` field (or it's empty), the system automatically inherits the authenticated account's own `default_client`
- Alerts are logged to `/data/logs/file/{client}/{date}.log`
- Override by specifying a different `client` in the payload
- **Breaking (v2.0.0):** the effective client (payload `client` else `default_client` else clientless `''`) must now be covered by an `alerts:write` scope the account holds; the clientless case additionally requires the `master_admin` role. A client the account has no scope for returns `403`. See `CLIENTNODE.md`.

## Pathway Configuration

### File Pathway (Basic)
```json
{
  "type": "file",
  "enabled": true,
  "path": "/data/logs/file/{date}.log",
  "format": "{timestamp} [{level}] [{category}] {source}: {title} - {message}",
  "create_directories": true,
  "append_mode": true,
  "rotation_period": "daily"
}
```

### File Pathway (Per-Client with Weekly Rotation)
```json
{
  "type": "file",
  "enabled": true,
  "path": "/data/logs/file/{date}.log",
  "format": "{timestamp} [{level}] [{category}] {source} [{client}]: {title} - {message}",
  "create_directories": true,
  "append_mode": true,
  "per_client_files": true,
  "client_path_template": "/data/logs/file/{client}/{date}.log",
  "rotation_period": "weekly"
}
```

**rotation_period** options:
- `"daily"` (default): One log file per day, rotates at midnight UTC
- `"weekly"`: One log file per week (Monday-Sunday), rotates at Monday midnight UTC

### File Pathway (Message Sanitization)

File pathways support message sanitization to maintain single-line log format by replacing newlines, carriage returns, and tabs. This prevents log format corruption when alert messages contain multiline text (e.g., SQL statements, stack traces).

```json
{
  "type": "file",
  "enabled": true,
  "path": "/data/logs/file/{date}.log",
  "format": "{timestamp} [{level}] [{category}] {source}: {title} - {message}",
  "sanitize_messages": true,
  "max_message_length": 500,
  "newline_replacement": " ↵ ",
  "create_directories": true,
  "append_mode": true
}
```

**Sanitization Options:**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `sanitize_messages` | boolean | `true` | Enable single-line message sanitization |
| `max_message_length` | integer | `500` | Maximum message length before truncation (0 = unlimited) |
| `newline_replacement` | string | `" ↵ "` | String to replace newlines with (visible indicator) |

**Sanitization Behavior:**
- Replaces `\r\n`, `\r`, and `\n` with the configured replacement string
- Replaces tabs (`\t`) with single space
- Collapses multiple consecutive spaces into one
- Truncates message field to `max_message_length` with `...` suffix
- Applies to `title`, `message`, and `source` fields
- Can be disabled by setting `"sanitize_messages": false`

**Note:** Sanitization only affects file pathway output. Email, database, and console pathways preserve full multiline messages.

### Email Pathway (O365 Graph API with MSAL)
```json
{
  "type": "email",
  "enabled": true,
  "from_address": "alerts@yourdomain.com",
  "to_addresses": ["admin@yourdomain.com"],
  "subject_template": "Alert: {title}",
  "html_format": true
}
```

**Note**: O365 credentials (tenant_id, client_id, client_secret) are loaded from environment variables. Set these in your .env file:
- `O365_TENANT_ID` - Azure tenant ID
- `O365_CLIENT_ID` - Azure app client ID
- `O365_CLIENT_SECRET` - Azure app client secret
- `EMAIL_FROM_ADDRESS` - Sender email address (optional, overrides config)

### Email Pathway (Storm Prevention)

Email pathways support storm prevention to aggregate multiple critical alerts arriving within a configurable time window into a single summary email. This prevents inbox flooding during system-wide failures.

```json
{
  "type": "email",
  "enabled": true,
  "from_address": "alerts@yourdomain.com",
  "to_addresses": ["admin@yourdomain.com"],
  "subject_template": "CRITICAL ALERT: {title}",
  "html_format": true,
  "level_filter": ["critical"],
  "storm_prevention": {
    "enabled": true,
    "window_seconds": 10,
    "levels": ["critical"],
    "aggregated_template": "emails/aggregated_alerts.html.j2"
  }
}
```

**Storm Prevention Options:**

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `enabled` | boolean | `false` | Enable storm prevention aggregation |
| `window_seconds` | integer | `10` | Rolling window duration before sending aggregated email |
| `levels` | array | `["critical"]` | Alert levels to aggregate (others sent immediately) |
| `aggregated_template` | string | `"emails/aggregated_alerts.html.j2"` | Jinja2 template for aggregated emails |

**Storm Prevention Behavior:**
- When a matching alert arrives, starts a rolling 10-second window
- Additional alerts within the window are queued and reset the timer
- After 10 seconds with no new alerts, sends ONE email:
  - **Single alert**: Normal email format (unchanged behavior)
  - **Multiple alerts**: Aggregated "STORM ALERT" summary with count and table
- On service shutdown, pending alerts are flushed immediately
- Each email pathway instance maintains independent aggregation state

**Available Templates:**
- `emails/alert_default.html.j2` / `emails/alert_default_aggregated.html.j2` - Dark-mode-aware default templates (single alert / aggregated storm), used automatically by any `email` pathway that doesn't set an explicit `template_file`/`aggregated_template` -- shared macro library at `emails/components/alert_macros.html.j2`
- `emails/aggregated_alerts.html.j2` - Generic storm alert template
- `emails/revysmc_aggregated.html.j2` - REVYSMC-branded storm alert template

**Send Tracking**: every Graph API send attempt (immediate and storm-aggregated, success and failure) is recorded by `pathways/email_log.py`'s `EmailLogWriter` into the `email_log` table (`sql/migrations/0003_email_log.sql`) -- fail-open (a tracking-DB outage never blocks a send; see `docs/DATABASE_INTEGRATION.md`). `status='accepted'` means Graph returned HTTP 202 (queued), not confirmed delivery.

### LED Sign Pathway (Multi-Zone)
```json
{
  "type": "led_sign",
  "enabled": true,
  "broker": "alert.d-t.pw",
  "port": 1883,
  "base_topic": "ledSign/{zone}/",
  "default_zone": "main",
  "zones": {
    "kitchen": {
      "enabled": true,
      "level_filter": ["critical", "warning", "info"],
      "category_filter": ["security", "system", "personal"],
      "format": "{title}: {message}",
      "max_message_length": 150
    }
  }
}
```

## Running, Profiles & Testing

This is a Python (3, no build step) multi-service application orchestrated by `docker-compose.yml`. All services run from a single image (`docker/Dockerfile`); each service overrides `command:` to run a different `src/*.py` entrypoint with `--config /data/config/docker.json`.

### Docker Compose Profiles
Services attach to the **external** Docker network `WebApps` (`docker network create WebApps` if it doesn't exist). Four services have **no profile and always start**: `mqtt-broker`, `alert-router` (the core consumer of `alerts/input`), and, as of the Phase 1 "Postgres becomes core" change, `postgres` and `database-logger` (the gateway's identity/RBAC store and the dashboard both have a hard Postgres dependency, so persistence can no longer be optional). Everything else is gated behind a profile:

| Profile | Services |
|---------|----------|
| `primary` | `file-logger`, `http-gateway` |
| `secondary` | `feed-aggregator`, `url-monitor` |
| `analytics` | `analytics-engine` |
| `deprecated` | `syslog-gateway`, `snmp-gateway` (do not enable) |

```bash
# Typical full stack (mqtt-broker/alert-router/postgres/database-logger always run):
docker-compose --profile primary --profile secondary --profile analytics up -d

# Rebuild after editing Python source (NO --no-cache needed unless deps changed).
# IMPORTANT: only `alert-router` declares a build context -- every other service
# reuses `image: alert-router:latest`. `docker-compose build http-gateway` prints
# "uses an image, skipping" and silently does nothing, so ALWAYS build alert-router:
docker-compose build alert-router && docker-compose up -d http-gateway
```
**Reminder:** per the Development Notes below, never run `docker`/`docker-compose` from the Bash tool — give the user the commands to run server-side.

### Running a service locally (no Docker)
From the repo root (entrypoints add `src/` to the path):
```bash
pip install -r requirements.txt
python3 src/alert_router.py --config data/config/test.json
gunicorn --chdir src --worker-class gthread -w 1 --threads 8 --timeout 60 --bind 0.0.0.0:8080 gateway.wsgi:app  # reads config via env (.env); needs GATEWAY_SECRET_KEY
python3 src/feed_aggregator.py --config data/config/test.json
```

### Tests
There **is** a pytest suite under `tests/` (~1000 tests, no live DB or broker needed -- Postgres/MQTT are mocked, and the handful of live-DB integration tests skip cleanly unless `AM_TEST_DSN` is set). pytest lives in `requirements-dev.txt`, not `requirements.txt`:
```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

Separately, `tools/tests/` holds integration shell scripts that hit a **running HTTP gateway** on `localhost:8080` (export `ALERT_USER`/`ALERT_PASSWORD` first -- they no longer hardcode credentials):
```bash
bash tools/tests/test_normalization.sh      # level/category alias + trim/lowercase
bash tools/tests/test_invalid_requests.sh   # validation rejection paths
bash tools/tests/test_log_viewer.sh         # /api/logs/* endpoints
```
Other helpers in `tools/` send live alerts (`send-http-alert.sh`, `send-mqtt-alert.sh`, `test-snmp-trap.sh`) and manage auth (`tools/user-manager.py` for gateway/HTTP identity, `mqtt-auth-manager.sh`, `snmp-auth-manager.sh`, `cert-manager.sh`).

## Service Configuration Files

- **data/config/docker.json** - Docker container configuration (pathway/routing config for `alert-router`/`file-logger`/etc.; the HTTP gateway itself is env-var-configured only -- see `src/gateway/config.py` -- and has no JSON config file or `auth` block)
- **data/config/main.json** - External/production configuration with TLS
- **data/config/test.json** - Local development configuration
- **examples/** - Example configurations (email, LED sign, pathway examples)
- **.env** - Environment variables for API keys and credentials, including `GATEWAY_SECRET_KEY` (create from .env.example)

## Service Health Monitoring

**Status Message** (published to `alerts/service/status` every 60 seconds):
```json
{
  "status": "running",
  "timestamp": "2024-01-15T10:30:00Z",
  "stats": {
    "alerts_processed": 42,
    "alerts_routed": 38,
    "errors": 0,
    "uptime_seconds": 3600,
    "last_alert": "2024-01-15T10:29:00Z",
    "loaded_pathways": ["console_alerts", "file_log", "led_sign", "email_alerts"],
    "enabled_pathways": ["console_alerts", "file_log", "led_sign"],
    "pathway_errors": {
      "email_alerts": 2
    }
  }
}
```

## HTTP Gateway Log Viewer

**Features (rewritten in v2.0.0 -- `src/gateway/logs/`, `src/static/js/log-viewer.js`):**
- Real-time streaming of log files (polls every `LOG_POLL_INTERVAL_MS`, default 10s)
- Recursive directory support (ops/, file/ subdirectories)
- Multiple log files with folder navigation
- Color-coded log levels (critical, warning, notice, info, debug)
- Client-side search (debounced, case-insensitive substring, `<mark>` highlighting, ANDed with level filters, live match counter) over every loaded line
- Progressive chunked backward-paging (`/api/logs/chunk`) with a load progress indicator -- **no server-side line-count truncation anymore** (the old 10,000-line cap is gone entirely)
- Pause/resume streaming, auto-scroll detection
- Access control by role/scope (`has_permission('logs:read', client)`) and `master_admin` for `ops/**`/flat files -- see "User Access Control" above

**API Endpoints:**
- `GET /api/logs/list` - Returns `{status, files: [...], user: {...}}`; `files` are relative paths (e.g., `["ops/alert-router.2025-11-06.log", "file/prod-web.2025-11-06.log"]`), filtered by the caller's authorization
- `GET /api/logs/tail?file={path}&position={offset}` - Forward, byte-aligned incremental read (binary mode, 1MB cap per call, never returns a partial line); `?stat=1` returns only `{"file_size": N}`
- `GET /api/logs/chunk?file={path}&end={offset}` - **New**: backward, byte-aligned page read (~1MB per call, start aligned to a line boundary); `413` if a single newline-free "line" longer than 50MB sits immediately before `end`

**{date} placeholder**: Supported in file paths, automatically replaced with current UTC date (YYYY-MM-DD format)

## Development Notes

- Use MQTT client v2 with callback_api_version set properly
- Timestamps are always communicated with Unix epoch format without decimals (one-second precision)
- Do not implement backwards compatibility unless explicitly asked (project is not live yet)
- Do not implement failsafes - throw critical error and quit on failure
- All time data is transmitted and saved as Unix epoch
- Update CLIENTNODE.md with any payload structural changes or new examples
- **Docker commands must be run manually server-side**
  - Code changes are NOT automatically reflected in running containers
  - After editing Python source files, containers must be rebuilt and restarted
  - Provide user with exact commands to run (e.g., `docker-compose build alert-router`, which is the only service with a build context -- see "Docker Compose Profiles" above)
  - Never execute `docker-compose`, `docker`, or container commands via Bash tool
  - User will run commands manually on the server where Docker is installed
  - For HTTP gateway UI changes, remind user to hard-refresh browser (Ctrl+Shift+R)

## Error Logging Levels

- **WARNING**: Client errors (invalid JSON, missing fields, wrong content-type)
- **ERROR**: Server errors (MQTT connection issues, unexpected exceptions)

## Version Management

The project uses centralized version management:

- **VERSION file**: Single source of truth at project root (e.g., `1.1.0`)
- **src/version.py**: Python module that reads from VERSION file
- **Usage**: Import `from version import __version__` in Python code
- **Versioning**: Follows Semantic Versioning (MAJOR.MINOR.PATCH)
- **Changelog**: All changes documented in CHANGELOG.md

**Increment Version:**
```bash
# Use the version bump script
./tools/bump-version.sh [major|minor|patch]

# Examples:
./tools/bump-version.sh patch   # 1.1.0 -> 1.1.1
./tools/bump-version.sh minor   # 1.1.0 -> 1.2.0
./tools/bump-version.sh major   # 1.1.0 -> 2.0.0
```

**After version bump:**
1. Update CHANGELOG.md with new version section
2. Commit changes with message like "Bump version to X.Y.Z"
3. Tag the commit: `git tag vX.Y.Z`
4. Push tags: `git push --tags`

## Documentation References

All reference docs live in `docs/` (except `README.md`, `CHANGELOG.md` at root). `docs/CLIENTNODE.md` is the integration source-of-truth — keep it in sync with payload changes.

**Integration & payload**
- **CLIENTNODE.md** - The single payload specification and client integration guide (public, served at `GET /docs`)

**Gateways & transports**
- **HTTP_GATEWAY.md** - HTTP API reference (sessions, `api`/`logs`/`dashboard`/`admin` blueprints)
- **AUTH.md** - Gateway auth model (sessions, RBAC, argon2id, CLI) and MQTT certificate management
- **MQTT_AUTH.md** - MQTT authentication (username/password and certificates)
- **SNMP_CLIENT_MAPPING.md** - SNMPv3 user management and client assignment (DEPRECATED, reference only)
- **SNMP_INTEGRATION.md** - Device configuration, OID mappings, and SNMP gateway setup (DEPRECATED, reference only)
- **SNMPV3_SPECIFICATION.md** - Technical SNMPv3 protocol details and MIB reference (DEPRECATED, reference only)
- **SYSLOG_INTEGRATION.md** - Syslog gateway (DEPRECATED, reference only)

**Pathways, data & analytics**
- **BETABRITE.md** - Complete LED sign capabilities reference
- **ESP32_BETABRITE_IMPLEMENTATION.md** - ESP32 firmware/device-side LED sign implementation
- **DATABASE_INTEGRATION.md** - PostgreSQL schema, partitions, and database-logger service
- **ANALYTICS.md** / **ANALYTICS_QUICKSTART.md** - Analytics engine setup, schedulers, custom analyzers
- **TEMPLATES_GUIDE.md** - Jinja2 email template system (`src/templates/emails/`)
- **TIMEZONE.md** - Timezone handling, Unix epoch storage, and display conversion

**Operations & design**
- **DOCKER.md** - Docker deployment, compose profiles, and volume layout
- **DEV_GUIDE.md** - Developer setup and contribution guide
- **THEME_SPECIFICATION.md** - UI/UX design system, color palette, logo specifications, brand guidelines
- **ALERTING_SYSTEM_PLAN.md** - Original system design/planning document (historical context)
- **src/feeds/README.md** - Feed aggregator architecture and how to add feeds
