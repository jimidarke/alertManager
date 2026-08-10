# Docker Deployment Guide

Microservices architecture with containerized pathway services and secure cloud MQTT broker.

## 🐳 Architecture

```
[Alert Sources] → alert.d-t.pw:42690 → [Core Router] → [Output MQTT Topics] → [Distributed Consumers]
                        ↓                   ↓                  ↓                         ↓
              Secure Cloud Broker    Parse/Classify/Route  alerts/output/*    Server Services + Device Controllers
```

### Hybrid Design: Server Services + Distributed Devices
```
┌─────────────────┐    ┌─────────────────┐
│  Core Router    │    │ File Logger     │
│   Container     │    │   Container     │
│                 │    │                 │
│ Routing Logic   │◄──►│ Centralized Log │
│ Email Pathways  │    │                 │
└─────────────────┘    └─────────────────┘
         ▲                       ▲
         │                       │
         └───────────────────────┘
                                 │
                  ┌─────────────────────────────┐
                  │   Secure Cloud MQTT Broker │
                  │      alert.d-t.pw:42690     │
                  │  Username/Password + TLS    │
                  └─────────────────────────────┘
                                 │
         ┌───────────────────────┼───────────────────────┐
         ▼                       ▼                       ▼
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│ LED Controller  │    │ Audio Controller│    │ ESP32 Device    │
│   Standalone    │    │   Standalone    │    │   Standalone    │
│                 │    │                 │    │                 │
│ Subscribes to   │    │ Subscribes to   │    │ Subscribes to   │
│ ledSign/{zone}/ │    │ alerts/output/  │    │ alerts/output/  │
│ message         │    │ audio/+/tts     │    │ device/+/cmd    │
└─────────────────┘    └─────────────────┘    └─────────────────┘
```

## 📁 Volume Mappings

### Core Router Service
| Host Path | Container Path | Description |
|-----------|----------------|-------------|
| `./data/config` | `/data/config` | Configuration files |
| `./data/logs` | `/data/logs` | Alert and service logs |
| `./data/certs` | `/data/certs` | MQTT client certificates |
| `./data/scripts` | `/data/scripts` | Custom scripts (optional) |

### Additional Service Volumes
| Host Path | Container Path | Description |
|-----------|----------------|-------------|
| `./data/postgres` | `/var/lib/postgresql/data` | PostgreSQL database (analytics) |
| `./sql/init` | `/docker-entrypoint-initdb.d` | Database initialization scripts |

## 🚀 Quick Start

### 1. Start Services
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f alert-router
```

### 2. Send Test Alerts
```bash
# Run test script
./docker-test.py

# Or manually with secure mosquitto_pub
mosquitto_pub \
  --cafile ./data/certs/ca.crt \
  --cert ./data/certs/client.crt \
  --key ./data/certs/client.key \
  -h alert.d-t.pw -p 42690 \
  -t "alerts/input" -m '{
    "timestamp": "'$(date -Iseconds)'",
    "level": "warning",
    "category": "test",
    "title": "Docker Test",
    "message": "Testing containerized services",
    "source": "docker_test"
  }'
```

### 3. View Results
```bash
# Service logs
docker logs alert-router

# Alert logs
cat ./data/logs/alerts.log

# MQTT broker logs
docker logs mqtt-broker
```

## 📋 Docker Services (Server-Side Only)

### Core Alert Router
- **Image**: `alert-router:latest`
- **Function**: Parse, classify, and route alerts via pathways (file, email, LED, console)
- **Config**: `./data/config/docker.json`
- **Network**: `WebApps` (shared Docker network)
- **Email Support**: Built-in email pathways (SMTP/O365 Graph API)

### File Logger Service
- **Image**: `alert-router:latest` (reuses same image)
- **Function**: Centralized logging from `alerts/input` topic
- **Volumes**: Exclusive write access to `/data/logs/file/`
- **Profile**: `logging`

### HTTP Gateway Service
- **Image**: `alert-router:latest` (reuses same image)
- **Function**: REST API and web-based log viewer
- **Port**: `8080` (host access)
- **Profile**: `http`

### PostgreSQL Database
- **Image**: `postgres:16-alpine`
- **Function**: Stores alerts for analytics and queries
- **Profile**: `database`

### Analytics Engine
- **Image**: `alert-router:latest` (reuses same image)
- **Function**: Scheduled log analysis and email reports
- **Profile**: `analytics`

## 🏠 Physical Device Controllers (Not Dockerized)

Physical devices connect directly to `alert.d-t.pw:42690` and subscribe to specific topics:

### LED Sign Controllers
- **Connection**: Direct MQTT client with certificates
- **Topics**: `alerts/output/led/{zone}/message`, `alerts/output/led/{zone}/config`
- **Hardware**: BetaBrite signs with ESP32/Pi controllers
- **Installation**: Deploy certificates + standalone Python/C++ client

### Audio Controllers
- **Connection**: Direct MQTT client with certificates
- **Topics**: `alerts/output/audio/{zone}/tts`, `alerts/output/audio/{zone}/chime`
- **Hardware**: Speakers with Pi/ESP32 + audio HAT
- **Installation**: Deploy certificates + audio processing software

### IoT Device Endpoints
- **Connection**: Direct MQTT client with certificates
- **Topics**: `alerts/output/device/{device_id}/command`
- **Hardware**: ESP32, Arduino, Raspberry Pi devices
- **Installation**: Flash firmware with embedded certificates

### Service Profiles

#### Production (Core Services)
```bash
# Start core alert router only
docker-compose up -d

# Start with file logging
docker-compose --profile logging up -d

# Start with HTTP gateway and logging
docker-compose --profile http,logging up -d
```

#### Full Stack
```bash
# Start all services (logging, HTTP, database, analytics)
docker-compose --profile logging,http,database,analytics up -d
```

#### Individual Service Profiles
```bash
# Core router only (includes built-in pathways like email)
docker-compose up -d

# Add file logging
docker-compose --profile logging up -d

# Add HTTP gateway for REST API and log viewer
docker-compose --profile http up -d

# Add SNMP gateway for SNMPv3 traps
docker-compose --profile snmp up -d
```

## 📱 Device Controller Examples

### ESP32 LED Sign Controller
```cpp
#include <WiFiClientSecure.h>
#include <PubSubClient.h>

// Certificate strings (from certificate bundle)
const char* ca_cert = "-----BEGIN CERTIFICATE-----\n...";
const char* client_cert = "-----BEGIN CERTIFICATE-----\n...";
const char* client_key = "-----BEGIN PRIVATE KEY-----\n...";

WiFiClientSecure secureClient;
PubSubClient mqttClient(secureClient);

void setup() {
    // Configure certificates
    secureClient.setCACert(ca_cert);
    secureClient.setCertificate(client_cert);
    secureClient.setPrivateKey(client_key);

    // Connect to MQTT broker
    mqttClient.setServer("alert.d-t.pw", 42690);
    mqttClient.setCallback(onMqttMessage);
    mqttClient.connect("led-sign-kitchen");

    // Subscribe to LED topics for this zone
    mqttClient.subscribe("alerts/output/led/kitchen/message");
    mqttClient.subscribe("alerts/output/led/kitchen/config");
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
    // Process LED display commands
    if (strstr(topic, "/message")) {
        displayMessage(payload, length);
    } else if (strstr(topic, "/config")) {
        updateDisplayConfig(payload, length);
    }
}
```

## ⚙️ Configuration

### Edit Configuration
```bash
# Edit main config
nano ./data/data/config/docker.json

# Restart to apply changes
docker-compose restart alert-router
```

### MQTT Broker Settings
```bash
# Edit MQTT config
nano ./data/mosquitto/config/mosquitto.conf

# Restart broker
docker-compose restart mqtt-broker
```

## 🔧 Development

### Build Image
```bash
# Build alert router image
docker-compose build alert-router

# Rebuild and restart
docker-compose up -d --build alert-router
```

### Debug Container
```bash
# Access running container
docker exec -it alert-router bash

# Run service manually
docker exec -it alert-router python3 alert_router.py --config /data/data/config/docker.json --test
```

### View Container Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f alert-router

# Since timestamp
docker-compose logs --since 10m alert-router
```

## 📊 Monitoring

### Health Checks
```bash
# Check container health
docker inspect alert-router | grep -A5 Health

# View health check logs
docker inspect alert-router | jq '.[].State.Health'
```

### Service Status
```bash
# Subscribe to status topic
docker exec mqtt-broker mosquitto_sub -t "alerts/service/status" -v

# Check if service is responding
docker exec alert-router python3 -c "
import os, json
status_file='/data/logs/status.json'
if os.path.exists(status_file):
    with open(status_file) as f:
        print(json.dumps(json.load(f), indent=2))
else:
    print('Status file not found')
"
```

## 🗂️ File Structure

```
alertManager/
├── docker-compose.yml          # Main compose file
├── Dockerfile                  # Alert router image
├── requirements.txt            # Python dependencies
├── alert_router.py            # Main service script
├── docker-test.py             # Docker test script
├── data/                      # Volume mappings
│   ├── config/
│   │   └── config.json        # Service configuration
│   ├── logs/                  # Log files
│   │   ├── alerts.log         # Alert messages
│   │   ├── alert-router.log   # Service logs
│   │   └── status.json        # Service status
│   ├── mosquitto/             # MQTT broker data
│   │   ├── config/
│   │   │   └── mosquitto.conf # MQTT configuration
│   │   ├── data/              # MQTT persistence
│   │   └── log/               # MQTT logs
│   └── scripts/               # Custom scripts
└── .dockerignore              # Docker ignore file
```

## 🛠️ Troubleshooting

### Service Won't Start
```bash
# Check container logs
docker-compose logs alert-router

# Verify configuration
docker run --rm -v $(pwd)/data/config:/data/config alert-router \
  python3 -c "import json; print(json.load(open('/data/data/config/docker.json')))"

# Check permissions
ls -la ./data/
```

### Cloud MQTT Connection Issues
```bash
# Test secure cloud MQTT connection
mosquitto_pub \
  --cafile ./data/certs/ca.crt \
  --cert ./data/certs/client.crt \
  --key ./data/certs/client.key \
  -h alert.d-t.pw -p 42690 \
  -t "test" -m "hello"

# Subscribe to test topic
mosquitto_sub \
  --cafile ./data/certs/ca.crt \
  --cert ./data/certs/client.crt \
  --key ./data/certs/client.key \
  -h alert.d-t.pw -p 42690 \
  -t "test" -C 1

# Check service MQTT connections
docker logs alert-router | grep -i mqtt
docker logs alert-file-logger | grep -i mqtt
```

### No Alerts Received
```bash
# Subscribe to input topic
docker exec mqtt-broker mosquitto_sub -t "alerts/input" -v

# Send test alert (timestamp must be Unix epoch integer)
docker exec mqtt-broker mosquitto_pub -t "alerts/input" -m '{"timestamp":1704110400,"level":"info","category":"system","title":"Test","message":"Test message","source":"debug"}'

# Check service is processing
docker logs alert-router | grep "Processing alert"
```

### Volume Issues
```bash
# Check volume mounts
docker inspect alert-router | grep -A20 Mounts

# Verify permissions
docker exec alert-router ls -la /data/

# Fix permissions if needed
sudo chown -R 1000:1000 ./data/
```

## 🚦 Production Considerations

### Security
- **Certificate Management**: Store certificates in shared volumes with read-only access
- **Network Isolation**: Use internal Docker networks for inter-service communication
- **Environment Variables**: Use Docker secrets or .env files for sensitive data
- **Minimal Privileges**: Run services as non-root users with minimal required permissions
- **Cloud MQTT**: All external MQTT connections use TLS 1.3 with certificate authentication

### Performance
- Monitor container resource usage
- Adjust restart policies as needed
- Consider log rotation for large deployments
- Use persistent volumes for important data

### Backup
```bash
# Backup configuration and logs
tar -czf alert-backup-$(date +%Y%m%d).tar.gz ./data/

# Restore from backup
tar -xzf alert-backup-YYYYMMDD.tar.gz
```

## 📝 Environment Variables

You can override configuration with environment variables in `docker-compose.yml`:

```yaml
environment:
  - MQTT_BROKER=mqtt-broker
  - MQTT_PORT=1883
  - LOG_LEVEL=INFO
  - TZ=America/New_York
```

## 🔗 Microservices Network Configuration

### Internal Communication
Services communicate via the `alert-network` Docker bridge network:

```yaml
networks:
  alert-network:
    driver: bridge
    internal: false  # Allow cloud MQTT access
```

### Service Discovery
- **DNS Resolution**: Services can reference each other by container name
- **Shared Volumes**: Certificates and logs shared via Docker volumes
- **Environment Variables**: Configuration passed via environment

### MQTT Topic Flow
```
Input: alerts/input (External sources → Cloud MQTT)
   ↓
Router: Parse & Route (Core service processes)
   ↓
Output: alerts/output/* (Published to cloud MQTT)
   ↓
Pathways: Service-specific subscriptions (Each pathway container)
```

### Port Mappings
- **No Host Ports**: Services communicate internally only
- **Cloud MQTT**: All external MQTT via alert.d-t.pw:42690
- **Development**: Dashboard on localhost:8080 (dev profile only)

## 🔄 Service Management

### Starting Services
```bash
# Start specific profile
docker-compose --profile core up -d
docker-compose --profile full up -d

# Start individual services
docker-compose up -d alert-router
docker-compose --profile logging up -d

# Check running services
docker-compose ps
```

### Updating Services
```bash
# Update images
docker-compose pull

# Rebuild local images
docker-compose build

# Restart with new images
docker-compose up -d --build

# Clean old images
docker image prune -f
```

### Service Logs
```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f alert-router
docker-compose logs -f alert-file-logger

# Follow new logs only
docker-compose logs -f --tail=0 alert-router

# Filter by time
docker-compose logs --since=1h alert-router
```

### Service Status
```bash
# Check all services
docker-compose ps

# Check specific service health
docker inspect alert-router | jq '.[].State.Health'

# View service stats
docker stats alert-router alert-file-logger alert-http-gateway
```

## 📊 Monitoring and Debugging

### MQTT Topic Monitoring
```bash
# Monitor all output topics
mosquitto_sub \
  --cafile ./data/certs/ca.crt \
  --cert ./data/certs/client.crt \
  --key ./data/certs/client.key \
  -h alert.d-t.pw -p 42690 \
  -t "alerts/output/+/+" -v

# Monitor LED sign messages
mosquitto_sub \
  --cafile ./data/certs/ca.crt \
  -u username -P password \
  -h alert.d-t.pw -p 42690 \
  -t "ledSign/+/message" -v
```

### Container Debugging
```bash
# Access running container
docker exec -it alert-router bash
docker exec -it alert-file-logger bash

# Check operational logs
docker exec alert-router cat /data/logs/ops/alert-router.$(date +%Y-%m-%d).log
docker exec alert-file-logger cat /data/logs/ops/alert-file-logger.$(date +%Y-%m-%d).log

# Test service manually
docker exec alert-router python3 src/alert_router.py --config /data/config/docker.json
```

### Performance Monitoring
```bash
# Resource usage
docker stats --format "table {{.Container}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.NetIO}}"

# Container processes
docker exec alert-router ps aux

# Network connections
docker exec alert-router netstat -tlnp
```