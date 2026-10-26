# Alert Manager

![Version](https://img.shields.io/badge/version-3.4.0-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)

A self-hosted, centralized alerting system. Clients submit standardized JSON alerts over HTTPS (`POST /alert`); configurable rules route each alert to output pathways — files, email (O365 Graph API with storm prevention), BetaBrite LED signs, console, and MQTT — with everything logged to PostgreSQL. Includes a web dashboard and log viewer, role-based access control, a scheduled analytics/reporting engine, and a typed Python SDK.

**[Changelog](CHANGELOG.md)** | **[Documentation](docs/)** | **[Python SDK](sdk/python/)** | **[Quick Start](#quick-start)**

## 📁 Project Structure

```
alertManager/
├── src/                       # Service entrypoints and application code
│   ├── alert_router.py        # Core router: consumes alerts, dispatches to pathways
│   ├── gateway/               # HTTP gateway (Flask app-factory: api/auth/logs/dashboard/admin)
│   ├── database_logger.py     # PostgreSQL alert logging service
│   ├── feed_aggregator.py     # External content feeds -> alerts (RSS, jokes, facts, trivia)
│   ├── analytics_engine.py    # Scheduled log analysis + email reports
│   ├── userstore/             # Identity store (argon2id, roles, scopes, tokens)
│   └── templates/emails/      # Jinja2 email templates
├── pathways/                  # Output pathway implementations (file, email, LED, console, MQTT)
├── sdk/python/                # Python client SDK (alertmanager-sdk)
├── sql/                       # PostgreSQL schema and migrations
├── data/config/               # Service configuration (docker.json, main.json, test.json)
├── docker/                    # Dockerfile (single image shared by all services)
├── docker-compose.yml         # Service orchestration with profiles
├── tools/                     # Admin CLIs, test senders, auth managers
├── tests/                     # pytest suite (~1000 tests, no live DB/broker needed)
├── examples/                  # Example pathway/analytics configurations
└── docs/                      # Full documentation (see below)
```

## Key Features

- **🎯 Single Entry Point**: All alerts are submitted over HTTPS to `POST /alert` (the sole supported ingestion path as of v2.0.2)
- **🔀 Intelligent Routing**: First-match rules on level, category, source, and client, with per-alert overrides
- **🎨 Rich LED Display**: BetaBrite integration with 12 animation modes, special effects, colors, and multi-zone targeting
- **📤 Multi-Channel Output**: File logging, email (O365 Graph API), LED signs, console, and arbitrary MQTT topics
- **🔐 Role-Based Access Control**: PostgreSQL-backed users, roles, and `(client, permission)` scopes; argon2id passwords, Bearer tokens, sessions with CSRF protection
- **📈 Web UI**: Dashboard with alert summaries plus a streaming log viewer with search and access control
- **📊 Analytics Engine**: Scheduled log analysis with professional HTML email reports
- **🗄️ Database Logging**: Every alert recorded to partitioned PostgreSQL tables
- **🐍 Python SDK**: Client-side validation, typed exceptions, and a tested retry policy

## System Architecture

```
[Any client] --HTTPS--> [HTTP Gateway] --> [Local MQTT Broker] --> [Alert Router] --> [Output Pathways]
              POST /alert   443 only        alerts/input (internal)                          |
                                            (port 1883, unauth,                   alerts/service/status
                                             container network only)

[ESP32 LED signs] <--MQTTS--> broker :42690   (device bus ONLY -- not alert ingestion)
```

### Access Methods

**HTTP Gateway** (port 443 via `nginx-proxy`) -- the only way to submit an alert:
- **Security**: HTTPS only. HTTP Basic Auth or Bearer token for `POST /alert`; session cookie for browser pages. The gateway container publishes no host port -- all traffic goes through the TLS reverse proxy.
- **Use Cases**: every integration -- scripts, services, devices, the web UI
- **Endpoint**: `POST /alert` (see [docs/CLIENTNODE.md](docs/CLIENTNODE.md) for the wire contract)

**MQTT Endpoint** (port 42690, TLS) -- **not** an alert-ingestion path:
- **Scope**: ESP32 BetaBrite LED sign devices only (display push + telemetry). The internal `alerts/input` topic is service-to-service plumbing and is not ACL'd for external publish.
- **Security**: TLS 1.2 with username/password authentication (ESP32-optimized), QoS 1

### Alert Flow
1. **Submission**: client `POST /alert` over HTTPS with Basic/Bearer credentials
2. **Authorization**: the effective client is resolved and checked against the account's `alerts:write` scopes
3. **Local Processing**: the gateway republishes to the internal broker; Alert Router consumes `alerts/input`
4. **Intelligent Routing**: rules-based distribution to output pathways
5. **Multi-Channel Output**: LED signs, email, files, console, PostgreSQL

## Quick Start

### Docker Deployment (Recommended)

```bash
# Clone repository
git clone https://github.com/jimidarke/alertManager.git
cd alertManager

# Create the shared Docker network and your env file
docker network create WebApps
cp .env.example .env   # set GATEWAY_SECRET_KEY etc.

# Start core services (mqtt-broker, alert-router, postgres, database-logger --
# these have no profile and always start)
docker-compose up -d

# Typical full stack (file-logger + http-gateway, feeds, analytics)
docker-compose --profile primary --profile secondary --profile analytics up -d
```

Profiles are `primary` (file-logger, http-gateway), `secondary` (feed-aggregator),
`analytics` (analytics-engine), and `deprecated` (syslog/snmp gateways -- do not enable).

### Authentication Setup

**For HTTP Gateway users:**
```bash
# Create a device/service account (see tools/user-manager.py --help for
# the full role/scope/token command set)
docker exec -it alert-http-gateway python3 tools/user-manager.py create-user device-monitor --default-client device-monitor
docker exec -it alert-http-gateway python3 tools/user-manager.py create-role device-writer --description 'Alert submission for device-monitor'
docker exec -it alert-http-gateway python3 tools/user-manager.py add-scope device-writer device-monitor alerts:write
docker exec -it alert-http-gateway python3 tools/user-manager.py grant-role device-monitor device-writer

# Test authentication
./tools/send-http-alert.sh -u device-monitor -P your-password test
```

**For MQTT devices (ESP32/IoT):**
```bash
# Create MQTT username/password
./tools/mqtt-auth-manager.sh add your-device-name

# Configure ESP32 with:
# - CA certificate (data/certs/ca/ca.crt)
# - Username and password from output
# - QoS 1 for reliable delivery
```

> **SNMP devices**: SNMPv3 trap ingestion was deprecated in v2.0.2 and never had a
> production device. `tools/snmp-auth-manager.sh` and `docs/SNMP_*.md` are retained for
> historical reference only -- use the HTTP gateway instead.

**Send your first alert:**
```bash
# timestamp is REQUIRED (Unix epoch integer); category must be one of the fixed values
curl -X POST https://your-host/alert \
  -u your-service-name:password \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'$(date +%s)',"level":"info","category":"system","title":"Hello","message":"Test alert","source":"curl"}'
```

### Development Workflow

When making code changes to Python source files, Docker containers must be manually rebuilt and restarted to apply the changes.

**⚠️ Important:** Code changes are NOT automatically reflected in running containers. You must rebuild.

```bash
# 1. After editing source files, rebuild the image.
#    Only `alert-router` declares a build context -- every other service reuses
#    `image: alert-router:latest`. Building any other service name prints
#    "uses an image, skipping" and does nothing, so always build alert-router:
docker-compose build alert-router          # add --no-cache only if deps changed

# 2. Recreate the service(s) to pick up the new image
docker-compose up -d <service-name>

# 3. Verify the changes
docker logs <container-name> --tail 50

# 4. For browser cache (HTTP gateway UI changes)
# Hard refresh in browser: Ctrl+Shift+R (or Cmd+Shift+R on Mac)
```

**Running the test suite:**
```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -q
```

### Alert Message Format

```json
{
  "timestamp": 1705317000,
  "level": "warning",
  "category": "security",
  "title": "Motion Detected",
  "message": "Front door motion sensor triggered",
  "source": "security_system",
  "client": "home",
  "metadata": {
    "device_id": "motion_01",
    "led_zones": ["main", "security"]
  },
  "routing": {
    "override": true,
    "pathways": ["led_sign", "console", "file_log"]
  },
  "display_config": {
    "mode": "newsflash",
    "color": "red",
    "priority": true,
    "duration": 30
  }
}
```

`timestamp`, `level`, `category`, `title`, `message`, and `source` are required.
`timestamp` must be a Unix epoch **integer** (not ISO-8601) and `source` must be a
**string**. `routing.override: true` replaces the configured rules outright.
Full contract with validation rules: [docs/ALERT_PAYLOAD_SPEC.md](docs/ALERT_PAYLOAD_SPEC.md).

### Alert Levels
- **`critical`**: Immediate attention required (aliases: `error`, `err`, `crit`)
- **`warning`**: Important but not urgent (alias: `warn`)
- **`notice`**: Normal but significant operational events
- **`info`**: General information (alias: `inf`)
- **`debug`**: Troubleshooting information (alias: `dbg`)

### Alert Categories

`security`, `system`, `weather`, `automation`, `personal`, `network`, `application`,
`news`, `business`, `hardware`, `environmental` -- fixed values, case-insensitive.
See [docs/ALERT_PAYLOAD_SPEC.md](docs/ALERT_PAYLOAD_SPEC.md) for definitions.

## Client Integration

### Python SDK

Use the SDK in [`sdk/python/`](sdk/python/) -- it validates `level`/`category`/`timestamp`
client-side before the network call and raises typed exceptions:

```python
from alertmanager_sdk import AlertClient

client = AlertClient(
    "https://your-host",
    username="my-service",
    password="...",
    default_source="my-service",
)

client.send(level="warning", category="system", title="Test", message="System test")
```

Or with plain `requests`:

```python
import requests, time

requests.post(
    "https://your-host/alert",
    auth=("my-service", "password"),
    json={
        "timestamp": int(time.time()),
        "level": "warning",
        "category": "system",
        "title": "Test",
        "message": "System test",
        "source": "python-client",
    },
    timeout=10,
).raise_for_status()
```

> **Not via MQTT.** Publishing to `alerts/input` is not a supported client path -- the
> topic is internal service-to-service plumbing and the external broker does not ACL it
> for external publish. See "Access Methods" above.

### Shell Script

```bash
#!/bin/bash
send_alert() {
    local level="$1" category="$2" title="$3" message="$4" source="$5"
    curl -sS -X POST https://your-host/alert \
        -u "$ALERT_USER:$ALERT_PASSWORD" \
        -H "Content-Type: application/json" \
        -d "{
        \"timestamp\": $(date +%s),
        \"level\": \"$level\",
        \"category\": \"$category\",
        \"title\": \"$title\",
        \"message\": \"$message\",
        \"source\": \"$source\"
    }"
}

# Check disk space
if [ $(df / | tail -1 | awk '{print $5}' | sed 's/%//') -gt 90 ]; then
    send_alert "critical" "system" "Disk Space Critical" "Root partition over 90%" "disk_monitor"
fi
```

### Home Assistant

```yaml
automation:
  - alias: "Motion Alert"
    trigger:
      platform: state
      entity_id: binary_sensor.front_door_motion
      to: 'on'
    action:
      service: rest_command.send_alert
      data:
        level: "warning"
        category: "security"
        title: "Motion Detected"
        message: "Front door motion sensor triggered"
        source: "home_assistant"

rest_command:
  send_alert:
    url: "https://your-host/alert"
    method: POST
    headers:
      authorization: "Basic base64(username:password)"
      content-type: "application/json"
    payload: >
      {
        "timestamp": {{ as_timestamp(now()) | int }},
        "level": "{{ level }}",
        "category": "{{ category }}",
        "title": "{{ title }}",
        "message": "{{ message }}",
        "source": "{{ source }}"
      }
```

For more integration examples see [docs/CLIENTNODE.md](docs/CLIENTNODE.md) and [docs/DEV_GUIDE.md](docs/DEV_GUIDE.md).

## Output Pathways

- **File** -- per-client date-rotated logs (`data/logs/file/{client}/{date}.log`) with optional single-line message sanitization
- **Email** -- Microsoft Graph API (O365) with Jinja2 templates and storm-prevention aggregation (multiple criticals in a window collapse into one summary email)
- **LED Sign** -- BetaBrite signs over MQTT with per-zone filtering, animation modes, colors, and priority interrupts
- **Console** -- formatted stdout for development and debugging
- **MQTT** -- publish to arbitrary topics on the internal broker
- **PostgreSQL** -- every alert recorded by the `database-logger` service (a standalone consumer, not a router pathway)

## Routing Rules

Routing rules are processed in order; the first matching rule wins.

```json
{
  "name": "Critical Security Alerts",
  "conditions": {
    "level": ["critical"],
    "category": ["security"]
  },
  "pathways": ["led_sign", "console", "file_log"]
}
```

Individual alerts can bypass the rules with `"routing": {"override": true, "pathways": [...]}` (see the payload example above).

## Monitoring

- **`GET /health`** (no auth) -- gateway liveness over HTTPS; the right check for off-host monitoring
- **`POST /test`** (any valid login) -- generates a sample alert and routes it for real through the whole pipeline; a connectivity check, not a payload validator
- **`alerts/service/status`** -- router health + stats published every 60s on the **internal** broker (`localhost:1883`, container network only), e.g.:

```json
{
  "status": "running",
  "timestamp": "2024-01-15T10:30:00Z",
  "stats": {
    "alerts_processed": 42,
    "alerts_routed": 38,
    "errors": 0,
    "uptime_seconds": 3600,
    "last_alert": "2024-01-15T10:29:00Z"
  }
}
```

The web dashboard (`/dashboard`) and streaming log viewer (`/logs`) provide the same visibility in the browser, filtered by each account's roles and scopes.

## Troubleshooting

### Service Won't Start
- Verify configuration file syntax (`data/config/docker.json`)
- Check permissions on log directories
- Confirm `GATEWAY_SECRET_KEY` is set in `.env` (the gateway refuses to start without it)
- View logs: `docker logs alert-http-gateway` / `docker logs alert-router`
- Check overall health: `curl -s https://your-host/health`

### Alert Rejected
- **`400`** -- missing a required field. All six are mandatory: `timestamp`, `level`, `category`, `title`, `message`, `source`
- **`400`** -- `timestamp` must be a Unix epoch **integer** (no decimals, not ISO-8601), and `source` must be a string, not an object
- **`401`** -- bad credentials, or the account is locked (5 failed logins → 900s). Clear with `user-manager.py unlock <username>`
- **`403`** -- the account holds no `alerts:write` scope for the effective client (payload `client` → else the account's `default_client` → else clientless, which requires `master_admin`). See [docs/CLIENTNODE.md](docs/CLIENTNODE.md)
- **`429`** -- per-IP login throttle (20 failed logins / 10 min); applies to `POST /login` only

### No Alerts Appearing in Logs
- Confirm the pipeline works end-to-end: `curl -X POST https://your-host/test -u user:password`
- Check the router picked it up: `docker logs alert-router --tail 50`
- Watch for `pathway_manager - ERROR - Unknown pathway: <name>` -- `alert-router` reads config **only at startup**, so a pathway added to `docker.json` while the container kept running is silently dropped. Rebuild and restart the service
- Per-client alert content lands in `data/logs/file/{client}/{date}.log`; service logs are in `data/logs/ops/`

## Documentation

**Start here**
- **[docs/CLIENTNODE.md](docs/CLIENTNODE.md)** - Client integration guide (payload contract, credential rotation)
- **[docs/ALERT_PAYLOAD_SPEC.md](docs/ALERT_PAYLOAD_SPEC.md)** - Complete payload specification with validation rules
- **[sdk/python/README.md](sdk/python/README.md)** - Python SDK usage
- **[skills/alert-manager/](skills/alert-manager/)** - Claude Code skill: teaches an LLM agent the payload contract, SDK, and linux-agent so it can integrate correctly on the first try (`install.sh` copies it into `~/.claude/skills/`)
- **[sample-payload/](sample-payload/)** - JSON payload reference examples

**Operations & architecture**
- **[docs/HTTP_GATEWAY.md](docs/HTTP_GATEWAY.md)** - HTTP API reference (sessions, blueprints, endpoints)
- **[docs/AUTH.md](docs/AUTH.md)** - Auth model: sessions, RBAC roles/scopes, argon2id, Bearer tokens
- **[docs/DOCKER.md](docs/DOCKER.md)** - Docker deployment, compose profiles, volumes
- **[docs/DATABASE_INTEGRATION.md](docs/DATABASE_INTEGRATION.md)** - PostgreSQL schema, partitions, database-logger
- **[docs/DEV_GUIDE.md](docs/DEV_GUIDE.md)** - Developer setup and contribution guide

**Pathways & devices**
- **[docs/BETABRITE.md](docs/BETABRITE.md)** - LED sign capabilities reference
- **[docs/MQTT_AUTH.md](docs/MQTT_AUTH.md)** - MQTT username/password authentication (ESP32 LED signs)
- **[docs/TEMPLATES_GUIDE.md](docs/TEMPLATES_GUIDE.md)** - Jinja2 email template system
- **[docs/ANALYTICS.md](docs/ANALYTICS.md)** - Scheduled email reports & log analysis framework

**Historical / deprecated (reference only)**
- **[docs/ALERTING_SYSTEM_PLAN.md](docs/ALERTING_SYSTEM_PLAN.md)** - Original system design
- **[docs/SNMP_INTEGRATION.md](docs/SNMP_INTEGRATION.md)** - SNMPv3 integration (deprecated v2.0.2)
- **[docs/SYSLOG_INTEGRATION.md](docs/SYSLOG_INTEGRATION.md)** - Syslog gateway (deprecated v1.2.0)

## License

[MIT](LICENSE)
