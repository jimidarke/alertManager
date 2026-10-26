# HTTP to MQTT Gateway

REST API + web application service that accepts HTTP alert JSON payloads and forwards them to the MQTT broker, plus a session-authenticated dashboard, log viewer, and user-administration UI. Provides a simple HTTP interface for devices and services that don't have native MQTT capabilities, and a browser UI for humans.

## Overview

The HTTP Gateway acts as a bridge between HTTP-based systems and the MQTT-based Alert Manager infrastructure, and (as of v2.0.0) as the browser-facing front end for the whole system. It is implemented as `src/gateway/` (a Flask app-factory package, `create_app()` in `src/gateway/__init__.py`), organized into five blueprints:

- **`auth`** (`src/gateway/auth/`): `/login`, `/logout`, `/api/whoami` -- session-cookie login, CSRF protection, per-IP login throttle, and the `CredentialCache` that fronts Basic/Bearer verification for high-frequency traffic
- **`api`** (`src/gateway/api/`): `/health`, `/alert`, `/test`, `/docs`, `/docs/<filename>` -- the machine/integration-facing surface, unchanged in spirit from the pre-v2.0.0 gateway
- **`logs`** (`src/gateway/logs/`): `/logs`, `/api/logs/list`, `/api/logs/tail`, `/api/logs/chunk` -- the log viewer, rewritten for binary/newline-aligned reads and client-side search
- **`dashboard`** (`src/gateway/dashboard/`): `/dashboard`, `/api/dashboard/summary`, `/api/dashboard/groups` -- new in v2.0.0 (grouped view replaced `/api/dashboard/critical`)
- **`admin`** (`src/gateway/admin/`): `/admin/users` and sub-routes -- new in v2.0.0, `master_admin`-only user/role/scope administration

```
[HTTP Clients / Browsers] → nginx-proxy (TLS termination, alert.d-t.pw:443)
                                    → HTTP Gateway (gunicorn, container-internal :8080)
                                            → MQTT Broker (internal, unauth) → Alert Router
                                            → PostgreSQL (dashboard queries, identity/RBAC)
```

**Identity/RBAC** is backed by PostgreSQL (`sql/migrations/0002_identity.sql`) via `src/userstore/`'s `UserStore` -- there is no more `data/config/http-gateway/users.json`/`tokens.json`. See `docs/AUTH.md` for the full authentication/authorization model and `tools/user-manager.py` for account administration.

## Docker Deployment

### Start HTTP Gateway Service

```bash
# Start core services (router + broker, always on) plus the HTTP gateway/file-logger profile:
docker-compose --profile primary up -d

# Typical full stack:
docker-compose --profile primary --profile secondary --profile analytics up -d

# Check service status
docker-compose ps
docker logs alert-http-gateway
```

### Service Configuration

- **Container**: `alert-http-gateway`
- **Port**: `8080` (container-network only -- **no host port publish as of v2.0.0**; all external traffic goes through the `nginx-proxy` reverse proxy on the `WebApps` network at `https://alert.d-t.pw`, see `DOCKER.md`). Direct `http://<host>:8080` access from outside the Docker host no longer works.
- **Image**: Reuses `alert-router:latest` image
- **Command**: `gunicorn --chdir /app/src --worker-class gthread -w 1 --threads 8 --timeout 60 --bind 0.0.0.0:8080 gateway.wsgi:app`
- **Dependencies**: `alert-router`, `mqtt-broker` (started), `postgres` (**must be healthy** -- the gateway's identity store and dashboard are hard Postgres dependencies)
- **Required environment**: `GATEWAY_SECRET_KEY` (signs/encrypts the session cookie; the process refuses to start -- `sys.exit(1)` -- if unset or shorter than 32 characters). Generate with `openssl rand -hex 32`.

## API Endpoints

### Authentication

Three auth policies apply, per blueprint (see `docs/AUTH.md` for the full model):

| Blueprint | Routes | Accepted auth |
|-----------|--------|----------------|
| `api` | `/health`\*, `/alert`, `/test`, `/docs`, `/docs/<f>`\* | Basic Auth or Bearer token only (no session cookie accepted) |
| `logs` | `/logs`, `/api/logs/list`, `/api/logs/tail`, `/api/logs/chunk` | Session cookie **or** Basic/Bearer -- keeps existing `curl -u` / shell-test workflows working alongside the browser UI |
| `dashboard` | `/dashboard`, `/api/dashboard/*` | Session cookie only |
| `admin` | `/admin/users*` | Session cookie only, and the account must hold the `master_admin` role |

\* `/health` requires no auth at all; `/docs/<filename>` is intentionally unauthenticated (ported verbatim from the original gateway).

```bash
# Basic Authentication
curl -u username:password https://alert.d-t.pw/alert

# Bearer Token Authentication
curl -H "Authorization: Bearer amt_..." https://alert.d-t.pw/alert
```

An anonymous request to a session-allowing browser route (e.g. `GET /dashboard` with no cookie and no `Authorization` header) gets a `302` redirect to `/login?next=...`; a request to `/api/*` in the same situation, or any request presenting an invalid `Authorization` header, always gets a `401` challenge instead -- never an HTML redirect a script/device can't do anything with.

**User Management**: `tools/user-manager.py` (run via `docker exec -it alert-http-gateway python3 tools/user-manager.py <command>`) is the canonical CLI for account/role/scope/token administration -- see `docs/AUTH.md` and the tool's own `--help` output.

### Health Check

```bash
GET /health
```

**Authentication**: Not required

**Response:**
```json
{
  "status": "healthy",
  "service": "http-gateway",
  "version": "2.0.0",
  "timestamp": "2026-07-21T10:30:00Z",
  "mqtt_connected": true,
  "mqtt": {"connected": true},
  "db": {"status": "ok"}
}
```

Only MQTT connectivity affects the HTTP status code (`503` if disconnected). A Postgres blip is reported in `db.status`/`db.detail` but never flips the overall status -- the gateway must not restart-loop on a transient DB hiccup.

### Send Alert

```bash
POST /alert
Content-Type: application/json
Authorization: Basic base64(username:password)
```

**Authentication**: Required (Basic or Bearer)

**Authorization (breaking as of v2.0.0)**: the effective client (payload `client`, else the account's `default_client`, else clientless `''`) must be covered by an `alerts:write` scope the account holds; the clientless case additionally requires the `master_admin` role. See `CLIENTNODE.md` for the full breaking-change note.

**Required Payload:**
```json
{
  "timestamp": 1705315800,
  "level": "warning",
  "category": "system",
  "title": "Server High CPU",
  "message": "CPU usage at 95% for 5 minutes",
  "source": "monitoring-system"
}
```

**Success Response (200):**
```json
{
  "status": "success",
  "message": "Alert forwarded to MQTT broker",
  "timestamp": "2026-07-21T10:30:00Z",
  "alert_id": "monitoring-system-1642248600"
}
```

**Error Response (400) -- missing fields:**
```json
{
  "error": "Missing required fields: timestamp, level",
  "required_fields": ["timestamp", "level", "category", "title", "message", "source"],
  "timestamp": "2026-07-21T10:30:00Z"
}
```

**Error Response (403) -- client not authorized:**
```json
{"status": "error", "error": "forbidden", "timestamp": "2026-07-21T10:30:00Z"}
```

**Error Response (503) -- credential verification temporarily unavailable** (the in-memory `CredentialCache` had no fresh/stale answer and the live DB check itself failed -- see `docs/AUTH.md`):
```json
{"status": "error", "error": "Credential verification temporarily unavailable", "timestamp": "2026-07-21T10:30:00Z"}
```

### Test Endpoint

```bash
POST /test
Authorization: Basic base64(username:password)
```

**Authentication**: Required (any authenticated Basic/Bearer principal -- no `alerts:write` scope check; unlike `/alert` this route is unchanged by the v2.0.0 authorization rework)

Generates and sends a test alert. No payload required.

**Response:**
```json
{
  "status": "success",
  "message": "Test alert sent successfully",
  "alert": {
    "timestamp": 1705315800,
    "level": "info",
    "category": "system",
    "title": "HTTP Gateway Test",
    "message": "Test alert from HTTP gateway",
    "source": "http-gateway-test"
  }
}
```

### Documentation Endpoints

```bash
GET /docs
GET /docs/<filename>
```

`GET /docs` requires auth (Basic/Bearer) and serves `CLIENTNODE.md` as `text/markdown`. `GET /docs/<filename>` is deliberately unauthenticated (ported verbatim from the original gateway) and serves an arbitrary file from `/data/docs`, with directory-traversal protection.

## Browser-Facing Pages

### Login / Logout

```bash
GET  /login              # Renders the login form (redirects to /dashboard if already logged in)
POST /login               # username, password, next, csrf_token form fields
POST /logout               # Destroys the session; requires a valid CSRF token
```

A failed login always renders the same generic "Invalid username or password" message, regardless of whether the cause was a nonexistent username, wrong password, disabled account, or an active lockout -- see `docs/AUTH.md`. Five consecutive failed attempts against one account locks it for 900 seconds (clear it with `user-manager.py unlock`); 20 failed attempts from one source IP within 10 minutes returns `429` regardless of which username(s) were tried.

### Change Password

```bash
GET  /change-password     # Renders the form (redirects to /login if not authenticated)
POST /change-password     # current_password, new_password, confirm_password, csrf_token
```

Session auth only. As of v3.0.0, an account with `must_change_password` set is redirected here from every other page by a `before_request` gate until the change is made -- only `/change-password`, `/login`, `/logout`, `/api/whoami` and static assets are exempt. Basic/Bearer callers are never gated: a service account cannot use an HTML form, so `POST /alert` keeps working and the credential is rotated by an operator via `tools/user-manager.py set-password`.

The current password is re-verified through the same `verify_login()` path as a normal login, so wrong guesses count toward account lockout. The new password must be at least 12 characters, match its confirmation, and differ from the current one.

### `GET /api/whoami`

```bash
curl -b cookies.txt https://alert.d-t.pw/api/whoami
# or
curl -u username:password https://alert.d-t.pw/api/whoami
```

Accepts session, Basic, or Bearer. Returns `401` JSON if unauthenticated (never an HTML redirect, even though session is an allowed method for this route).

```json
{
  "user_id": 3,
  "username": "jimi",
  "auth_method": "session",
  "default_client": null,
  "is_master_admin": true,
  "must_change_password": false
}
```

### Dashboard

```bash
GET /dashboard                        # HTML page
GET /api/dashboard/summary?window=24h
GET /api/dashboard/groups?window=24h&levels=critical,warning
```

**Authentication**: Session only.

Five level-count cards (critical/warning/notice/info/debug) that double as multi-select level filters (default: critical only), plus the selected time window's alerts grouped **client -> source** as collapsible sections. Each source shows its latest 30 alerts at the selected levels ("showing 30 of N" beyond that -- the log viewer is the place for deep history). Every client/source active in the window is listed with its per-level counts even when nothing matches the selected levels (collapsed and dimmed). Clients and sources are alphabetical; the clientless group sorts last. The level selection and collapse state persist per browser (localStorage).

The time window is one of `1h`, `6h`, `12h`, `24h`, `7d`, `14d`, `30d`, or `all` (a 90-day cap, not literally unbounded). `levels` is a comma-separated subset of the five levels (unknown -> `400`); omitted it defaults to `critical`, and an empty `levels=` returns group counts with no alert rows. Results are scoped server-side to each principal's visible clients for the `logs:read` permission -- there is no client-selecting query parameter; a row with a null/empty `client` is visible to `master_admin` only (as the `""` group). A Postgres outage returns `503` with a `{"status": "error", "error": "database unavailable", ...}` body from the API calls, and a warning banner on the page itself.

**`/api/dashboard/summary` response:**
```json
{
  "counts": {"critical": 2, "warning": 5, "notice": 0, "info": 40, "debug": 0},
  "total": 47,
  "since": 1705230600,
  "scope": ["revysmc", "device-monitor"],
  "generated_at": 1705317000
}
```

**`/api/dashboard/groups` response:**
```json
{
  "clients": [
    {"client": "revysmc",
     "counts": {"critical": 1, "warning": 3, "notice": 0, "info": 40, "debug": 0},
     "last_seen": 1705315800, "matched": 4,
     "sources": [
       {"source": "web-server",
        "counts": {"critical": 1, "warning": 3, "notice": 0, "info": 40, "debug": 0},
        "last_seen": 1705315800, "matched": 4,
        "alerts": [
          {"id": 123, "timestamp": 1705315800, "level": "critical", "category": "security",
           "title": "Unauthorized Access", "message": "Failed login attempt detected",
           "source": "web-server", "client": "revysmc", "metadata": {}, "ingestion_source": "http"}
        ]}
     ]}
  ],
  "levels": ["critical", "warning"], "per_group": 30,
  "since": 1705230600, "scope": ["revysmc", "device-monitor"], "generated_at": 1705317000
}
```

`counts`/`last_seen` cover every level in the window; `matched` is the count at the selected `levels` (it can exceed `len(alerts)`, which is capped at `per_group`).

### Log Viewer Interface

```bash
GET /logs
```

**Authentication**: Session or Basic/Bearer.

Serves the (v2.0.0-rewritten) interactive log viewer:
- Live tail (10s poll interval, configurable via `LOG_POLL_INTERVAL_MS`), pause/resume, auto-scroll detection
- Level filtering (critical, warning, notice, info, debug) and folder/weekly-file navigation, unchanged from the previous viewer
- **New**: client-side search over every loaded line (debounced, case-insensitive substring match, `<mark>` highlighting, ANDed with the active level filters, live match counter)
- **New**: chunked backward-paging via `/api/logs/chunk` -- the entire file loads progressively (with a progress indicator) instead of the old server-side truncation to the most recent 10,000 lines. There is no server-side line-count cap anymore; a single "line" (run of bytes with no newline) larger than 50MB immediately before a requested chunk boundary is refused with `413` rather than read into memory.

### Log API Endpoints

```bash
GET /api/logs/list
```

**Authentication**: Required (session or Basic/Bearer).

Returns JSON array of available log files, filtered by the caller's authorization: `ops/**` and flat files are `master_admin`-only; `file/{client}/**` requires a `logs:read` scope for that client.

**Response:**
```json
{
  "status": "success",
  "files": ["file/mynode/2025-12-22.log", "file/mynode/2025-12-21.log"],
  "timestamp": "2026-07-21T10:30:00Z",
  "user": {"username": "jimi", "is_master_admin": true, "default_client": null}
}
```

```bash
GET /api/logs/tail?file=<path>&position=<byte_offset>
GET /api/logs/tail?file=<path>&stat=1
```

**Authentication**: Required.

Forward, byte-aligned incremental read (binary mode, capped at 1MB per call; `position`/the returned `position` are always `0` or immediately after a `\n` byte -- never mid-line, even under a concurrently-appending writer). `?stat=1` returns only `{"file_size": N}` with no read at all, so a client can plan its initial backward-paging sequence.

**Response:**
```json
{"status": "success", "lines": ["2025-12-22 10:30:00 [info] Alert processed"], "position": 1234, "file_size": 5000, "eof": false}
```

```bash
GET /api/logs/chunk?file=<path>&end=<byte_offset>
```

**New in v2.0.0.** **Authentication**: Required.

Backward, byte-aligned page read ending at `end` (defaults to the file's current size), targeting ~1MB per response, START-aligned to a line boundary. `/api/logs/tail` and `/api/logs/chunk` share the exact same boundary definition, so a client can page backward via `/api/logs/chunk` until `eof: true`, then switch to forward polling via `/api/logs/tail` using the last chunk's `start` as its first `position` -- the two can never overlap or leave a gap.

**Response:**
```json
{"status": "success", "lines": ["..."], "start": 4000, "end": 5000, "file_size": 5000, "eof": false}
```

**413 response** (a single newline-free "line" larger than 50MB sits immediately before `end` -- treat as a hard, non-retryable refusal, not a transient error):
```json
{"status": "error", "message": "Log entry too large to read in one chunk", "error_detail": "..."}
```

### Admin UI

```bash
GET  /admin/users
GET  /admin/clients
GET  /admin/clients/new
POST /admin/clients/new
POST /admin/clients/<name>/disable
POST /admin/clients/<name>/enable
POST /admin/clients/<name>/delete
GET  /admin/users/new
POST /admin/users/new
GET  /admin/users/<username>/edit
POST /admin/users/<username>/edit
POST /admin/users/<username>/disable
POST /admin/users/<username>/enable
POST /admin/users/<username>/delete
POST /admin/users/<username>/reset-password
POST /admin/users/<username>/tokens/new
POST /admin/users/<username>/tokens/<token_id>/revoke
```

**Authentication**: Session only, and the account must hold the `master_admin` role (`403` otherwise). Every mutating route requires a valid CSRF token (a POST without one is rejected with `400`).

Server-rendered user/role/scope administration -- the same `UserStore` methods `tools/user-manager.py` calls, exercised from a web form instead of argv. Username is fixed at creation (`^[a-z0-9_-]{3,32}$`); roles are assigned via checkboxes; per-user ad-hoc `(client, permission)` scopes are entered as repeatable rows and stored under an auto-managed "personal role" (`user:<username>`) that never appears as a general-purpose role for anyone else. Passwords are always server-generated (never operator-chosen) and shown exactly once on a `Cache-Control: no-store` result page. Two guard rails prevent locking yourself out: you cannot disable your own account, and you cannot remove your own `master_admin` role. Delete is a second step after disable: `POST .../delete` (users and clients) returns `400` unless the account/client is already disabled, and an admin cannot delete their own account. Deleting a user also removes their tokens and personal scope role; deleting a client removes only its registry entry. Alerts, logs, and audit history are never touched.

The New User form also has a "Service account" checkbox (a client-side convenience that defaults "Require password change" off and "Issue a Bearer token" on -- nothing about it is stored) and an "Issue a Bearer token" checkbox + name field, so a machine/API account can get both its password and its first token in one step. An account's edit page lists every token it holds (name/status/created/last used) with a Revoke button per active token, plus a small form to issue additional tokens later. Tokens issued through the web UI always have `expires_at` unset (never expire) -- there's no expiry-picker control; `POST .../tokens/<token_id>/revoke` verifies the token actually belongs to the account named in the URL before revoking it, the same class of check as the disable/edit guard rails above.

## Usage Examples

### cURL Commands

```bash
# Health check (no auth required)
curl -X GET https://alert.d-t.pw/health

# Send alert with authentication
curl -X POST https://alert.d-t.pw/alert \
  -u username:password \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "critical",
    "category": "security",
    "title": "Unauthorized Access",
    "message": "Failed login attempt detected",
    "source": "web-server"
  }'

# Test alert with authentication
curl -X POST https://alert.d-t.pw/test -u username:password
```

### Python Client

```python
import requests
import time

# Send alert via HTTP gateway with authentication
def send_alert(level, category, title, message, source, username, password, metadata=None):
    url = "https://alert.d-t.pw/alert"

    payload = {
        "timestamp": int(time.time()),
        "level": level,
        "category": category,
        "title": title,
        "message": message,
        "source": source
    }

    if metadata:
        payload["metadata"] = metadata

    response = requests.post(url, json=payload, auth=(username, password))
    return response.json()

# Example usage
result = send_alert(
    level="warning",
    category="system",
    title="Disk Space Low",
    message="Only 15% disk space remaining",
    source="monitoring-script",
    username="device-monitor",
    password="your-password",
    metadata={"server": "web01", "disk": "/var"}
)

print(f"Status: {result['status']}")
```

### JavaScript/Node.js Client

```javascript
const axios = require('axios');

async function sendAlert(level, category, title, message, source, username, password, metadata = null) {
    const url = 'https://alert.d-t.pw/alert';

    const payload = {
        timestamp: Math.floor(Date.now() / 1000),
        level,
        category,
        title,
        message,
        source,
        ...(metadata && { metadata })
    };

    try {
        const response = await axios.post(url, payload, {
            headers: { 'Content-Type': 'application/json' },
            auth: { username, password }
        });
        return response.data;
    } catch (error) {
        throw new Error(`HTTP Gateway error: ${error.response?.data?.error || error.message}`);
    }
}

// Example usage
sendAlert('info', 'automation', 'Task Complete', 'Backup completed successfully', 'backup-script')
    .then(result => console.log('Alert sent:', result.alert_id))
    .catch(error => console.error('Error:', error.message));
```

### PowerShell Client

```powershell
# Send alert via HTTP gateway
function Send-Alert {
    param(
        [string]$Level,
        [string]$Category,
        [string]$Title,
        [string]$Message,
        [string]$Source,
        [string]$Username,
        [string]$Password,
        [hashtable]$Metadata = $null
    )

    $payload = @{
        timestamp = [int][double]::Parse((Get-Date -UFormat %s))
        level = $Level
        category = $Category
        title = $Title
        message = $Message
        source = $Source
    }

    if ($Metadata) {
        $payload.metadata = $Metadata
    }

    $cred = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${Username}:${Password}"))
    $headers = @{ Authorization = "Basic $cred" }

    $response = Invoke-RestMethod -Uri "https://alert.d-t.pw/alert" -Method POST -Body ($payload | ConvertTo-Json) -ContentType "application/json" -Headers $headers
    return $response
}

# Example usage
Send-Alert -Level "critical" -Category "system" -Title "Service Down" -Message "IIS service has stopped" -Source "windows-monitoring"
```

## Integration Patterns

### Home Assistant

```yaml
# configuration.yaml
rest_command:
  send_alert:
    url: "https://alert.d-t.pw/alert"
    method: POST
    headers:
      Content-Type: "application/json"
      Authorization: "Basic {{ 'username:password' | base64_encode }}"
    payload: >
      {
        "timestamp": {{ as_timestamp(now()) | int }},
        "level": "{{ level | default('info') }}",
        "category": "{{ category | default('automation') }}",
        "title": "{{ title }}",
        "message": "{{ message }}",
        "source": "home-assistant"
      }

# Automation example
automation:
  - alias: "Motion Alert via HTTP"
    trigger:
      platform: state
      entity_id: binary_sensor.motion_detected
      to: 'on'
    action:
      service: rest_command.send_alert
      data:
        level: "warning"
        category: "security"
        title: "Motion Detected"
        message: "Motion sensor triggered in {{ trigger.to_state.attributes.friendly_name }}"
```

### Prometheus AlertManager

```yaml
# alertmanager.yml
global:
  resolve_timeout: 5m

route:
  group_by: ['alertname']
  group_wait: 10s
  group_interval: 10s
  repeat_interval: 1h
  receiver: 'alert-manager-http'

receivers:
- name: 'alert-manager-http'
  webhook_configs:
  - url: 'https://alert.d-t.pw/alert'
    http_config:
      basic_auth:
        username: ''
        password: ''
    send_resolved: false

# Alert transformation would need a webhook proxy to convert Prometheus format to Alert Manager format
```

### Nagios/Icinga Integration

```bash
#!/bin/bash
# nagios-alert-webhook.sh

LEVEL="$1"      # critical, warning, info
CATEGORY="$2"   # system, network, application
TITLE="$3"      # Alert title
MESSAGE="$4"    # Alert message
SOURCE="nagios"
USERNAME="nagios-node"
PASSWORD="your-password"

curl -X POST https://alert.d-t.pw/alert \
  -u "$USERNAME:$PASSWORD" \
  -H "Content-Type: application/json" \
  -d "{
    \"timestamp\": $(date +%s),
    \"level\": \"$LEVEL\",
    \"category\": \"$CATEGORY\",
    \"title\": \"$TITLE\",
    \"message\": \"$MESSAGE\",
    \"source\": \"$SOURCE\",
    \"metadata\": {
      \"hostname\": \"$(hostname)\",
      \"environment\": \"production\"
    }
  }"
```

## Security Considerations

### Network Security

TLS termination and host access are handled by the `nginx-proxy` reverse proxy container on the shared `WebApps` Docker network (see `DOCKER.md`); the gateway container itself no longer publishes any port to the Docker host. All external traffic must arrive at `https://alert.d-t.pw`.

### Authentication & Authorization

See `docs/AUTH.md` for the full model: session cookies (browser), Basic/Bearer (scripts/devices), argon2id password hashing, role/scope-based authorization, account lockout, and per-IP login throttling.

### Rate Limiting

There is no general per-request API rate limiting in v2.0.0 (this was explicitly deferred -- the old per-user `rate_limit` field was never enforced and has been dropped from the schema entirely; account lockout + the per-IP login throttle cover the abuse-prevention intent for the login path specifically). See the "Deferred" note in `CHANGELOG.md`'s `[2.0.0]` entry.

## Monitoring & Logging

### Service Health

```bash
# Check service status
curl -f https://alert.d-t.pw/health

# Monitor logs
docker logs -f alert-http-gateway

# View metrics
docker stats alert-http-gateway
```

### Gateway Metadata

All alerts forwarded through `POST /alert` include additional metadata:

```json
{
  "timestamp": 1705315800,
  "level": "info",
  "category": "system",
  "title": "Sample Alert",
  "message": "Test message",
  "source": "external-system",
  "_gateway": {
    "source": "http-gateway",
    "received_at": 1705315801,
    "client_ip": "192.168.1.100",
    "user_agent": "curl/7.68.0",
    "authenticated_user": "my-node",
    "user_type": "user"
  }
}
```

`user_type` is `"master_admin"` if the authenticated principal holds the `master_admin` role, `"user"` otherwise -- it reflects the RBAC model, not the retired `admin`/`node`/`client` account types. `received_at` is a Unix epoch integer, per this project's timestamp convention.

## Troubleshooting

### Common Issues

#### Gateway Not Starting

```bash
# Check logs
docker logs alert-http-gateway

# Common fatal-startup causes:
# - GATEWAY_SECRET_KEY unset or under 32 characters -> the process exits immediately with a clear stderr message
# - Postgres unreachable at boot -> gateway/db.py's connection pool fails to initialize
# - Initial MQTT connect failure -> logged CRITICAL, sys.exit(1)
```

#### Alerts Not Forwarding

```bash
# Test HTTP gateway directly (from inside the container network)
docker exec alert-http-gateway curl -X POST http://localhost:8080/test -u username:password

# Check MQTT broker connectivity
docker exec alert-http-gateway python3 -c "
import paho.mqtt.client as mqtt
client = mqtt.Client()
client.connect('mqtt-broker', 1883)
result = client.publish('test', 'hello')
print('Publish result:', result.rc)
"
```

#### 403 Forbidden on `POST /alert`

As of v2.0.0, this means the account authenticated successfully but does not hold an `alerts:write` scope for the effective client (or is submitting a clientless alert without being `master_admin`). Grant the missing scope:

```bash
docker exec -it alert-http-gateway python3 tools/user-manager.py add-scope <role> <client> alerts:write
docker exec -it alert-http-gateway python3 tools/user-manager.py grant-role <username> <role>
```

### Performance Tuning

```yaml
# docker-compose.yml - http-gateway's gunicorn command already runs 1 worker x 8 gthread
# threads (docs/OVERHAUL_PLAN.md 4.9 -- a single process keeps the in-memory
# CredentialCache/IPThrottle/MQTT client state coherent). Increasing --workers
# beyond 1 would break that invariant; scale threads or add compose resource
# limits instead if needed:
http-gateway:
  # ... existing config
  deploy:
    resources:
      limits:
        memory: 256M
        cpus: '0.5'
      reservations:
        memory: 128M
        cpus: '0.25'
```

The HTTP Gateway provides a simple, reliable bridge between HTTP-based systems and the secure MQTT Alert Manager infrastructure, alongside a session-authenticated dashboard, log viewer, and admin UI for humans, enabling broader integration possibilities while maintaining the security and performance benefits of the core MQTT architecture.
