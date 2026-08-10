# Alert Payload Specification

## Overview

This document defines the **single standardized JSON payload structure** for all alert inputs to the Alert Manager system (MQTT, HTTP, etc.). The syslog gateway is the only exception - it transforms syslog messages into this standard format internally.

## Required Fields

All alerts **MUST** include these fields:

| Field | Type | Description | Validation |
|-------|------|-------------|------------|
| `timestamp` | integer | Unix epoch timestamp (seconds, no decimals) | Must be integer |
| `level` | string | Alert severity level | Must be one of: `critical`, `warning`, `notice`, `info`, `debug` |
| `category` | string | Alert category/type | Must be one of: `security`, `system`, `weather`, `automation`, `personal`, `network`, `application`, `news`, `business`, `hardware`, `environmental` |
| `title` | string | Short alert title/summary | Any non-empty string |
| `message` | string | Detailed alert message/description | Any non-empty string |
| `source` | string | Source identifier (system/device/service name) | Must be string (not object) |

## Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `client` | string | Client name/identifier for filtering and logging (defaults to empty string if not provided) |
| `metadata` | object | Additional arbitrary key-value data |
| `routing.override` | boolean | If true, use `routing.pathways` instead of config rules |
| `routing.pathways` | array | List of pathway names to route to (requires `routing.override: true`) |
| `routing.exclude` | array | List of pathway names to exclude from routing |
| `display_config` | object | LED sign display configuration (see LED Sign section) |

## Alert Levels

### Level Usage Guidelines

- **`critical`** - Emergency conditions requiring immediate attention (system down, security breach, data loss)
- **`warning`** - Warning conditions that may require investigation (high resource usage, failed attempts, degraded performance)
- **`notice`** - Normal but significant operational events (scheduled tasks completed, reboots, successful operations)
- **`info`** - General informational messages (status updates, routine operations)
- **`debug`** - Debug-level messages for troubleshooting

## Alert Categories

- **`security`** - Security events (authentication, authorization, intrusion detection)
- **`system`** - System-level events (boot, shutdown, hardware, OS events)
- **`weather`** - Weather-related alerts
- **`automation`** - Home/building automation events
- **`personal`** - Personal reminders and notifications
- **`network`** - Network events (connectivity, devices, traffic)
- **`application`** - Application-level events (errors, status, performance)
- **`news`** - News and information feeds
- **`business`** - Business operations (audits, imports, config changes, CRUD events)
- **`hardware`** - Physical infrastructure (device status, battery, disk health, sensors)
- **`environmental`** - Environmental monitoring (temperature, humidity, air quality, CO2)

## Metadata Object

The `metadata` field can contain any additional information as key-value pairs:

```json
{
  "metadata": {
    "hostname": "server-01.example.com",
    "service": "nginx",
    "environment": "production",
    "region": "us-west-2",
    "custom_field": "any value"
  }
}
```

### Special Metadata Fields

- **`led_zones`** (array): Target specific LED sign zones (e.g., `["kitchen", "living_room"]`)
- **`syslog_facility`** (integer): Syslog facility code (added by syslog gateway)
- **`syslog_severity`** (integer): Syslog severity code (added by syslog gateway)
- **`source_ip`** (string): Client IP address (added by gateways)

## Gateway-Added Fields

Gateways automatically add a `_gateway` object with information about how the alert was received:

```json
{
  "_gateway": {
    "source": "http-gateway",
    "received_at": 1729213847,
    "client_ip": "192.168.1.100",
    "user_agent": "curl/7.68.0",
    "authenticated_user": "device-monitor",
    "protocol": "http"
  }
}
```

## LED Sign Display Configuration

For alerts targeting LED signs, you can include a `display_config` object:

```json
{
  "display_config": {
    "position": "topline",
    "mode": "flash",
    "special_effect": "starburst",
    "color": "red",
    "character_set": "7high",
    "speed": 3,
    "priority": true,
    "duration": 30
  }
}
```

See `docs/BETABRITE.md` for complete LED display configuration options.

## Client Field Usage

The optional `client` field allows you to identify which client is sending the alert. This is useful for:
- **Filtering**: Route alerts differently based on client
- **Logging**: Include client information in log files
- **Debugging**: Track which client is generating alerts

### Client in Log Format

Configure pathway formats to include the client field:

```json
{
  "type": "file",
  "format": "{timestamp} [{level}] [{category}] {source} [{client}]: {title} - {message}"
}
```

If client is not provided, it will appear as an empty string in logs: `[web-server-01] []:`

### Per-Client Log Files

Enable separate log files for each client:

```json
{
  "type": "file",
  "enabled": true,
  "path": "/data/logs/alerts.log",
  "format": "{timestamp} [{level}] [{category}] {source}: {title} - {message}",
  "per_client_files": true,
  "client_path_template": "/data/logs/alerts-{client}.log"
}
```

**Behavior:**
- Alert with `client: "prod-web-01"` → writes to `/data/logs/alerts-prod-web-01.log`
- Alert with `client: "test-monitor"` → writes to `/data/logs/alerts-test-monitor.log`
- Alert without client → writes to `/data/logs/alerts-log.log`

**Alternative Template (without placeholder):**
```json
{
  "client_path_template": "/data/logs/alerts.log"
}
```
Results in: `/data/logs/alerts-<client>.log` (automatically inserts client before extension)

### Client-Based Routing

Add client conditions to routing rules in your configuration:

```json
{
  "routing_rules": [
    {
      "name": "Production Client Alerts",
      "conditions": {
        "level": ["critical", "warning"],
        "client": ["prod-monitor", "prod-web-01"]
      },
      "pathways": ["email_alerts", "file_log", "led_sign"]
    },
    {
      "name": "Dev Client Alerts",
      "conditions": {
        "client": ["dev-monitor", "staging-web-01"]
      },
      "pathways": ["file_log"]
    }
  ]
}
```

### Client Filtering in Pathways

Filter alerts at the pathway level using `client_filter`:

```json
{
  "type": "email",
  "enabled": true,
  "client_filter": ["prod-monitor", "critical-systems"],
  "from_address": "alerts@example.com",
  "to_addresses": ["admin@example.com"]
}
```

## Routing Overrides

### Using Specific Pathways

Override the default routing rules and send to specific pathways:

```json
{
  "timestamp": 1729213847,
  "level": "warning",
  "category": "system",
  "title": "Database Backup Complete",
  "message": "Nightly backup completed successfully",
  "source": "backup-service",
  "routing": {
    "override": true,
    "pathways": ["file_log", "email_alerts"]
  }
}
```

### Excluding Pathways

Use normal routing rules but exclude specific pathways:

```json
{
  "timestamp": 1729213847,
  "level": "debug",
  "category": "application",
  "title": "Debug Event",
  "message": "Debug information",
  "source": "app-server",
  "routing": {
    "exclude": ["email_alerts", "led_sign"]
  }
}
```

## Complete Examples

### Minimal Alert (Required Fields Only)

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

### Full Alert with All Options

```json
{
  "timestamp": 1729213847,
  "level": "critical",
  "category": "security",
  "title": "Unauthorized Access Attempt",
  "message": "Failed login attempt from suspicious IP address",
  "source": "auth-service",
  "client": "prod-web-01",
  "metadata": {
    "username": "admin",
    "ip_address": "203.0.113.45",
    "attempt_count": 5,
    "geo_location": "Unknown",
    "led_zones": ["office", "main"]
  },
  "display_config": {
    "mode": "flash",
    "special_effect": "bomb",
    "color": "red",
    "priority": true,
    "duration": 60
  },
  "routing": {
    "exclude": ["console_alerts"]
  }
}
```

### Weather Alert

```json
{
  "timestamp": 1729213847,
  "level": "warning",
  "category": "weather",
  "title": "Severe Thunderstorm Warning",
  "message": "Severe thunderstorm warning in effect until 8:00 PM",
  "source": "weather-api",
  "metadata": {
    "region": "Edmonton, AB",
    "expires_at": 1729220400,
    "alert_type": "thunderstorm",
    "led_zones": ["kitchen", "living_room"]
  }
}
```

### Home Automation Alert

```json
{
  "timestamp": 1729213847,
  "level": "notice",
  "category": "automation",
  "title": "Front Door Unlocked",
  "message": "Front door was unlocked by keypad code",
  "source": "smart-lock",
  "metadata": {
    "device_id": "lock-front-001",
    "user_code": "4",
    "user_name": "Alice",
    "led_zones": ["entrance"]
  }
}
```

### Application Error

```json
{
  "timestamp": 1729213847,
  "level": "critical",
  "category": "application",
  "title": "Database Connection Failed",
  "message": "Unable to connect to primary database server, switching to replica",
  "source": "api-server-03",
  "metadata": {
    "error_code": "ECONNREFUSED",
    "database_host": "db-primary.local",
    "retry_count": 3,
    "environment": "production"
  },
  "routing": {
    "override": true,
    "pathways": ["email_alerts", "file_log", "console_alerts"]
  }
}
```

## HTTP Gateway Example (cURL)

```bash
curl -X POST http://localhost:8080/alert \
  -H "Content-Type: application/json" \
  -u "username:password" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "warning",
    "category": "system",
    "title": "High CPU Usage",
    "message": "CPU usage exceeded 90% for 5 minutes",
    "source": "monitoring-agent",
    "client": "prod-monitor-01",
    "metadata": {
      "cpu_percent": 94.5,
      "hostname": "web-server-01"
    }
  }'
```

## MQTT Example (mosquitto_pub)

```bash
mosquitto_pub -h alert.d-t.pw -p 42690 \
  -t "alerts/input" \
  -m '{
    "timestamp": '$(date +%s)',
    "level": "critical",
    "category": "security",
    "title": "Motion Detected",
    "message": "Motion sensor triggered in server room",
    "source": "motion-sensor-03",
    "metadata": {
      "room": "server-room",
      "sensor_id": "motion-03"
    }
  }'
```

## Python Example

```python
import json
import time
import paho.mqtt.client as mqtt

def send_alert(level, category, title, message, source, metadata=None):
    """Send alert to MQTT broker"""
    alert = {
        "timestamp": int(time.time()),
        "level": level,
        "category": category,
        "title": title,
        "message": message,
        "source": source
    }

    if metadata:
        alert["metadata"] = metadata

    client = mqtt.Client()
    client.connect("alert.d-t.pw", 42690)
    client.publish("alerts/input", json.dumps(alert))
    client.disconnect()

# Usage
send_alert(
    level="warning",
    category="system",
    title="Disk Space Low",
    message="Available disk space is below 10%",
    source="disk-monitor",
    metadata={"disk": "/dev/sda1", "percent_free": 8.2}
)
```

## Validation Errors

If an alert fails validation, you'll receive a detailed error message:

### Missing Required Field
```json
{
  "error": "Missing required field: timestamp",
  "timestamp": "2025-10-18T01:23:45Z"
}
```

### Invalid Level
```json
{
  "error": "Invalid alert level: error",
  "valid_levels": ["critical", "warning", "notice", "info", "debug"],
  "timestamp": "2025-10-18T01:23:45Z"
}
```

### Invalid Category
```json
{
  "error": "Invalid alert category: testing",
  "valid_categories": ["security", "system", "weather", "automation", "personal", "network", "application", "news", "business", "hardware", "environmental"],
  "timestamp": "2025-10-18T01:23:45Z"
}
```

### Invalid Type
```json
{
  "error": "timestamp must be Unix epoch integer, got: <class 'str'>",
  "timestamp": "2025-10-18T01:23:45Z"
}
```

## Best Practices

1. **Use Unix Timestamps**: Always use Unix epoch integers (seconds) for timestamps
2. **Descriptive Sources**: Use clear, descriptive source identifiers (e.g., `"web-server-01"` not `"srv1"`)
3. **Meaningful Titles**: Keep titles concise but descriptive (under 100 characters)
4. **Structured Metadata**: Use metadata for machine-readable data, message for human-readable descriptions
5. **Appropriate Levels**: Use `critical` sparingly for true emergencies only
6. **Consistent Categories**: Use categories consistently across your alerts for better routing
7. **Routing Overrides**: Only use routing overrides when necessary; let configuration rules handle most routing

## Syslog Exception

The **syslog gateway** accepts standard syslog messages and transforms them into this format automatically. You don't need to format syslog messages - the gateway handles the conversion:

- Syslog severity → Alert level
- Syslog facility → Alert category
- Syslog hostname → Alert source
- Syslog message → Alert message

See `docs/ALERTING_SYSTEM_PLAN.md` for syslog gateway details.
