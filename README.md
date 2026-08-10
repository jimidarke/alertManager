# Unified Alerting System

![Version](https://img.shields.io/badge/version-3.1.1-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Python](https://img.shields.io/badge/python-3.8+-blue.svg)

A centralized alerting system that accepts standardized alert messages and routes them to appropriate output channels based on configurable rules. The system uses a publish/subscribe pattern with MQTT as the backbone, featuring rich LED sign display capabilities and comprehensive integration options.

**[View Changelog](CHANGELOG.md)** | **[Documentation](docs/)** | **[Quick Start](#quick-start)**

## 📁 Project Structure

```
alertManager/
├── src/                        # Source code
│   ├── alert_router.py         # Main service script
│   └── requirements.txt        # Python dependencies
├── data/config/                # Configuration files
│   ├── docker.json            # Docker container configuration
│   ├── main.json              # External/production configuration
│   └── test.json              # Local development configuration
├── examples/                   # Example configurations
│   ├── email-example.json     # Email pathway examples
│   ├── led-sign-example.json  # LED sign configuration
│   └── pathway-examples.json  # Pathway configuration examples
├── docker/                     # Docker deployment
│   ├── Dockerfile             # Container image definition
│   ├── docker-compose.yml     # Service orchestration
│   └── .dockerignore          # Docker ignore rules
├── scripts/                    # Installation and service scripts
│   ├── install.sh             # System installation script
│   └── alert-router.service   # Systemd service definition
├── tests/                      # Test scripts
│   ├── test_alerts.py         # Local MQTT test script
│   └── docker-test.py         # Docker test script
├── docs/                       # Documentation
│   ├── ALERTING_SYSTEM_PLAN.md # Complete system architecture plan
│   ├── BETABRITE.md           # BetaBrite LED sign display capabilities
│   ├── DEV_GUIDE.md           # Developer integration guide
│   ├── DOCKER.md              # Docker deployment guide
│   └── CLAUDE.md              # Claude Code development guidance
├── data/                       # Runtime data (Docker volumes)
│   ├── config/                # Runtime configuration
│   ├── logs/                  # Log files
│   └── scripts/               # Custom runtime scripts
└── CLAUDE.md                   # Claude Code guidance (root level)
```

## Key Features

- **🎯 Single Entry Point**: All alerts are submitted over HTTPS to `POST /alert` (the sole supported ingestion path as of v2.0.2)
- **🔀 Intelligent Routing**: Route alerts based on level, category, zone, and time conditions
- **🎨 Rich LED Display**: BetaBrite integration with 23+ animation modes, 21+ special effects, 12+ colors
- **📤 Multi-Channel Output**: Console, file logging, LED signs, Home Assistant, email, SMS, and more
- **⚙️ Service Integration**: Runs as systemd service or Docker container for reliability
- **🎛️ Payload-Level Control**: Override routing and display settings per alert
- **📊 Status Monitoring**: Real-time service health and statistics via MQTT
- **🔌 Extensible Architecture**: Easy integration with IoT devices, monitoring systems, and automation platforms

## System Architecture

```
[Any client] --HTTPS--> [HTTP Gateway] --> [Local MQTT Broker] --> [Alert Router] --> [Output Channels]
              POST /alert   443 only        alerts/input (internal)                          |
                                            (port 1883, unauth,                   alerts/service/status
                                             container network only)

[ESP32 LED signs] <--MQTTS--> alert.d-t.pw:42690   (device bus ONLY -- not alert ingestion)
```

### Access Methods

**HTTP Gateway** (`https://alert.d-t.pw`) -- the only way to submit an alert:
- **Security**: HTTPS only via `nginx-proxy`. HTTP Basic Auth or Bearer token for `POST /alert`; session cookie for browser pages. As of v2.0.0 the container publishes **no** host port -- the old `:8080` URL is unreachable from outside the Docker host.
- **Use Cases**: every integration -- scripts, services, devices, the web UI
- **Endpoint**: `POST /alert` (see `docs/CLIENTNODE.md` for the wire contract)

**MQTT Endpoint** (`alert.d-t.pw:42690`) -- **not** an alert-ingestion path:
- **Scope**: ESP32 BetaBrite LED sign devices only (display push + telemetry). As of v2.0.2 this is a device-communication bus; the internal `alerts/input` topic is service-to-service plumbing and is not ACL'd for external publish.
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
git clone https://github.com/yourorg/alertManager.git
cd alertManager

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
# the full role/scope/token command set -- replaces tools/http-auth-manager.sh)
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

**For HTTP clients:**
```bash
# Create user account (see the HTTP Gateway users example above for the
# full create-user/create-role/add-scope/grant-role sequence)
docker exec -it alert-http-gateway python3 tools/user-manager.py create-user your-service-name

# Test connection (timestamp is REQUIRED; category must be one of the fixed values)
curl -X POST https://alert.d-t.pw/alert \
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

# Examples:
docker-compose build alert-router && docker-compose up -d http-gateway
docker-compose build alert-router && docker-compose up -d alert-router

# 3. Verify the changes
docker logs <container-name> --tail 50

# 4. For browser cache (HTTP gateway UI changes)
# Hard refresh in browser: Ctrl+Shift+R (or Cmd+Shift+R on Mac)
```

**Common Services:**
- `http-gateway` - HTTP API and log viewer UI
- `alert-router` - Core alert routing service (includes email pathways)
- `file-logger` - File logging pathway

**Note:** The `--no-cache` flag ensures a clean rebuild. Omit it for faster builds if only Python code changed (not dependencies).

### Alert Message Format

```json
{
  "timestamp": 1705317000,
  "level": "warning",
  "category": "security",
  "title": "Motion Detected",
  "message": "Front door motion sensor triggered",
  "source": {
    "id": "security_system",
    "type": "sensor",
    "location": "front_door",
    "metadata": {
      "device_id": "motion_01",
      "firmware": "v1.2.3"
    }
  },
  "routing": {
    "override": true,
    "pathways": ["led_sign", "console", "file"]
  },
  "pathway_config": {
    "led_sign": {
      "zones": ["main", "security"],
      "display_config": {
        "mode": "newsflash",
        "color": "red",
        "priority": true,
        "duration": 30
      }
    },
    "file": {
      "filename": "security_alerts.log"
    }
  }
}
```

### Alert Levels
- **`critical`**: Immediate attention required (security breaches, system failures)
- **`warning`**: Important but not urgent (resource limits, degraded performance)
- **`notice`**: Normal but significant operational events (scheduled tasks completed, reboots occurred, service restarts)
- **`info`**: General information (status updates, routine events)
- **`debug`**: Troubleshooting information (development logs, diagnostics)

### Alert Categories
- **`security`**: Security events (motion detection, login failures, intrusions)
- **`weather`**: Weather alerts (temperature warnings, storm alerts)
- **`system`**: System status/health (CPU usage, disk space, service status)
- **`automation`**: Home automation events (device state changes, schedules)
- **`personal`**: Personal reminders (calendar events, task notifications)
- **`network`**: Network events (connection issues, bandwidth alerts)
- **`application`**: Application events (errors, deployments, user actions)

For complete message schema and integration examples, see [docs/DEV_GUIDE.md](docs/DEV_GUIDE.md)

## Configuration Examples

### Python Client

Use the SDK in [`sdk/python/`](sdk/python/) -- it validates `level`/`category`/`timestamp`
client-side before the network call and raises typed exceptions:

```python
from alertmanager_sdk import AlertClient

client = AlertClient(
    "https://alert.d-t.pw",
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
    "https://alert.d-t.pw/alert",
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

### HTTP Client (cURL)
```bash
curl -X POST https://alert.d-t.pw/alert \
  -u username:password \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "info",
    "category": "system",
    "title": "Service Started",
    "message": "Application has started successfully",
    "source": "my-service"
  }'
```

### Home Assistant Integration
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
    url: "https://alert.d-t.pw/alert"
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

## Documentation

### Complete Documentation

- **[docs/ANALYTICS.md](docs/ANALYTICS.md)** - Scheduled email reports & log analysis framework
- **[docs/CLIENTNODE.md](docs/CLIENTNODE.md)** - Client integration guide (payload contract, credential rotation) -- start here
- **[docs/AUTH.md](docs/AUTH.md)** - Gateway auth model: sessions, RBAC roles/scopes, argon2id, Bearer tokens
- **[docs/MQTT_AUTH.md](docs/MQTT_AUTH.md)** - MQTT username/password authentication (ESP32 LED signs)
- **[docs/SNMP_INTEGRATION.md](docs/SNMP_INTEGRATION.md)** - SNMPv3 integration (**DEPRECATED** v2.0.2, reference only)
- **[docs/ALERTING_SYSTEM_PLAN.md](docs/ALERTING_SYSTEM_PLAN.md)** - Original system design (historical context)
- **[docs/BETABRITE.md](docs/BETABRITE.md)** - LED sign integration guide
- **[docs/DOCKER.md](docs/DOCKER.md)** - Docker deployment guide
- **[docs/HTTP_GATEWAY.md](docs/HTTP_GATEWAY.md)** - HTTP API reference
- **[sample-payload/](sample-payload/)** - JSON payload reference examples (basic.json, full.json)

### Testing

**Send a test alert:**
```bash
curl -X POST https://alert.d-t.pw/alert \
  -u test-user:password \
  -H "Content-Type: application/json" \
  -d '{"timestamp":'$(date +%s)',"level":"info","category":"system","title":"Test","message":"Hello World","source":"test"}'
```

**Check the pipeline end-to-end** (`POST /test` ignores any body you send, generates its own
sample alert, and routes it for real — it is a connectivity check, not a payload validator.
It needs only a valid login, not an `alerts:write` scope):
```bash
curl -X POST https://alert.d-t.pw/test -u test-user:password
```

Or use the helper script, which handles auth and the timestamp for you:
```bash
export ALERT_USER=test-user ALERT_PASSWORD=...
./tools/send-http-alert.sh test
```

## Output Channels

### LED Sign Display (BetaBrite)
Rich visual alerts with 23+ animation modes, 21+ special effects, and 12+ colors. Supports dynamic display configuration based on alert severity and type.

**Topic**: `ledSign/{device}/message`
**Features**: Priority messages, animated transitions, contextual color coding

### Console Output
Real-time console display with formatted timestamps and structured output for development and debugging.

### File Logging
Comprehensive logging with rotation support, detailed metadata preservation, and searchable alert history.

**Location**: `./data/logs/alerts.log` (Docker) or `/var/log/alerts/alerts.log` (System)

### Home Assistant Integration
Native integration via MQTT topics and REST API calls for notifications, automations, and dashboard display.

### Additional Output Channels
- **Smart Lighting**: Color-coded room lighting via smart bulbs and LED strips
- **Audio Alerts**: TTS announcements, sound effects, and chimes
- **Mobile Notifications**: Push notifications to phones and tablets  
- **Email/SMS**: Critical alert delivery via external services
- **External APIs**: Webhook integration with third-party services

For complete output channel configuration, see [docs/ALERTING_SYSTEM_PLAN.md](docs/ALERTING_SYSTEM_PLAN.md)

## Routing Rules

Routing rules are processed in order. The first matching rule determines outputs.

### Example Rules

```json
{
  "name": "Critical Security Alerts",
  "conditions": {
    "level": ["critical"],
    "category": ["security"]
  },
  "outputs": ["led_sign", "console", "file"]
}
```

### Routing Override

Individual alerts can override routing rules:

```json
{
  "routing": {
    "override": true,
    "outputs": ["console", "file"]
  }
}
```

## Monitoring

### Service Status

The service publishes status to `alerts/service/status`:

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

### Home Assistant Integration

```yaml
# configuration.yaml
mqtt:
  sensor:
    - name: "Alert Router Status"
      state_topic: "alerts/service/status"
      value_template: "{{ value_json.status }}"
      json_attributes_topic: "alerts/service/status"
```

> **Reachability caveat**: `alerts/service/status` is published on the **internal** broker
> (`localhost:1883`, container network only). The external broker on `:42690` is scoped to
> ESP32 LED sign devices with per-zone ACLs, so this only works for a Home Assistant instance
> on the Docker host's own network. For off-host monitoring use `GET /health` over HTTPS instead.

## Integration Examples

### Python Integration
```python
import time
import requests

class AlertSender:
    def __init__(self, base_url="https://alert.d-t.pw", username=None, password=None):
        self.url = f"{base_url}/alert"
        self.auth = (username, password)

    def send_alert(self, level, category, title, message, source,
                   metadata=None, routing=None, display_config=None):
        alert = {
            # Unix epoch SECONDS, integer -- an ISO-8601 string is rejected
            "timestamp": int(time.time()),
            "level": level,
            "category": category,
            "title": title,
            "message": message,
            "source": source,
        }

        if metadata:
            alert["metadata"] = metadata
        if routing:
            alert["routing"] = routing
        if display_config:
            alert["display_config"] = display_config

        r = requests.post(self.url, auth=self.auth, json=alert, timeout=10)
        r.raise_for_status()
        return r.json()

# Usage
alerter = AlertSender(username="security-cam", password="...")
alerter.send_alert("critical", "security", "Intrusion Detected",
                   "Motion in server room", "security_cam",
                   metadata={"camera_id": "cam_005", "zone": "server_room"},
                   display_config={"mode": "flash", "color": "red", "priority": True})
```

> For production use prefer the SDK (`sdk/python/`) over this hand-rolled class -- it adds
> client-side validation and a tested retry policy. This example exists to show the raw contract.

### Shell Script Integration
```bash
#!/bin/bash
send_alert() {
    local level="$1" category="$2" title="$3" message="$4" source="$5"
    curl -sS -X POST https://alert.d-t.pw/alert \
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

For comprehensive integration examples and advanced routing, see [docs/DEV_GUIDE.md](docs/DEV_GUIDE.md)

## Troubleshooting

### Service Won't Start
- Verify configuration file syntax (`data/config/docker.json`)
- Check permissions on log directories
- Confirm `GATEWAY_SECRET_KEY` is set in `.env` (the gateway refuses to start without it)
- View logs: `docker logs alert-http-gateway` / `docker logs alert-router`
- Check overall health: `curl -s https://alert.d-t.pw/health`

### Alert Rejected
- **`400`** -- missing a required field. All six are mandatory: `timestamp`, `level`, `category`, `title`, `message`, `source`
- **`400`** -- `timestamp` must be a Unix epoch **integer** (no decimals, not ISO-8601), and `source` must be a string, not an object
- **`401`** -- bad credentials, or the account is locked (5 failed logins → 900s). Clear with `user-manager.py unlock <username>`
- **`403`** -- the account holds no `alerts:write` scope for the effective client (payload `client` → else the account's `default_client` → else clientless, which requires `master_admin`). See `docs/CLIENTNODE.md`
- **`429`** -- per-IP login throttle (20 failed logins / 10 min); applies to `POST /login` only

### No Alerts Appearing in Logs
- Confirm the alert was accepted (`POST /test` validates a payload without routing it)
- Check the router picked it up: `docker logs alert-router --tail 50`
- Watch for `pathway_manager - ERROR - Unknown pathway: <name>` -- `alert-router` reads config **only at startup**, so a pathway added to `docker.json` while the container kept running is silently dropped. Rebuild and restart the service
- Per-client alert content lands in `data/logs/file/{client}/{date}.log`; service logs are in `data/logs/ops/`

### Missing Dependencies
```bash
pip3 install paho-mqtt

# Additional setup for MQTT
sudo mkdir -p /opt/alert-router/certs
sudo chmod 750 /opt/alert-router/certs
# Copy CA certificate to /opt/alert-router/certs/
# - ca.crt (Certificate Authority for TLS verification)
# No client certificates needed - use username/password authentication
```

## Documentation

- **[docs/ALERTING_SYSTEM_PLAN.md](docs/ALERTING_SYSTEM_PLAN.md)** - Complete system architecture and implementation plan
- **[docs/DEV_GUIDE.md](docs/DEV_GUIDE.md)** - Developer integration guide with examples and API reference
- **[docs/BETABRITE.md](docs/BETABRITE.md)** - Comprehensive BetaBrite LED sign display capabilities
- **[docs/DOCKER.md](docs/DOCKER.md)** - Docker deployment and container management
- **[CLAUDE.md](CLAUDE.md)** - Development commands and Claude Code guidance

## Project Files

- **Core Service**: `src/alert_router.py` - Main alert processing service
- **Configuration**: `config/*.json` - Environment-specific settings
- **Deployment**: `docker/` - Container orchestration and deployment
- **Installation**: `scripts/` - System installation and service setup
- **Testing**: `tests/` - Alert testing and validation scripts

## License

This project is provided as-is for educational and personal use.