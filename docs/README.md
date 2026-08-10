# Alert Router Service

A simple, lightweight alerting system that subscribes to MQTT alerts and routes them to appropriate pathways based on configurable rules.

## Features 

- **MQTT-based**: Subscribes to `alerts/input` topic for incoming alerts
- **Flexible Routing**: Route alerts based on level, category, and source
- **Multiple Outputs**: Console, file logging, and MQTT publishing (e.g., LED signs)
- **Service Integration**: Runs as a systemd service for reliability
- **Routing Overrides**: Payload-level routing control
- **Status Monitoring**: Publishes service status and statistics to MQTT

## Architecture

```
[Alert Sources] → alerts/input → [Alert Router] → [Pathways]
                      ↑                ↓
              External MQTT       alerts/service/status
           (192.168.1.40:1883)
```

### Alert Message Format

```json
{
  "timestamp": 1705315800,
  "level": "warning",
  "category": "security",
  "title": "Motion Detected",
  "message": "Front door motion sensor triggered",
  "source": "security_system",
  "metadata": {
    "location": "front_door",
    "device_id": "motion_01"
  },
  "routing": {
    "override": true,
    "pathways": ["led_sign", "console", "file"]
  }
}
```

### Alert Levels
- `critical`: Immediate attention required
- `warning`: Important but not urgent  
- `info`: General information
- `debug`: Troubleshooting information

### Categories
- `security`: Security events
- `weather`: Weather alerts
- `system`: System status/health
- `automation`: Home automation events
- `personal`: Personal reminders/notifications

## Installation

### Prerequisites
- Python 3.6+
- Access to existing MQTT broker at 192.168.1.40:1883

### Quick Start (Development)

```bash
# Install dependencies
pip3 install paho-mqtt

# Test locally
python3 alert_router.py --config test_config.json --test

# Send test alerts
./test_alerts.py
```

### Docker Deployment (Recommended)

```bash
# Start the service
docker-compose up -d

# Test with Docker
./docker-test.py

# View logs
docker logs alert-router
cat ./data/logs/alerts.log
```

### Manual System Installation

```bash
# Run installation script as root
sudo ./install.sh
```

This will:
- Install Python dependencies (`paho-mqtt`)
- Create system user `alert-router`
- Copy files to `/opt/alert-router`
- Create log directories
- Install systemd service

**Note**: Update `/opt/alert-router/config.json` to point to your MQTT broker (192.168.1.40) after installation.

## Usage

### Start the Service

```bash
sudo systemctl start alert-router
```

### Check Status

```bash
sudo systemctl status alert-router
```

### View Logs

```bash
# Service logs
sudo journalctl -u alert-router -f

# Alert logs
tail -f /var/log/alerts/alerts.log
```

### Configuration

Edit `/opt/alert-router/config.json` to customize:

- MQTT broker settings
- Output channel configuration  
- Routing rules
- Logging settings

Restart the service after configuration changes:
```bash
sudo systemctl restart alert-router
```

### Testing

#### Test Mode (Offline)
```bash
python3 alert_router.py --test
```

#### Send Test Alerts (Requires MQTT)
```bash
./test_alerts.py
```

#### Manual Alert Sending
```bash
# Using mosquitto_pub
mosquitto_pub -h 192.168.1.40 -t "alerts/input" -m '{
  "timestamp": "'$(date -Iseconds)'",
  "level": "warning",
  "category": "system",
  "title": "Test Alert",
  "message": "This is a test message",
  "source": "manual_test"
}'
```

## Pathways

### Console Output
Prints alerts to stdout with timestamp and formatting.

### File Logging  
Writes alerts to `/var/log/alerts/alerts.log` with detailed formatting including category and source.

### MQTT Output (LED Sign)
Publishes formatted alerts to `ledSign/main/message` topic for display devices.

## Routing Rules

Routing rules are processed in order. The first matching rule determines pathways.

### Example Rules

```json
{
  "name": "Critical Security Alerts",
  "conditions": {
    "level": ["critical"],
    "category": ["security"]
  },
  "pathways": ["led_sign", "console", "file"]
}
```

### Routing Override

Individual alerts can override routing rules:

```json
{
  "routing": {
    "override": true,
    "pathways": ["console", "file"]
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

## Extending

### Adding Pathways

1. Add pathway configuration to `config.json`
2. Implement handler in `send_pathway()` method
3. Add to routing rules

### Custom Alert Sources

Any system can send alerts by publishing JSON to `alerts/input`:

```python
import json
import paho.mqtt.client as mqtt
from datetime import datetime

def send_alert(level, category, title, message, source):
    client = mqtt.Client()
    client.connect("192.168.1.40", 1883)
    
    alert = {
        "timestamp": datetime.now().isoformat(),
        "level": level,
        "category": category, 
        "title": title,
        "message": message,
        "source": source
    }
    
    client.publish("alerts/input", json.dumps(alert))
    client.disconnect()

# Usage
send_alert("warning", "system", "High CPU", "CPU at 85%", "monitor_script")
```

## Troubleshooting

### Service Won't Start
- Check MQTT broker at 192.168.1.40 is running and accessible
- Verify configuration file syntax
- Check permissions on log directories
- View logs: `journalctl -u alert-router` or `docker logs alert-router`

### No Alerts Received
- Verify MQTT broker connection to 192.168.1.40:1883
- Check topic name (`alerts/input`)
- Validate JSON message format
- Test with `mosquitto_sub -h 192.168.1.40 -t "alerts/input"`

### Missing Dependencies
```bash
pip3 install paho-mqtt
```

## Files

- `alert_router.py` - Main service script
- `config.json` - Configuration file
- `alert-router.service` - Systemd service definition
- `install.sh` - Installation script
- `test_alerts.py` - Test script for sending sample alerts
- `README.md` - This documentation

## License

This project is provided as-is for educational and personal use.