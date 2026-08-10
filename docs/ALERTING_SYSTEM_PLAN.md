# Unified Alerting System Architecture Plan

## Overview

A centralized alerting system that accepts standardized alert messages and routes them to appropriate pathways based on configurable rules. The system uses a publish/subscribe pattern with MQTT as the backbone, with a dedicated Python service handling intelligent routing while maintaining Home Assistant integration for automation and visibility.

## Architecture Components

### 1. Alert Input Interface
**Single Entry Point**: All alerts flow through one standardized MQTT topic
- Topic: `alerts/input`
- Payload: JSON message with standardized schema
- Multiple input sources converge to this single topic
- Payload-level routing overrides supported

### 2. Alert Router Service (Python)
**Central Intelligence**: Standalone Python service using modular pathway system
- Subscribes to `alerts/input` MQTT topic
- Uses PathwayManager to load and manage modular pathways
- Applies configurable routing rules based on level, category, source, metadata
- Processes payload-level routing overrides via `routing.pathways`
- Routes to multiple pathway instances simultaneously (file, email, LED signs, console, MQTT)
- Maintains alert history, statistics, and per-pathway error tracking
- Publishes enhanced state information to `alerts/service/status` for monitoring
- Operates independently with clean pathway abstraction for extensibility

### 3. Modular Pathway System
**Extensible Output Architecture**:
- **FilePathway**: Local file logging with rotation and directory creation
- **EmailPathway**: SMTP and Microsoft Graph API (O365) email notifications with HTML/text formatting
- **LEDSignPathway**: Multi-zone BetaBrite LED sign control with 23+ animation modes, 21+ special effects, 12+ colors, 14+ character sets, intelligent routing, and context-aware display configurations
- **ConsolePathway**: Console/stdout output with configurable formatting
- **MQTTPathway**: Generic MQTT publishing to arbitrary topics
- **Future Pathways**: Audio alerts, visual indicators (smart lights/RGB strips), SMS, webhooks, physical actuators

**Pathway Features**:
- Independent configuration validation and error handling
- Message filtering by level, category, source
- Configurable formatting and pathway limits
- Per-pathway statistics and error tracking
- Zone-based routing (LED signs support multiple zones with different filtering rules)

## Message Schema

**Standardized Alert JSON Structure** - See [sample-payload/](../sample-payload/) for complete examples.

### Required Fields
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

### Optional Fields
```json
{
  "metadata": {
    "priority": true,
    "led_zones": ["living_room", "office"],
    "custom_field": "optional_data"
  },
  "routing": {
    "override": true,
    "pathways": ["led_security", "email_alerts", "file_log"],
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
    "email_alerts": {
      "to_addresses": ["admin@example.com"],
      "subject_template": "ALERT: {title}",
      "html_format": true
    }
  }
}
```

### Core Fields (Required)
- `timestamp`: Unix epoch timestamp (integer, no decimals)
- `level`: Alert severity level
- `category`: Alert category/domain
- `title`: Brief alert title
- `message`: Detailed alert message
- `source`: Structured source object with `id` (required), `type`, and `metadata` fields

### Routing Overrides (Optional)
- `routing.override`: If true, ignore default routing rules
- `routing.pathways`: Specific pathway instances to use
- `routing.exclude`: Exclude specific pathway instances
- `metadata.led_zones`: Target specific LED sign zones (e.g., ["kitchen", "living_room"])

### Pathway-Specific Configuration (Optional)
The `pathway_config` object allows per-pathway customization:

**LED Sign Configuration** (`pathway_config.{pathway_name}.zones.{zone_name}.display_config`):
- `mode`: Animation mode (23 options: rotate, hold, flash, scroll, explode, rollup, rolldown, rollleft, rollright, wipeup, wipedown, wipeleft, wiperight, special, automode, rollin, rollout, wipein, wipeout, compressed_rotate, clock, etc.)
- `special_effect`: Visual effects (21 options: twinkle, sparkle, snow, interlock, switch, slide, spray, starburst, welcome, slots, newsflash, trumpet, cyclecolors, thankyou, nosmoking, dontdrinkanddrive, fishimal, fireworks, turballoon, bomb, etc.)
- `color`: Text color (12 options: red, green, amber, dimred, dimgreen, brown, orange, yellow, rainbow1, rainbow2, colormix, autocolor)
- `character_set`: Font style (14 options: 5high, 5stroke, 7high, 7stroke, 7highfancy, 10high, 7shadow, fhighfancy, fhigh, 7shadowfancy, 5wide, 7wide, 7widefancy, 5widestroke)
- `speed`: Animation speed (1-5, where 1=slowest, 5=fastest)
- `priority`: Interrupt normal display rotation (true/false)
- `duration`: Display duration in seconds (for priority messages)

**Email Configuration** (`pathway_config.{pathway_name}`):
- `to_addresses`: Array of recipient email addresses
- `subject_template`: Custom subject line template
- `html_format`: Send as HTML email (true/false)

**File Configuration** (`pathway_config.{pathway_name}`):
- `filename`: Custom log file path
- `format`: Custom format string for log entries
- `create_directories`: Auto-create directory structure (true/false)

### Alert Levels
- `critical`: Immediate attention required
- `warning`: Important but not urgent
- `notice`: Normal but significant operational events (scheduled tasks, reboots, successful completions)
- `info`: General information
- `debug`: Troubleshooting information

### Categories
- `security`: Security events (motion, breaches, alarms)
- `weather`: Weather alerts (storms, warnings, advisories)
- `system`: System status/health (errors, performance, backups)
- `automation`: Home automation events (schedules, completions)
- `personal`: Personal reminders/notifications (tasks, timers)
- `network`: Network and connectivity issues
- `application`: Application-specific alerts
- `news`: News and information updates

## Input Sources & Integration Patterns

All input sources ultimately publish to the single `alerts/input` MQTT topic using the standardized message schema. Below are common integration patterns for various systems.

### Home Assistant Integration
**Direct Automation Triggers**
```yaml
automation:
  - alias: "Motion Alert"
    trigger:
      platform: state
      entity_id: binary_sensor.front_door_motion
      to: 'on'
    action:
      service: mqtt.publish
      data:
        topic: "alerts/input"
        payload: >
          {
            "timestamp": "{{ now().isoformat() }}",
            "level": "warning",
            "category": "security",
            "title": "Motion Detected",
            "message": "Front door motion sensor activated",
            "source": "home_assistant",
            "metadata": {
              "entity_id": "binary_sensor.front_door_motion",
              "location": "front_door"
            }
          }
```

**Template Sensors for Complex Logic**
```yaml
sensor:
  - platform: template
    sensors:
      system_health_alert:
        value_template: >
          {% if states('sensor.cpu_temperature')|float > 80 %}
            {{ now().isoformat() }}
          {% endif %}
        attribute_templates:
          alert_payload: >
            {
              "level": "critical",
              "category": "system",
              "title": "High CPU Temperature",
              "message": "CPU temperature is {{ states('sensor.cpu_temperature') }}°C"
            }
```

### REST API Submissions
**cURL Command Example**
```bash
curl -X POST "http://homeassistant.local:1883/alerts/input" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": "'$(date -Iseconds)'",
    "level": "info",
    "category": "system",
    "title": "Backup Complete",
    "message": "Daily backup completed successfully",
    "source": "backup_script",
    "metadata": {"backup_size": "2.3GB", "duration": "45min"}
  }'
```

**Python Script Integration**
```python
import json, paho.mqtt.client as mqtt
from datetime import datetime

def send_alert(level, category, title, message, source, metadata=None):
    client = mqtt.Client()
    client.connect("homeassistant.local", 1883, 60)
    
    payload = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "category": category,
        "title": title,
        "message": message,
        "source": source,
        "metadata": metadata or {}
    }
    
    client.publish("alerts/input", json.dumps(payload))
    client.disconnect()

# Usage in monitoring scripts
send_alert("critical", "system", "Disk Space Low", 
           f"Root partition at {usage}%", "disk_monitor")
```

### Syslog/Rsyslog Integration
**Rsyslog Configuration Template**
```bash
# /etc/rsyslog.d/50-alerts.conf
template(name="AlertTemplate" type="list") {
    constant(value="{")
    constant(value="\"timestamp\":\"") property(name="timestamp" dateformat="rfc3339")
    constant(value="\",\"level\":\"warning")
    constant(value="\",\"category\":\"system")
    constant(value="\",\"title\":\"System Log Alert")
    constant(value="\",\"message\":\"") property(name="msg")
    constant(value="\",\"source\":\"syslog")
    constant(value="\",\"metadata\":{\"facility\":\"") property(name="syslogfacility-text")
    constant(value="\",\"severity\":\"") property(name="syslogseverity-text")
    constant(value="\",\"hostname\":\"") property(name="hostname")
    constant(value="\"}}")
}

# Forward critical errors to MQTT
if $syslogseverity <= 2 then {
    action(type="omprog" binary="/usr/local/bin/mqtt-publish.sh"
           template="AlertTemplate")
}
```

**MQTT Publisher Script** (`/usr/local/bin/mqtt-publish.sh`)
```bash
#!/bin/bash
read alert_json
mosquitto_pub -h homeassistant.local -t "alerts/input" -m "$alert_json"
```

### Shell Script Patterns
**Monitoring Script Template**
```bash
#!/bin/bash
send_alert() {
    local level="$1" category="$2" title="$3" message="$4"
    local timestamp=$(date -Iseconds)
    local source="$(basename $0)"
    
    mosquitto_pub -h homeassistant.local -t "alerts/input" -m "{
        \"timestamp\": \"$timestamp\",
        \"level\": \"$level\",
        \"category\": \"$category\",
        \"title\": \"$title\",
        \"message\": \"$message\",
        \"source\": \"$source\"
    }"
}

# Usage examples
if ! ping -c 1 google.com > /dev/null; then
    send_alert "warning" "system" "Network Connectivity" "Internet connection lost"
fi

if [ $(df / | tail -1 | awk '{print $5}' | sed 's/%//') -gt 90 ]; then
    send_alert "critical" "system" "Disk Space Critical" "Root partition over 90% full"
fi
```

### Server Heartbeat Monitoring
**Heartbeat Daemon Script**
```bash
#!/bin/bash
# /usr/local/bin/heartbeat-monitor.sh

HEARTBEAT_FILE="/tmp/service.heartbeat"
TIMEOUT=300  # 5 minutes

while true; do
    if [ -f "$HEARTBEAT_FILE" ]; then
        last_heartbeat=$(stat -c %Y "$HEARTBEAT_FILE")
        current_time=$(date +%s)
        
        if [ $((current_time - last_heartbeat)) -gt $TIMEOUT ]; then
            mosquitto_pub -h homeassistant.local -t "alerts/input" -m "{
                \"timestamp\": \"$(date -Iseconds)\",
                \"level\": \"critical\",
                \"category\": \"system\",
                \"title\": \"Service Heartbeat Lost\",
                \"message\": \"No heartbeat received for $((current_time - last_heartbeat)) seconds\",
                \"source\": \"heartbeat_monitor\",
                \"metadata\": {\"service\": \"critical_service\", \"timeout\": $TIMEOUT}
            }"
        fi
    fi
    sleep 60
done
```

### Application Integration Patterns
**Web Application Error Handler**
```javascript
// Node.js/Express error handling
const mqtt = require('mqtt');
const client = mqtt.connect('mqtt://homeassistant.local');

function sendAlert(level, category, title, message, metadata = {}) {
    const alert = {
        timestamp: new Date().toISOString(),
        level,
        category,
        title,
        message,
        source: 'webapp',
        metadata
    };
    
    client.publish('alerts/input', JSON.stringify(alert));
}

// Usage in error handlers
app.use((err, req, res, next) => {
    if (err.status >= 500) {
        sendAlert('critical', 'system', 'Application Error', 
                 err.message, {url: req.url, method: req.method});
    }
});
```

**Database Monitoring**
```sql
-- PostgreSQL function to send alerts
CREATE OR REPLACE FUNCTION send_db_alert(
    p_level text,
    p_title text,
    p_message text
) RETURNS void AS $$
BEGIN
    PERFORM pg_notify('mqtt_alerts', json_build_object(
        'timestamp', now(),
        'level', p_level,
        'category', 'database',
        'title', p_title,
        'message', p_message,
        'source', 'postgresql'
    )::text);
END;
$$ LANGUAGE plpgsql;

-- Trigger on table size threshold
CREATE OR REPLACE FUNCTION check_table_size()
RETURNS trigger AS $$
BEGIN
    IF (SELECT pg_total_relation_size('large_table')) > 1000000000 THEN
        PERFORM send_db_alert('warning', 'Large Table Growth', 
                             'Table large_table exceeded 1GB');
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
```

### IoT Device Integration
**ESP32 Device Example**
```cpp
// ESP32 alert sender
#include <WiFi.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

void sendAlert(String level, String category, String title, String message) {
    StaticJsonDocument<512> alert;
    alert["timestamp"] = "2024-01-15T10:30:00Z";  // Use NTP time
    alert["level"] = level;
    alert["category"] = category;
    alert["title"] = title;
    alert["message"] = message;
    alert["source"] = "sensor_device";
    alert["metadata"]["device_id"] = WiFi.macAddress();
    
    String alertString;
    serializeJson(alert, alertString);
    
    client.publish("alerts/input", alertString.c_str());
}

// Usage
if (temperature > 50.0) {
    sendAlert("critical", "system", "Temperature Alert", 
             "Device temperature: " + String(temperature) + "°C");
}
```

### Third-Party Service Webhooks
**Generic Webhook Receiver** (Python Flask)
```python
from flask import Flask, request
import json, paho.mqtt.client as mqtt

app = Flask(__name__)

@app.route('/webhook/<service>', methods=['POST'])
def webhook_handler(service):
    data = request.json
    
    # Transform webhook data to alert format
    alert = {
        "timestamp": datetime.now().isoformat(),
        "level": map_severity(data.get('severity', 'info')),
        "category": "external",
        "title": f"{service.title()} Alert",
        "message": data.get('message', 'External service alert'),
        "source": f"webhook_{service}",
        "metadata": data
    }
    
    client = mqtt.Client()
    client.connect("homeassistant.local", 1883)
    client.publish("alerts/input", json.dumps(alert))
    client.disconnect()
    
    return "OK"

# Usage: curl -X POST webhook_server:5000/webhook/github -d '{"message":"Build failed"}'
```

### SNMP Trap Receiver
**SNMP Trap to Alert Bridge**
```python
#!/usr/bin/env python3
# snmp-trap-receiver.py
from pysnmp.entity import engine, config
from pysnmp.carrier.asyncore import dgram
from pysnmp.entity.rfc3413 import ntfrcv
import json, paho.mqtt.client as mqtt
from datetime import datetime

def trap_handler(snmpEngine, stateReference, contextEngineId, contextName,
                 varBinds, cbCtx):
    # Parse SNMP trap data
    oid_values = {}
    for name, val in varBinds:
        oid_values[str(name)] = str(val)
    
    # Map common SNMP severity to alert levels
    severity_map = {
        '1': 'critical',  # Critical
        '2': 'critical',  # Major  
        '3': 'warning',   # Minor
        '4': 'info',      # Warning
        '5': 'info'       # Informational
    }
    
    alert = {
        "timestamp": datetime.now().isoformat(),
        "level": severity_map.get(oid_values.get('1.3.6.1.6.3.1.1.4.1.0', '4'), 'info'),
        "category": "network",
        "title": "SNMP Trap Received",
        "message": oid_values.get('1.3.6.1.2.1.1.3.0', 'SNMP trap notification'),
        "source": "snmp_trap_receiver",
        "metadata": {
            "trap_oid": oid_values.get('1.3.6.1.6.3.1.1.4.1.0'),
            "agent_addr": oid_values.get('1.3.6.1.6.3.1.1.4.3.0'),
            "all_oids": oid_values
        }
    }
    
    # Publish to MQTT
    client = mqtt.Client()
    client.connect("homeassistant.local", 1883)
    client.publish("alerts/input", json.dumps(alert))
    client.disconnect()

# Setup SNMP trap receiver
snmpEngine = engine.SnmpEngine()
config.addTransport(snmpEngine, dgram.udpDomainName,
                   dgram.UdpTransport().openServerMode(('0.0.0.0', 162)))
config.addV1System(snmpEngine, 'my-area', 'public')
ntfrcv.NotificationReceiver(snmpEngine, trap_handler)
snmpEngine.transportDispatcher.jobStarted(1)

try:
    snmpEngine.transportDispatcher.runDispatcher()
except KeyboardInterrupt:
    snmpEngine.transportDispatcher.closeDispatcher()
```

### Dedicated Syslog Server
**Syslog UDP Server to MQTT Bridge**
```python
#!/usr/bin/env python3
# syslog-mqtt-bridge.py
import socketserver, re, json
import paho.mqtt.client as mqtt
from datetime import datetime

class SyslogUDPHandler(socketserver.BaseRequestHandler):
    # RFC3164 syslog regex pattern
    SYSLOG_PATTERN = re.compile(
        r'<(?P<priority>\d+)>(?P<timestamp>\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+'
        r'(?P<hostname>\S+)\s+(?P<tag>[^:]+):\s*(?P<message>.*)'
    )
    
    def handle(self):
        data = bytes.decode(self.request[0].strip())
        match = self.SYSLOG_PATTERN.match(data)
        
        if match:
            priority = int(match.group('priority'))
            facility = priority >> 3
            severity = priority & 0x07
            
            # Map syslog severity to alert levels
            severity_map = {0: 'critical', 1: 'critical', 2: 'critical',
                          3: 'warning', 4: 'warning', 5: 'info',
                          6: 'info', 7: 'debug'}
            
            # Filter critical/warning only
            if severity <= 4:
                alert = {
                    "timestamp": datetime.now().isoformat(),
                    "level": severity_map.get(severity, 'info'),
                    "category": "system",
                    "title": f"Syslog Alert from {match.group('hostname')}",
                    "message": match.group('message'),
                    "source": "syslog_server",
                    "metadata": {
                        "hostname": match.group('hostname'),
                        "tag": match.group('tag'),
                        "facility": facility,
                        "severity": severity,
                        "raw_message": data
                    }
                }
                
                client = mqtt.Client()
                client.connect("homeassistant.local", 1883)
                client.publish("alerts/input", json.dumps(alert))
                client.disconnect()

if __name__ == "__main__":
    server = socketserver.UDPServer(('0.0.0.0', 514), SyslogUDPHandler)
    print("Syslog server listening on UDP 514...")
    server.serve_forever()
```

### Log File Monitoring
**File Watcher with Pattern Matching**
```python
#!/usr/bin/env python3
# log-file-monitor.py
import time, re, json
import paho.mqtt.client as mqtt
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class LogFileHandler(FileSystemEventHandler):
    def __init__(self, patterns_config):
        self.patterns = patterns_config
        self.file_positions = {}
        
    def on_modified(self, event):
        if event.is_directory:
            return
            
        file_path = event.src_path
        if file_path in self.patterns:
            self.process_log_file(file_path)
    
    def process_log_file(self, file_path):
        # Track file position to only read new lines
        if file_path not in self.file_positions:
            self.file_positions[file_path] = 0
            
        with open(file_path, 'r') as f:
            f.seek(self.file_positions[file_path])
            new_lines = f.readlines()
            self.file_positions[file_path] = f.tell()
            
        # Process each new line
        for line in new_lines:
            self.check_patterns(file_path, line.strip())
    
    def check_patterns(self, file_path, line):
        for pattern_config in self.patterns[file_path]:
            if re.search(pattern_config['regex'], line):
                alert = {
                    "timestamp": datetime.now().isoformat(),
                    "level": pattern_config['level'],
                    "category": pattern_config.get('category', 'system'),
                    "title": f"Log Alert: {pattern_config['title']}",
                    "message": line,
                    "source": "log_file_monitor",
                    "metadata": {
                        "file_path": file_path,
                        "pattern_name": pattern_config['title'],
                        "matched_regex": pattern_config['regex']
                    }
                }
                
                client = mqtt.Client()
                client.connect("homeassistant.local", 1883)
                client.publish("alerts/input", json.dumps(alert))
                client.disconnect()

# Configuration for log patterns
LOG_PATTERNS = {
    "/var/log/apache2/error.log": [
        {
            "regex": r"Fatal error|segmentation fault|core dumped",
            "level": "critical",
            "category": "system",
            "title": "Apache Critical Error"
        },
        {
            "regex": r"File does not exist|Permission denied",
            "level": "warning", 
            "category": "system",
            "title": "Apache Access Issue"
        }
    ],
    "/var/log/auth.log": [
        {
            "regex": r"Failed password.*ssh",
            "level": "warning",
            "category": "security", 
            "title": "SSH Login Failure"
        },
        {
            "regex": r"Invalid user.*ssh",
            "level": "warning",
            "category": "security",
            "title": "SSH Invalid User"
        }
    ],
    "/home/user/app/application.log": [
        {
            "regex": r"ERROR|FATAL|Exception",
            "level": "critical",
            "category": "application",
            "title": "Application Error"
        }
    ]
}

if __name__ == "__main__":
    event_handler = LogFileHandler(LOG_PATTERNS)
    observer = Observer()
    
    # Watch directories containing the log files
    watched_dirs = set()
    for file_path in LOG_PATTERNS.keys():
        dir_path = '/'.join(file_path.split('/')[:-1])
        if dir_path not in watched_dirs:
            observer.schedule(event_handler, dir_path, recursive=False)
            watched_dirs.add(dir_path)
    
    observer.start()
    print(f"Monitoring log files: {list(LOG_PATTERNS.keys())}")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
```

### Windows Event Log Integration
**PowerShell Event Log to MQTT**
```powershell
# windows-eventlog-monitor.ps1
param(
    [string]$MQTTBroker = "homeassistant.local",
    [int]$MQTTPort = 1883
)

# Install Posh-MQTT if not available
if (!(Get-Module -ListAvailable -Name Posh-MQTT)) {
    Install-Module -Name Posh-MQTT -Force
}

Import-Module Posh-MQTT

function Send-MQTTAlert {
    param($Level, $Category, $Title, $Message, $Metadata)
    
    $Alert = @{
        timestamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
        level = $Level
        category = $Category  
        title = $Title
        message = $Message
        source = "windows_eventlog"
        metadata = $Metadata
    } | ConvertTo-Json -Compress
    
    Publish-MQTTMessage -Topic "alerts/input" -Message $Alert -MQTTBroker $MQTTBroker -Port $MQTTPort
}

# Monitor critical Windows events
$Events = @(
    @{LogName="System"; Level=1,2; EventID=@(1074,6005,6006,6008)}, # System events
    @{LogName="Security"; Level=2; EventID=@(4625,4648,4740)},      # Security failures  
    @{LogName="Application"; Level=1,2}                              # Application errors
)

foreach ($EventConfig in $Events) {
    Register-WinEvent -FilterHashtable $EventConfig -Action {
        $Event = $Event.SourceEventArgs.NewEvent
        
        $LevelMap = @{1="critical"; 2="critical"; 3="warning"; 4="info"}
        $Level = $LevelMap[$Event.Level]
        $Category = switch ($Event.LogName) {
            "Security" { "security" }
            "System" { "system" }
            default { "application" }
        }
        
        Send-MQTTAlert -Level $Level -Category $Category `
            -Title "Windows Event $($Event.Id)" `
            -Message $Event.Message `
            -Metadata @{
                event_id = $Event.Id
                log_name = $Event.LogName
                source = $Event.Source
                computer = $Event.MachineName
            }
    }
}

Write-Host "Windows Event Log monitoring started..."
try {
    while ($true) { Start-Sleep -Seconds 30 }
} finally {
    Get-EventSubscriber | Unregister-Event
}
```

### Scheduled Alert Patterns
**Cron-based Maintenance Reminders**
```bash
# Crontab entry: 0 9 1 * * /usr/local/bin/maintenance-reminder.sh
#!/bin/bash
mosquitto_pub -h homeassistant.local -t "alerts/input" -m "{
    \"timestamp\": \"$(date -Iseconds)\",
    \"level\": \"info\",
    \"category\": \"personal\",
    \"title\": \"Monthly Maintenance\",
    \"message\": \"Time for monthly system maintenance tasks\",
    \"source\": \"cron_scheduler\",
    \"metadata\": {\"task_type\": \"maintenance\", \"recurring\": \"monthly\"}
}"
```

**Log Rotation Alert on Size/Age**
```bash
#!/bin/bash
# log-rotation-monitor.sh
for log_file in /var/log/*.log; do
    size=$(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file" 2>/dev/null)
    age=$(find "$log_file" -mtime +7 2>/dev/null)
    
    if [ "$size" -gt 104857600 ]; then  # 100MB
        mosquitto_pub -h homeassistant.local -t "alerts/input" -m "{
            \"timestamp\": \"$(date -Iseconds)\",
            \"level\": \"warning\",
            \"category\": \"system\",
            \"title\": \"Large Log File\",
            \"message\": \"$log_file is $(($size/1024/1024))MB\",
            \"source\": \"log_rotation_monitor\",
            \"metadata\": {\"file_path\": \"$log_file\", \"size_bytes\": $size}
        }"
    fi
done
```

## Alert Router Service Configuration

### Main Configuration File (`alert_router_config.json`)
```json
{
  "service": {
    "mqtt_broker": "homeassistant.local",
    "mqtt_port": 1883,
    "input_topic": "alerts/input",
    "status_topic": "alerts/service/status",
    "log_level": "INFO",
    "log_file": "/var/log/alert-router.log"
  },
  "zones": {
    "main_floor": ["kitchen", "living_room", "office"],
    "upstairs": ["bedroom", "bathroom"],
    "security_zone": ["front_door", "back_door", "windows"],
    "outdoor": ["garage", "garden", "driveway"]
  },
  "pathway_types": {
    "visual": ["led_sign", "smart_lights", "display_panel"],
    "audible": ["tts_announce", "sound_alert", "chime"],
    "notification": ["mobile_push", "email", "ha_notification"],
    "logging": ["log_file", "database"],
    "external": ["webhook", "api_call"]
  },
  "routing_rules": [
    {
      "name": "Security Critical",
      "conditions": {
        "level": ["critical"],
        "category": ["security"]
      },
      "pathways": {
        "all_zones": {
          "visual": ["led_sign", "smart_lights"],
          "audible": ["tts_announce"],
          "notification": ["mobile_push", "ha_notification"]
        }
      },
      "priority": 100
    },
    {
      "name": "System Warnings",
      "conditions": {
        "level": ["warning"],
        "category": ["system"]
      },
      "pathways": {
        "zones": ["main_floor"],
        "types": ["visual", "logging"],
        "specific": ["led_sign", "log_file"]
      },
      "priority": 50
    },
    {
      "name": "Weather Info",
      "conditions": {
        "level": ["info"],
        "category": ["weather"]
      },
      "pathways": {
        "zones": ["main_floor"],
        "types": ["visual"],
        "exclude": ["mobile_push"]
      },
      "time_conditions": {
        "time_range": "06:00-23:00",
        "days": ["all"]
      },
      "priority": 10
    }
  ],
  "pathway_configs": {
    "led_sign": {
      "type": "mqtt",
      "zones": {
        "kitchen": "ledSign/kitchen/message",
        "living_room": "ledSign/livingroom/message",
        "office": "ledSign/office/message"
      },
      "format_template": "[{color}]{title}: {message}",
      "color_mapping": {
        "critical": "red",
        "warning": "amber", 
        "info": "green"
      },
      "betabrite_mappings": {
        "display_modes": {
          "rotate": "a", "hold": "b", "flash": "c", "rollup": "e", "rolldown": "f",
          "rollleft": "g", "rollright": "h", "wipeup": "i", "wipedown": "j",
          "wipeleft": "k", "wiperight": "l", "scroll": "m", "special": "n",
          "automode": "o", "rollin": "p", "rollout": "q", "wipein": "r",
          "wipeout": "s", "compressed_rotate": "t", "explode": "u", "clock": "v"
        },
        "special_effects": {
          "twinkle": "0", "sparkle": "1", "snow": "2", "interlock": "3",
          "switch": "4", "slide": "5", "spray": "6", "starburst": "7",
          "welcome": "8", "slots": "9", "newsflash": "A", "trumpet": "B",
          "cyclecolors": "C", "thankyou": "S", "nosmoking": "U",
          "dontdrinkanddrive": "V", "fishimal": "W", "fireworks": "X",
          "turballoon": "Y", "bomb": "Z"
        },
        "colors": {
          "red": "1", "green": "2", "amber": "3", "dimred": "4",
          "dimgreen": "5", "brown": "6", "orange": "7", "yellow": "8",
          "rainbow1": "9", "rainbow2": "A", "colormix": "B", "autocolor": "C"
        },
        "character_sets": {
          "5high": "1", "5stroke": "2", "7high": "3", "7stroke": "4",
          "7highfancy": "5", "10high": "6", "7shadow": "7", "fhighfancy": "8",
          "fhigh": "9", "7shadowfancy": ":", "5wide": ";", "7wide": "<",
          "7widefancy": "=", "5widestroke": ">"
        },
        "positions": {
          "midline": " ", "topline": "\"", "botline": "&", "fill": "0",
          "left": "1", "right": "2"
        }
      },
      "alert_level_presets": {
        "critical": {
          "mode": "explode", "special_effect": "bomb", "color": "red",
          "character_set": "10high", "position": "fill", "speed": 5,
          "priority": true, "flash": true
        },
        "warning": {
          "mode": "newsflash", "special_effect": "trumpet", "color": "amber",
          "character_set": "7high", "position": "topline", "speed": 3
        },
        "info": {
          "mode": "scroll", "special_effect": "twinkle", "color": "green",
          "character_set": "7high", "position": "midline", "speed": 2
        },
        "debug": {
          "mode": "hold", "special_effect": "none", "color": "dimgreen",
          "character_set": "5high", "position": "botline", "speed": 1
        }
      },
      "category_themes": {
        "security": {
          "effects": ["starburst", "bomb", "newsflash"],
          "modes": ["flash", "explode"], "colors": ["red", "amber"]
        },
        "weather": {
          "effects": ["snow", "spray", "twinkle"],
          "modes": ["scroll", "wipedown"], "colors": ["amber", "yellow", "autocolor"]
        },
        "system": {
          "effects": ["sparkle", "cyclecolors"],
          "modes": ["rotate", "compressed_rotate"], "colors": ["green", "amber", "red"]
        },
        "celebration": {
          "effects": ["fireworks", "welcome", "thankyou"],
          "modes": ["explode", "rollin"], "colors": ["rainbow1", "colormix"]
        }
      }
    },
    "smart_lights": {
      "type": "homeassistant",
      "service": "light.turn_on",
      "zones": {
        "kitchen": ["light.kitchen_strip", "light.under_cabinet"],
        "living_room": ["light.floor_lamp", "light.ceiling"],
        "office": ["light.desk_lamp"]
      },
      "color_mapping": {
        "critical": {"rgb_color": [255, 0, 0], "brightness": 255},
        "warning": {"rgb_color": [255, 165, 0], "brightness": 200},
        "info": {"rgb_color": [0, 255, 0], "brightness": 150}
      },
      "flash_pattern": {
        "critical": {"effect": "colorloop", "duration": 30},
        "warning": {"effect": "flash", "duration": 10}
      }
    },
    "tts_announce": {
      "type": "homeassistant",
      "service": "tts.google_translate_say",
      "zones": {
        "main_floor": "media_player.living_room_speaker",
        "upstairs": "media_player.bedroom_speaker"
      },
      "message_template": "Alert: {title}. {message}",
      "conditions": {
        "time_range": "08:00-22:00",
        "max_frequency": "1/5min"
      }
    },
    "mobile_push": {
      "type": "homeassistant",
      "service": "notify.mobile_app",
      "target": "notify.all_devices",
      "title_template": "[{level.upper()}] {title}",
      "message_template": "{message}",
      "conditions": {
        "min_level": "warning"
      }
    },
    "ha_notification": {
      "type": "homeassistant", 
      "service": "notify.persistent_notification",
      "title_template": "{category}: {title}",
      "message_template": "{message}\n\nSource: {source}",
      "mqtt_topic": "homeassistant/notify"
    },
    "email_alert": {
      "type": "smtp",
      "server": "smtp.gmail.com",
      "port": 587,
      "username": "alerts@domain.com",
      "to": ["admin@domain.com"],
      "subject_template": "[ALERT-{level.upper()}] {category}: {title}",
      "body_template": "Alert Details:\n\nTitle: {title}\nMessage: {message}\nLevel: {level}\nCategory: {category}\nSource: {source}\nTime: {timestamp}",
      "conditions": {
        "min_level": "critical"
      }
    },
    "log_file": {
      "type": "file",
      "path": "/var/log/alerts/alerts.log",
      "format": "{timestamp} [{level}] [{category}] {source}: {title} - {message}",
      "rotation": {
        "max_size": "10MB",
        "backup_count": 5
      }
    },
    "sound_alert": {
      "type": "mqtt",
      "topic": "sound/alerts/{zone}",
      "sound_mapping": {
        "critical": "siren.wav",
        "warning": "chime.wav",
        "info": "ding.wav"
      }
    }
  },
  "homeassistant_integration": {
    "enabled": true,
    "api_url": "http://homeassistant.local:8123/api",
    "token": "your_long_lived_access_token",
    "status_entity": "sensor.alert_router_status",
    "stats_topic": "alerts/stats"
  }
}
```

## Python Alert Router Service Implementation

### Core Service Architecture (Recommended)
**Components**:
- Python daemon service with MQTT client
- JSON configuration file with hot-reload capability
- Multi-threaded pathway processing
- Home Assistant API integration
- Systemd service for reliability and auto-start
- Built-in logging, statistics, and health monitoring

**Pros**: 
- Independent operation (no Home Assistant dependency)
- Complex routing logic with zone/type support
- Payload-level routing overrides
- Direct device communication
- Easier testing and debugging
- Scalable and maintainable

**Cons**: Additional service to maintain

### Python Service Structure
```python
#!/usr/bin/env python3
# alert-router-service.py

import json, logging, threading, time
import paho.mqtt.client as mqtt
from datetime import datetime
from typing import Dict, List, Any
import requests  # For Home Assistant API calls

class AlertRouter:
    def __init__(self, config_file: str):
        self.config = self.load_config(config_file)
        self.mqtt_client = mqtt.Client()
        self.setup_mqtt()
        self.setup_logging()
        self.stats = {
            'alerts_processed': 0,
            'alerts_routed': 0,
            'start_time': datetime.now(),
            'last_alert': None
        }
        
    def load_config(self, config_file: str) -> Dict:
        with open(config_file, 'r') as f:
            return json.load(f)
    
    def setup_mqtt(self):
        self.mqtt_client.on_connect = self.on_connect
        self.mqtt_client.on_message = self.on_message
        broker = self.config['service']['mqtt_broker']
        port = self.config['service']['mqtt_port']
        self.mqtt_client.connect(broker, port, 60)
    
    def on_connect(self, client, userdata, flags, rc):
        logging.info(f"Connected to MQTT broker with result code {rc}")
        client.subscribe(self.config['service']['input_topic'])
        self.publish_service_status('online')
    
    def on_message(self, client, userdata, msg):
        try:
            alert = json.loads(msg.payload.decode())
            self.process_alert(alert)
            self.stats['alerts_processed'] += 1
            self.stats['last_alert'] = datetime.now()
        except Exception as e:
            logging.error(f"Error processing alert: {e}")
    
    def process_alert(self, alert: Dict[str, Any]):
        # Apply routing logic
        if alert.get('routing', {}).get('override', False):
            pathways = self.process_routing_override(alert)
        else:
            pathways = self.apply_routing_rules(alert)
        
        # Process each pathway in separate thread
        for pathway_config in pathways:
            thread = threading.Thread(
                target=self.send_pathway,
                args=(pathway_config, alert)
            )
            thread.start()
    
    def apply_routing_rules(self, alert: Dict) -> List[Dict]:
        matching_pathways = []
        
        # Sort rules by priority (highest first)
        sorted_rules = sorted(
            self.config['routing_rules'],
            key=lambda x: x.get('priority', 0),
            reverse=True
        )
        
        for rule in sorted_rules:
            if self.matches_conditions(alert, rule['conditions']):
                pathways = self.resolve_pathways(rule['pathways'])
                matching_pathways.extend(pathways)
                break  # Use first matching rule
        
        return matching_pathways
    
    def matches_conditions(self, alert: Dict, conditions: Dict) -> bool:
        # Check level
        if 'level' in conditions:
            if alert['level'] not in conditions['level']:
                return False
        
        # Check category
        if 'category' in conditions:
            if alert['category'] not in conditions['category']:
                return False
        
        # Check time conditions
        if 'time_conditions' in conditions:
            if not self.check_time_conditions(conditions['time_conditions']):
                return False
        
        return True
    
    def resolve_pathways(self, pathway_spec: Dict) -> List[Dict]:
        resolved = []
        
        # Handle zone-based pathways
        if 'zones' in pathway_spec:
            for zone in pathway_spec['zones']:
                zone_pathways = self.get_zone_pathways(zone, pathway_spec.get('types', []))
                resolved.extend(zone_pathways)
        
        # Handle type-based pathways
        if 'types' in pathway_spec:
            for pathway_type in pathway_spec['types']:
                type_pathways = self.get_type_pathways(pathway_type)
                resolved.extend(type_pathways)
        
        # Handle specific pathways
        if 'specific' in pathway_spec:
            for pathway_name in pathway_spec['specific']:
                if pathway_name in self.config['pathway_configs']:
                    resolved.append({
                        'name': pathway_name,
                        'config': self.config['pathway_configs'][pathway_name]
                    })
        
        return resolved
    
    def send_pathway(self, pathway_config: Dict, alert: Dict):
        try:
            pathway_type = pathway_config['config']['type']
            
            if pathway_type == 'mqtt':
                self.send_mqtt_pathway(pathway_config, alert)
            elif pathway_type == 'homeassistant':
                self.send_homeassistant_pathway(pathway_config, alert)
            elif pathway_type == 'smtp':
                self.send_email_pathway(pathway_config, alert)
            elif pathway_type == 'file':
                self.send_file_pathway(pathway_config, alert)
            
            self.stats['alerts_routed'] += 1
            
        except Exception as e:
            logging.error(f"Error sending pathway {pathway_config['name']}: {e}")
    
    def send_mqtt_pathway(self, pathway_config: Dict, alert: Dict):
        config = pathway_config['config']
        
        # Format message
        if 'format_template' in config:
            color = config.get('color_mapping', {}).get(alert['level'], 'white')
            message = config['format_template'].format(
                color=color,
                title=alert['title'],
                message=alert['message'],
                level=alert['level']
            )
        else:
            message = alert['message']
        
        # Send to appropriate zones
        if 'zones' in config:
            for zone, topic in config['zones'].items():
                self.mqtt_client.publish(topic, message)
        else:
            topic = config.get('topic', f"alerts/pathway/{pathway_config['name']}")
            self.mqtt_client.publish(topic, message)
    
    def send_homeassistant_pathway(self, pathway_config: Dict, alert: Dict):
        if not self.config['homeassistant_integration']['enabled']:
            return
        
        config = pathway_config['config']
        ha_url = self.config['homeassistant_integration']['api_url']
        ha_token = self.config['homeassistant_integration']['token']
        
        headers = {
            'Authorization': f'Bearer {ha_token}',
            'Content-Type': 'application/json'
        }
        
        # Format service call data
        service_data = {
            'title': config.get('title_template', '{title}').format(**alert),
            'message': config.get('message_template', '{message}').format(**alert)
        }
        
        # Make API call
        service_url = f"{ha_url}/services/{config['service'].replace('.', '/')}"
        response = requests.post(service_url, json=service_data, headers=headers)
        
        if response.status_code != 200:
            logging.error(f"Home Assistant API error: {response.status_code}")
    
    def publish_service_status(self, status: str):
        status_data = {
            'status': status,
            'timestamp': datetime.now().isoformat(),
            'stats': self.stats
        }
        
        status_topic = self.config['service']['status_topic']
        self.mqtt_client.publish(status_topic, json.dumps(status_data))
    
    def run(self):
        logging.info("Starting Alert Router Service")
        self.mqtt_client.loop_start()
        
        try:
            while True:
                # Publish stats every minute
                self.publish_service_status('running')
                time.sleep(60)
                
        except KeyboardInterrupt:
            logging.info("Shutting down Alert Router Service")
            self.publish_service_status('offline')
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()

if __name__ == "__main__":
    router = AlertRouter('/etc/alert-router/config.json')
    router.run()
```

### Systemd Service Configuration
```ini
# /etc/systemd/system/alert-router.service
[Unit]
Description=Alert Router Service
After=network.target
Requires=network.target

[Service]
Type=simple
User=alert-router
Group=alert-router
WorkingDirectory=/opt/alert-router
ExecStart=/usr/bin/python3 /opt/alert-router/alert-router-service.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### Alternative Implementation Options

#### Option 2: Home Assistant Integration
**Components**:
- MQTT trigger automation for `alerts/input`
- Python script service calls for complex routing
- Template sensors for alert processing
- Multiple service calls for pathway routing

**Pros**: Native integration, GUI configuration, existing infrastructure
**Cons**: Complex automations, Home Assistant dependency

#### Option 3: Node-RED Integration
**Components**:
- Node-RED flow for alert processing
- JSON configuration node
- MQTT input/pathway nodes
- Home Assistant integration nodes

**Pros**: Visual flow design, easy modification
**Cons**: Additional dependency, limited scalability

### Home Assistant Integration & Visibility

While the Python service operates independently, it maintains full visibility with Home Assistant through MQTT topics and API integration:

**Service Status Monitoring**
```yaml
# Home Assistant configuration.yaml
mqtt:
  sensor:
    - name: "Alert Router Status"
      state_topic: "alerts/service/status"
      value_template: "{{ value_json.status }}"
      json_attributes_topic: "alerts/service/status"
      json_attributes_template: "{{ value_json.stats | tojson }}"
      
    - name: "Alerts Processed Today"
      state_topic: "alerts/service/status" 
      value_template: "{{ value_json.stats.alerts_processed }}"
      
    - name: "Last Alert Time"
      state_topic: "alerts/service/status"
      value_template: "{{ value_json.stats.last_alert }}"
      device_class: timestamp
```

**Alert History Tracking**
```yaml
# Track all processed alerts for Home Assistant automations
automation:
  - alias: "Log Alert to Home Assistant"
    trigger:
      platform: mqtt
      topic: "alerts/input"
    action:
      - service: logbook.log
        data:
          name: "Alert System"
          message: "{{ trigger.payload_json.title }}: {{ trigger.payload_json.message }}"
          entity_id: sensor.alert_router_status
      
      - service: input_text.set_value
        target:
          entity_id: input_text.last_alert
        data:
          value: "{{ trigger.payload_json.title }}"
```

**Service Control Dashboard**
```yaml
# Lovelace dashboard card for alert router control
type: entities
title: Alert Router Service
entities:
  - entity: sensor.alert_router_status
    name: Service Status
  - entity: sensor.alerts_processed_today
    name: Alerts Processed
  - entity: sensor.last_alert_time
    name: Last Alert
  - type: divider
  - entity: input_boolean.alert_router_maintenance_mode
    name: Maintenance Mode
  - entity: button.restart_alert_router
    name: Restart Service
```

**Home Assistant Automation Integration**
```yaml
# Example: Additional Home Assistant automation triggered by alert service
automation:
  - alias: "Critical Alert - Additional Actions"
    trigger:
      platform: mqtt
      topic: "alerts/input"
    condition:
      condition: template
      value_template: "{{ trigger.payload_json.level == 'critical' }}"
    action:
      # Additional actions beyond what the service handles
      - service: script.emergency_protocol
      - service: notify.family_group
        data:
          message: "CRITICAL ALERT: {{ trigger.payload_json.title }}"
      - service: scene.turn_on
        target:
          entity_id: scene.emergency_lighting
```

## Network Discovery & Extensibility

### mDNS Service Discovery
```json
{
  "mdns_services": {
    "alert_endpoints": {
      "service_type": "_alert._tcp",
      "discovery_topic": "alerts/discovery",
      "capabilities": ["led_display", "sound_alert", "visual_notification"]
    }
  }
}
```

### Comprehensive Output Channel Ideas

#### Audible Alerts
- **TTS (Text-to-Speech)**: Home Assistant TTS service, Google Nest/Echo devices
- **Sound Effects**: Predefined audio files via media players, Sonos speakers
- **Chimes/Bells**: MQTT-controlled doorbells, Westminster chimes
- **Buzzers/Sirens**: ESP32-controlled piezo buzzers, car alarm sirens
- **Voice Announcements**: Custom audio messages with alert details

#### Visual Indicators
- **Smart Lighting**: Philips Hue, LIFX, Govee - color-coded by alert level
- **RGB LED Strips**: Addressable LEDs for room ambiance alerts
- **Display Panels**: E-ink displays, small OLED screens showing alert text
- **TV Overlays**: Fire TV notifications, Chromecast overlays
- **Status Lights**: Dedicated alert towers with red/amber/green indicators
- **Window Displays**: LED matrix panels visible from outside

#### Home Assistant Integration
- **Entity State Changes**: Turn switches on/off, adjust thermostats
- **Scene Activation**: Trigger "Alert" scenes (lights, music, etc.)
- **Input Boolean Toggles**: Set flags for other automations to react
- **Counter Increments**: Track alert frequencies per category
- **Template Sensors**: Create dynamic sensors based on alert data

#### External Communications
- **Email**: SMTP notifications with HTML formatting and attachments
- **SMS**: Twilio, carrier gateways for text message alerts
- **Messaging Apps**: 
  - Slack channels with threaded conversations
  - Discord webhooks with embed formatting
  - Telegram bot messages with inline keyboards
  - WhatsApp Business API integration
- **Voice Calls**: Automated phone calls for critical alerts
- **Push Notifications**: Custom mobile apps, browser notifications

#### Physical World Integration
- **Mechanical Actuators**: 
  - Servo motors to move physical alert flags
  - Relay-controlled desk bells or physical chimes
  - Vibrating motor alerts (silent but noticeable)
- **Environmental Controls**:
  - Flash room lights in patterns
  - Adjust HVAC to get attention (brief temp change)
  - Control window blinds/shades for visual signals
- **Wearable Alerts**: 
  - Smartwatch notifications
  - Bluetooth vibrating devices
  - LED badges or pins

#### Network-Based Alerts
- **Network Broadcasts**:
  - UDP broadcast messages for custom receivers
  - Wake-on-LAN packets to wake sleeping devices
  - DHCP reservation changes to trigger network events
- **Protocol-Specific**:
  - SNMP traps to network monitoring systems
  - Syslog messages to centralized logging
  - HTTP webhooks to cloud services or local endpoints

#### Creative/Novelty Alerts
- **Pet Integration**: Automated pet feeders with alert sounds
- **IoT Appliances**: Coffee maker starts brewing on critical alerts
- **Garden Systems**: Sprinkler activation patterns as alert codes
- **Vehicle Integration**: Car horn honks, headlight flashes (if accessible)
- **Weather Simulation**: Fan activation for "windy" alerts, misting for rain alerts

## Configuration Management

### Dynamic Configuration Reloading
1. **File Watcher**: Monitor `alerts_config.json` for changes
2. **MQTT Configuration**: Publish config updates to `alerts/config`
3. **Web Interface**: Home Assistant dashboard for rule management
4. **Version Control**: Git tracking for configuration changes

### Configuration Validation
```python
# Example validation schema
config_schema = {
  "routing_rules": [
    {
      "condition": {"level": ["critical", "warning", "notice", "info", "debug"]},
      "pathways": ["led_sign", "ha_notification", "mobile_push", "log_file"]
    }
  ]
}
```

## Integration Examples

### Home Assistant Automation
```yaml
automation:
  - alias: "Process Alert Input"
    trigger:
      platform: mqtt
      topic: "alerts/input"
    action:
      - service: python_script.process_alert
        data:
          payload: "{{ trigger.payload_json }}"
```

### LED Sign Message Formatting
```python
def format_led_message(alert):
    color_map = {"critical": "red", "warning": "amber", "info": "green"}
    color = color_map.get(alert["level"], "green")
    return f"[{color}]{alert['title']}: {alert['message']}"
```

### Alert History Dashboard
- Recent alerts table
- Alert frequency by category/level
- System health indicators
- Configuration status

## Benefits

1. **Standardization**: Single message format across all alert sources
2. **Flexibility**: Easy routing rule modifications without code changes
3. **Scalability**: Add new pathways without system redesign
4. **Maintainability**: Centralized configuration and logging
5. **Extensibility**: Network discovery for future alert endpoints
6. **Integration**: Seamless Home Assistant ecosystem integration

## Implementation Phases

### Phase 1: Core System
- Basic MQTT routing
- LED sign and Home Assistant pathways
- JSON configuration file
- Manual configuration management

### Phase 2: Enhanced Features
- Dynamic configuration reloading
- Alert history and statistics
- Mobile push notifications
- File logging

### Phase 3: Advanced Capabilities
- mDNS service discovery
- Web-based configuration interface
- Additional pathways
- Advanced routing logic (time-based, location-based)

This architecture provides a solid foundation for unified alerting while maintaining simplicity and allowing for future growth.