# Developer Guide - Alert Router Service

This guide covers the alert message payload structure, routing possibilities, and integration examples for developers working with the Alert Router Service.

## 📋 Alert Message Schema

**Standardized JSON Structure** - See [sample-payload/](../sample-payload/) for complete reference examples.

### Required Fields

Every alert message must include these core fields:

```json
{
  "timestamp": 1704045600,
  "level": "warning",
  "category": "security",
  "title": "Motion Detected",
  "message": "Front door motion sensor triggered",
  "source": {
    "id": "security-system-01",
    "type": "security_service",
    "metadata": {
      "version": "1.2.3",
      "location": "front_door",
      "device_id": "motion_01"
    }
  }
}
```

| Field | Type | Description | Required |
|-------|------|-------------|----------|
| `timestamp` | integer | Unix epoch timestamp (no decimals) | ✅ |
| `level` | string | Alert severity level | ✅ |
| `category` | string | Alert category/domain | ✅ |
| `title` | string | Brief alert title | ✅ |
| `message` | string | Detailed alert message | ✅ |
| `source` | object | Structured source with `id` field | ✅ |
| `source.id` | string | Unique source identifier | ✅ |

### Optional Fields

```json
{
  "metadata": {
    "priority": true,
    "led_zones": ["living_room", "office"],
    "custom_field": "any_value"
  },
  "routing": {
    "override": true,
    "pathways": ["led_security", "console", "file_log"],
    "exclude": ["mobile_push"]
  },
  "pathway_config": {
    "led_security": {
      "zones": {
        "living_room": {
          "display_config": {
            "mode": "newsflash",
            "special_effect": "starburst",
            "color": "red",
            "character_set": "7high",
            "speed": 3,
            "priority": true,
            "duration": 30
          }
        }
      }
    },
    "email_critical": {
      "to_addresses": ["admin@example.com"],
      "subject_template": "URGENT: {title}",
      "html_format": true
    }
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `metadata` | object | Custom data (any JSON structure) |
| `routing` | object | Routing overrides (see Routing section) |
| `pathway_config` | object | Per-pathway configuration overrides |

## 🎯 Alert Levels

| Level | Description | Use Cases |
|-------|-------------|-----------|
| `critical` | Immediate attention required | Security breaches, system failures, emergencies |
| `warning` | Important but not urgent | Resource limits, degraded performance, maintenance needed |
| `info` | General information | Status updates, completed tasks, notifications |
| `debug` | Troubleshooting information | Development logs, diagnostic data |

## 📂 Alert Categories

| Category | Description | Examples |
|----------|-------------|----------|
| `security` | Security events | Motion detection, login failures, intrusion attempts |
| `system` | System status/health | CPU usage, disk space, service status |
| `weather` | Weather alerts | Temperature warnings, storm alerts |
| `automation` | Home automation events | Device state changes, schedule triggers |
| `personal` | Personal reminders | Calendar events, task notifications |
| `network` | Network events | Connection issues, bandwidth alerts |
| `application` | Application events | Errors, deployments, user actions |

## 🔀 Routing System

### Default Routing Rules

The service processes routing rules in order and uses the first match:

1. **Critical Security Alerts** → `led_sign`, `console`, `file`
2. **Critical System Alerts** → `led_sign`, `console`, `file`
3. **Warning Alerts** → `console`, `file`
4. **Info Alerts** → `file`
5. **Debug Alerts** → `file`

### Routing Override

Override default routing by including a `routing` object:

```json
{
  "routing": {
    "override": true,
    "pathways": ["console", "led_sign"],
    "exclude": ["file"]
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `override` | boolean | If true, ignore default routing rules |
| `pathways` | array | Specific pathway instances to use |
| `exclude` | array | Output channels to exclude |

## 📤 Pathways

| Channel | Description | Configuration |
|---------|-------------|---------------|
| `console` | Real-time console pathway | Always enabled |
| `file` | Log file pathway | Path: `./alerts.log` (dev) or `/var/log/alerts.log` (prod) |
| `led_sign` | MQTT pathway for LED displays | Topic: `ledSign/main/message` |

## 🖥️ Display Configuration

The `display_config` object allows fine-grained control over BetaBrite LED sign presentation. This enables dynamic, context-aware display effects that match alert severity and type.

### Display Configuration Schema

```json
{
  "display_config": {
    "position": "topline",
    "mode": "newsflash", 
    "special_effect": "starburst",
    "color": "red",
    "character_set": "7high",
    "speed": 3,
    "flash": true,
    "priority": true,
    "duration": 30,
    "character_attributes": ["shadow", "wide"],
    "spacing": "proportional"
  }
}
```

### Complete BetaBrite Display Options

#### All Available Display Modes (23 options)
| Mode | Description | Visual Effect | Best For |
|------|-------------|---------------|----------|
| `rotate` | Cycles through text files | Basic rotation | Default display |
| `hold` | Static display | No movement | Important messages |
| `flash` | Text blinks on/off | Flashing | Critical alerts |
| `rollup` | Text rolls up from bottom | Vertical motion | Smooth transitions |
| `rolldown` | Text rolls down from top | Vertical motion | Announcements |
| `rollleft` | Text rolls in from right | Horizontal motion | Side entry |
| `rollright` | Text rolls in from left | Horizontal motion | Side entry |
| `wipeup` | Text wipes upward | Clean reveal | Professional look |
| `wipedown` | Text wipes downward | Clean reveal | Top-down info |
| `wipeleft` | Text wipes leftward | Horizontal reveal | Left-to-right reading |
| `wiperight` | Text wipes rightward | Horizontal reveal | Right-to-left effect |
| `scroll` | Text scrolls horizontally | Continuous scroll | Long messages |
| `special` | Enables special effects | Effect-dependent | Enhanced presentations |
| `automode` | Sign chooses method | Automatic | General purpose |
| `rollin` | Text rolls in from edges | Converging motion | Focused attention |
| `rollout` | Text rolls out to edges | Diverging motion | Expanding effect |
| `wipein` | Text wipes in from edges | Converging reveal | Centered focus |
| `wipeout` | Text wipes out to edges | Diverging reveal | Spreading effect |
| `compressed_rotate` | Compact rotation | Space-efficient | Multiple messages |
| `explode` | Text explodes outward | Explosive effect | Maximum impact |
| `clock` | Time/date display | Clock format | Timestamps |

#### All Available Special Effects (21 options)
| Effect | Description | Mood/Theme | Best For |
|--------|-------------|------------|----------|
| `twinkle` | Stars twinkling | Gentle, pleasant | Info messages |
| `sparkle` | Sparkling lights | Celebratory | Good news |
| `snow` | Falling snow | Winter, weather | Weather alerts |
| `interlock` | Interlocking pattern | Mechanical | System status |
| `switch` | Switching pattern | Toggle effect | State changes |
| `slide` | Sliding motion | Smooth directional | Transitions |
| `spray` | Water spray effect | Dynamic, energetic | Active alerts |
| `starburst` | Explosive star | Dramatic attention | Critical alerts |
| `welcome` | Welcome message | Greeting, hospitality | Positive messages |
| `slots` | Slot machine effect | Random, gaming | Fun notifications |
| `newsflash` | Breaking news style | Urgent updates | Important news |
| `trumpet` | Trumpet fanfare | Announcement | Celebrations |
| `cyclecolors` | Color cycling | Rainbow effect | Dynamic display |
| `thankyou` | Thank you message | Gratitude | Completion |
| `nosmoking` | No smoking symbol | Warning | Prohibition |
| `dontdrinkanddrive` | Safety message | Safety warning | Warnings |
| `fishimal` | Fish animation | Playful, aquatic | Fun messages |
| `fireworks` | Fireworks explosion | Celebration | Major events |
| `turballoon` | Turbulent balloon | Floating, dynamic | Atmospheric |
| `bomb` | Explosion effect | Extreme urgency | Emergencies |

#### All Available Colors (12 options)
| Color | Description | Mood | Usage |
|-------|-------------|------|-------|
| `red` | Bright red | Urgent, critical | Emergencies |
| `green` | Bright green | Success, normal | Good status |
| `amber` | Amber/orange | Caution, warning | Warnings |
| `dimred` | Darker red | Subdued urgent | Quiet alerts |
| `dimgreen` | Darker green | Subtle positive | Background |
| `brown` | Brown/dark amber | Neutral | Information |
| `orange` | Orange | Moderate warning | Attention |
| `yellow` | Bright yellow | Attention | Highlights |
| `rainbow1` | Rainbow pattern | Multi-color | Dynamic |
| `rainbow2` | Alt rainbow | Multi-color | Variety |
| `colormix` | Mixed colors | Varied display | Creative |
| `autocolor` | Auto selection | Automatic | Default |

#### All Available Character Sets (14 options)
| Set | Description | Height | Style | Best For |
|-----|-------------|--------|-------|----------|
| `5high` | Basic 5-pixel | Small | Simple | Compact text |
| `5stroke` | 5-pixel outline | Small | Outlined | Contrast |
| `7high` | Standard 7-pixel | Medium | Clean | General use |
| `7stroke` | 7-pixel outline | Medium | Outlined | Visibility |
| `7highfancy` | Decorative 7-pixel | Medium | Stylized | Special messages |
| `10high` | Large 10-pixel | Large | Bold | Important text |
| `7shadow` | Shadow effect | Medium | Dimensional | Depth |
| `fhighfancy` | Full height decorative | Maximum | Elegant | Formal |
| `fhigh` | Full height standard | Maximum | Bold | Maximum impact |
| `7shadowfancy` | Shadow + decoration | Medium | Ornate | Elaborate |
| `5wide` | 5-pixel wide | Small | Compact wide | Space-efficient |
| `7wide` | 7-pixel wide | Medium | Standard wide | Readable wide |
| `7widefancy` | Decorative wide | Medium | Stylized wide | Special wide |
| `5widestroke` | 5-pixel wide outline | Small | Outlined wide | Contrast wide |

#### All Available Positions (6 options)
| Position | Description | Usage |
|----------|-------------|-------|
| `topline` | Upper display area | Headers, titles |
| `midline` | Center display area | Main content |
| `botline` | Lower display area | Status, footers |
| `fill` | Full display area | Maximum visibility |
| `left` | Left-aligned | Standard alignment |
| `right` | Right-aligned | Numbers, timestamps |

### Display Configuration Fields

| Field | Type | Options | Description |
|-------|------|---------|-------------|
| `position` | string | `topline`, `midline`, `botline`, `fill`, `left`, `right` | Text positioning on display |
| `mode` | string | `rotate`, `hold`, `flash`, `scroll`, `explode`, `rollup`, `newsflash`, etc. | Display animation mode (30+ options) |
| `special_effect` | string | `twinkle`, `sparkle`, `snow`, `starburst`, `fireworks`, `welcome`, etc. | Special visual effects (16+ options) |
| `color` | string | `red`, `green`, `amber`, `yellow`, `rainbow1`, `autocolor`, etc. | Text color (10+ options) |
| `character_set` | string | `5high`, `7high`, `10high`, `7shadow`, `7fancy`, etc. | Font style and size |
| `speed` | integer | `1`-`5` | Animation speed (1=slowest, 5=fastest) |
| `flash` | boolean | `true`/`false` | Enable character flashing |
| `priority` | boolean | `true`/`false` | Interrupt normal display rotation |
| `duration` | integer | Seconds | How long to display (priority messages only) |
| `character_attributes` | array | `wide`, `shadow`, `fancy`, `doublewide`, `doublehigh` | Text styling attributes |
| `spacing` | string | `proportional`, `fixedwidth` | Character spacing style |

### Level-Based Display Presets

#### Critical Alerts - Maximum Impact
```json
{
  "display_config": {
    "position": "fill",
    "mode": "explode",
    "special_effect": "bomb", 
    "color": "red",
    "character_set": "10high",
    "speed": 5,
    "flash": true,
    "priority": true,
    "duration": 30
  }
}
```

#### Warning Alerts - Attention Grabbing
```json
{
  "display_config": {
    "position": "topline",
    "mode": "newsflash",
    "special_effect": "trumpet",
    "color": "amber",
    "character_set": "7high", 
    "speed": 3,
    "priority": false
  }
}
```

#### Info Alerts - Gentle Display
```json
{
  "display_config": {
    "position": "midline",
    "mode": "scroll",
    "special_effect": "twinkle",
    "color": "green",
    "character_set": "7high",
    "speed": 2
  }
}
```

#### Debug Alerts - Minimal Distraction
```json
{
  "display_config": {
    "position": "botline",
    "mode": "hold",
    "special_effect": "none",
    "color": "dimgreen",
    "character_set": "5high",
    "speed": 1
  }
}
```

### Category-Specific Presets

#### Security Alerts - Immediate Attention
```json
{
  "display_config": {
    "position": "fill",
    "mode": "flash",
    "special_effect": "starburst",
    "color": "red",
    "character_set": "10high",
    "speed": 5,
    "priority": true,
    "flash": true
  }
}
```

#### Weather Alerts - Atmospheric
```json
{
  "display_config": {
    "position": "topline",
    "mode": "scroll", 
    "special_effect": "snow",
    "color": "amber",
    "character_set": "7high",
    "speed": 2
  }
}
```

#### System Status - Informational
```json
{
  "display_config": {
    "position": "midline",
    "mode": "compressed_rotate",
    "special_effect": "sparkle", 
    "color": "autocolor",
    "character_set": "7high",
    "speed": 2
  }
}
```

#### Celebration - Festive
```json
{
  "display_config": {
    "position": "fill",
    "mode": "explode",
    "special_effect": "fireworks",
    "color": "rainbow1",
    "character_set": "10high",
    "speed": 4
  }
}
```

#### Emergency - Maximum Urgency
```json
{
  "display_config": {
    "position": "fill",
    "mode": "flash",
    "special_effect": "bomb",
    "color": "red",
    "character_set": "fhigh",
    "speed": 5,
    "priority": true,
    "flash": true,
    "duration": 60
  }
}
```

### Complete Display Options Reference

For a comprehensive list of all available display modes, special effects, colors, and character sets, see the [BETABRITE.md](../BETABRITE.md) documentation.

## 💡 Usage Examples

### Basic Alert

```json
{
  "timestamp": 1705329000,
  "level": "info",
  "category": "system",
  "title": "Backup Complete",
  "message": "Daily backup completed successfully",
  "source": "backup_service"
}
```

### Alert with Metadata

```json
{
  "timestamp": 1705329000,
  "level": "warning",
  "category": "system",
  "title": "High CPU Usage",
  "message": "CPU usage has exceeded 85% for 5 minutes",
  "source": "monitoring_system",
  "metadata": {
    "cpu_usage": 87.5,
    "duration_minutes": 5,
    "hostname": "server01",
    "process": "node_exporter"
  }
}
```

### Security Alert with Location

```json
{
  "timestamp": 1705356930,
  "level": "critical",
  "category": "security",
  "title": "Unauthorized Access Attempt",
  "message": "Failed SSH login attempt from suspicious IP",
  "source": "ssh_monitor",
  "metadata": {
    "ip_address": "203.0.113.123",
    "username": "admin",
    "attempts": 5,
    "country": "Unknown",
    "blocked": true
  }
}
```

### Weather Alert

```json
{
  "timestamp": 1705334400,
  "level": "warning",
  "category": "weather",
  "title": "Severe Weather Warning",
  "message": "Thunderstorm warning in effect until 20:00",
  "source": "weather_service",
  "metadata": {
    "warning_type": "thunderstorm",
    "expires_epoch": 1705348800,
    "wind_speed": "65 mph",
    "location": "County Area"
  }
}
```

### Alert with Routing Override

```json
{
  "timestamp": 1705309200,
  "level": "info",
  "category": "automation",
  "title": "Morning Routine Started",
  "message": "Automated morning routine has begun",
  "source": "home_automation",
  "metadata": {
    "routine": "morning",
    "devices_activated": 12,
    "estimated_duration": "15 minutes"
  },
  "routing": {
    "override": true,
    "pathways": ["led_sign", "console"]
  }
}
```

### IoT Device Alert

```json
{
  "timestamp": 1705322700,
  "level": "critical",
  "category": "system",
  "title": "Sensor Offline",
  "message": "Temperature sensor in server room has gone offline",
  "source": "iot_monitor",
  "metadata": {
    "device_id": "temp_sensor_001",
    "device_type": "temperature_sensor",
    "location": "server_room",
    "last_seen_epoch": 1705321800,
    "battery_level": 5,
    "expected_interval": "60 seconds"
  }
}
```

## 🔧 Integration Examples

### Python Script Integration

```python
import json
import paho.mqtt.client as mqtt
from datetime import datetime

class AlertSender:
    def __init__(self, broker="192.168.1.40", port=1883):
        self.broker = broker
        self.port = port
        self.topic = "alerts/input"
    
    def send_alert(self, level, category, title, message, source, metadata=None, routing=None):
        """Send an alert to the Alert Router Service"""
        alert = {
            "timestamp": datetime.now().isoformat(),
            "level": level,
            "category": category,
            "title": title,
            "message": message,
            "source": source
        }
        
        if metadata:
            alert["metadata"] = metadata
        
        if routing:
            alert["routing"] = routing
        
        client = mqtt.Client()
        client.connect(self.broker, self.port, 60)
        client.publish(self.topic, json.dumps(alert))
        client.disconnect()
        
        return alert

# Usage examples
alerter = AlertSender()

# Simple alert
alerter.send_alert(
    level="warning",
    category="system", 
    title="High Memory Usage",
    message="Memory usage at 90%",
    source="memory_monitor"
)

# Alert with metadata
alerter.send_alert(
    level="critical",
    category="security",
    title="Intrusion Detected", 
    message="Motion detected in restricted area",
    source="security_camera",
    metadata={
        "camera_id": "cam_005",
        "zone": "server_room",
        "confidence": 0.95
    }
)

# Alert with routing override
alerter.send_alert(
    level="info",
    category="automation",
    title="System Maintenance",
    message="Scheduled maintenance starting",
    source="maintenance_scheduler",
    routing={
        "override": True,
        "pathways": ["led_sign", "console"]
    }
)
```

### Bash Script Integration

```bash
#!/bin/bash

# Alert sender function
send_alert() {
    local level="$1"
    local category="$2" 
    local title="$3"
    local message="$4"
    local source="$5"
    local timestamp=$(date -Iseconds)
    
    mosquitto_pub -h 192.168.1.40 -t "alerts/input" -m "{
        \"timestamp\": \"$timestamp\",
        \"level\": \"$level\",
        \"category\": \"$category\",
        \"title\": \"$title\",
        \"message\": \"$message\",
        \"source\": \"$source\"
    }"
}

# Usage examples
send_alert "warning" "system" "Disk Space Low" "Root partition at 85%" "disk_monitor"
send_alert "info" "system" "Service Restarted" "Apache service restarted successfully" "service_monitor"
```

### Node.js Integration

```javascript
const mqtt = require('mqtt');

class AlertRouter {
    constructor(brokerUrl = 'mqtt://192.168.1.40:1883') {
        this.client = mqtt.connect(brokerUrl);
        this.topic = 'alerts/input';
    }
    
    sendAlert({level, category, title, message, source, metadata = {}, routing = null}) {
        const alert = {
            timestamp: Math.floor(Date.now() / 1000),
            level,
            category,
            title,
            message,
            source,
            metadata
        };
        
        if (routing) {
            alert.routing = routing;
        }
        
        this.client.publish(this.topic, JSON.stringify(alert));
        return alert;
    }
    
    disconnect() {
        this.client.end();
    }
}

// Usage
const alerter = new AlertRouter();

// Application error
alerter.sendAlert({
    level: 'critical',
    category: 'application',
    title: 'Database Connection Failed',
    message: 'Unable to connect to primary database',
    source: 'webapp',
    metadata: {
        error_code: 'CONN_TIMEOUT',
        database: 'primary_db',
        retry_count: 3
    }
});

alerter.disconnect();
```

## 🧪 Testing

### Send Test Alert via Command Line

```bash
# Simple test
mosquitto_pub -h 192.168.1.40 -t "alerts/input" -m '{
  "timestamp": "'$(date -Iseconds)'",
  "level": "info",
  "category": "test",
  "title": "Test Alert",
  "message": "This is a test message",
  "source": "manual_test"
}'

# Test with metadata
mosquitto_pub -h 192.168.1.40 -t "alerts/input" -m '{
  "timestamp": "'$(date -Iseconds)'",
  "level": "warning", 
  "category": "system",
  "title": "Test System Alert",
  "message": "Testing system alert with metadata",
  "source": "test_suite",
  "metadata": {
    "test_id": "001",
    "environment": "development"
  }
}'
```

### Using Test Scripts

```bash
# Run built-in test alerts
python3 src/alert_router.py --test

# Send custom test alerts
./tests/test_alerts.py

# Docker test alerts  
./tests/docker-test.py
```

## 📊 Monitoring Service Status

The Alert Router publishes its status to `alerts/service/status` every 60 seconds:

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

### Monitor Status via MQTT

```bash
# Subscribe to status updates
mosquitto_sub -h 192.168.1.40 -t "alerts/service/status" -v

# Monitor all alert topics
mosquitto_sub -h 192.168.1.40 -t "alerts/#" -v
```

## 🔍 Troubleshooting

### Validate Alert Format

Use this Python snippet to validate your alert payload:

```python
import json

def validate_alert(alert_json):
    """Validate alert message format"""
    required_fields = ['timestamp', 'level', 'category', 'title', 'message', 'source']
    valid_levels = ['critical', 'warning', 'info', 'debug']
    
    try:
        alert = json.loads(alert_json) if isinstance(alert_json, str) else alert_json
    except json.JSONDecodeError as e:
        return False, f"Invalid JSON: {e}"
    
    # Check required fields
    for field in required_fields:
        if field not in alert:
            return False, f"Missing required field: {field}"
    
    # Validate level
    if alert['level'] not in valid_levels:
        return False, f"Invalid level: {alert['level']}. Must be one of {valid_levels}"
    
    return True, "Valid alert format"

# Test your alert (timestamp must be Unix epoch integer)
alert = {
    "timestamp": 1705315800,
    "level": "info",
    "category": "system",
    "title": "Test",
    "message": "Test message",
    "source": "test"
}

valid, message = validate_alert(alert)
print(f"Alert validation: {message}")
```

### Common Issues

1. **Alert not processed**: Check required fields are present
2. **Wrong routing**: Verify level/category matches routing rules  
3. **MQTT connection**: Ensure broker at 192.168.1.40:1883 is accessible
4. **JSON format**: Validate JSON syntax before sending

## 📚 Additional Resources

- [README.md](README.md) - General usage and installation
- [DOCKER.md](DOCKER.md) - Docker deployment guide
- [CLAUDE.md](CLAUDE.md) - Development commands and architecture
- [ALERTING_SYSTEM_PLAN.md](ALERTING_SYSTEM_PLAN.md) - Complete system architecture