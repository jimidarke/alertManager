#!/bin/bash

# Send Test Alert
# Simulates test payloads to the alerts/input topic

set -e

# Configuration
BROKER="alert.d-t.pw"
PORT="42690"
TOPIC="alerts/input"
CLIENT_ID="test-sender-$(date +%s)"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m'

# Certificate paths (relative to tools directory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CERT_DIR="$SCRIPT_DIR/certs"
CA_CERT="$CERT_DIR/ca.crt"
CLIENT_CERT="$CERT_DIR/client.crt"
CLIENT_KEY="$CERT_DIR/client.key"

show_help() {
    echo -e "${BLUE}=== Test Alert Sender ===${NC}"
    echo ""
    echo "Usage: $0 [OPTIONS] [TEST_TYPE]"
    echo ""
    echo "Options:"
    echo "  -b, --broker HOST    MQTT broker hostname (default: alert.d-t.pw)"
    echo "  -p, --port PORT      MQTT broker port (default: 42690)"
    echo "  -c, --count NUM      Number of alerts to send (default: 1)"
    echo "  -i, --interval SEC   Interval between alerts in seconds (default: 1)"
    echo "  -h, --help           Show this help"
    echo ""
    echo "Test Types:"
    echo "  critical             Send critical security alert"
    echo "  warning              Send warning system alert"
    echo "  info                 Send info status alert"
    echo "  debug                Send debug message"
    echo "  random               Send random alerts"
    echo "  sequence             Send all alert levels in sequence"
    echo "  custom               Send custom message (interactive)"
    echo ""
    echo "Examples:"
    echo "  $0 critical          # Send single critical alert"
    echo "  $0 random -c 5       # Send 5 random alerts"
    echo "  $0 sequence -i 2     # Send sequence with 2s intervals"
    echo "  $0 -b localhost -p 1883 info  # Local broker"
}

# Default settings
USE_TLS=true
COUNT=1
INTERVAL=1
TEST_TYPE="info"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -b|--broker)
            BROKER="$2"
            shift 2
            ;;
        -p|--port)
            PORT="$2"
            shift 2
            ;;
        -c|--count)
            COUNT="$2"
            shift 2
            ;;
        -i|--interval)
            INTERVAL="$2"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        critical|warning|info|debug|random|sequence|custom)
            TEST_TYPE="$1"
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

# Check for local/internal broker
if [[ "$BROKER" == "localhost" || "$BROKER" == "mqtt-broker" || "$PORT" == "1883" ]]; then
    USE_TLS=false
fi

echo -e "${BLUE}=== Test Alert Sender ===${NC}"
echo -e "${CYAN}Broker: $BROKER:$PORT${NC}"
echo -e "${CYAN}Topic: $TOPIC${NC}"
echo -e "${CYAN}Test Type: $TEST_TYPE${NC}"
echo -e "${CYAN}Count: $COUNT${NC}"
echo -e "${CYAN}TLS: $([ "$USE_TLS" = true ] && echo "Enabled" || echo "Disabled")${NC}"
echo ""

# Build mosquitto_pub command base
MOSQUITTO_CMD="mosquitto_pub"

if [[ "$USE_TLS" == "true" ]]; then
    # Check certificates for TLS connection
    if [[ ! -f "$CA_CERT" ]] || [[ ! -f "$CLIENT_CERT" ]] || [[ ! -f "$CLIENT_KEY" ]]; then
        echo -e "${RED}Error: Certificate files missing${NC}"
        echo "Required files:"
        echo "  CA Cert: $CA_CERT"
        echo "  Client Cert: $CLIENT_CERT"
        echo "  Client Key: $CLIENT_KEY"
        exit 1
    fi

    MOSQUITTO_CMD="$MOSQUITTO_CMD --cafile $CA_CERT --cert $CLIENT_CERT --key $CLIENT_KEY"
    echo -e "${GREEN}✓ Certificates found${NC}"
else
    echo -e "${YELLOW}⚠ Using insecure connection (no TLS)${NC}"
fi

# Add connection parameters
MOSQUITTO_CMD="$MOSQUITTO_CMD -h $BROKER -p $PORT -t $TOPIC -i $CLIENT_ID"

# Check if mosquitto_pub is available
if ! command -v mosquitto_pub &> /dev/null; then
    echo -e "${RED}Error: mosquitto_pub not found${NC}"
    echo "Install with: apt install mosquitto-clients"
    exit 1
fi

# Generate test alert payload
generate_alert() {
    local level="$1"
    local category="$2"
    local title="$3"
    local message="$4"
    local source_id="${5:-test-script}"

    local timestamp=$(date +%s)

    cat << EOF
{
  "timestamp": $timestamp,
  "level": "$level",
  "category": "$category",
  "title": "$title",
  "message": "$message",
  "source": {
    "id": "$source_id",
    "type": "mqtt-test",
    "metadata": {
      "test_run": true,
      "generated_by": "send-mqtt-alert.sh"
    }
  },
  "metadata": {
    "test_id": "$(date +%s)-$$"
  }
}
EOF
}

# Generate custom alert interactively
generate_custom_alert() {
    echo -e "${CYAN}Enter custom alert details:${NC}"

    read -p "Level (critical/warning/info/debug): " level
    read -p "Category (security/system/network/etc): " category
    read -p "Title: " title
    read -p "Message: " message
    read -p "Source (default: custom-test): " source

    source=${source:-custom-test}

    generate_alert "$level" "$category" "$title" "$message" "$source"
}

# Send single alert
send_alert() {
    local payload="$1"
    local description="$2"

    echo -e "${YELLOW}Sending: $description${NC}"

    if echo "$payload" | $MOSQUITTO_CMD -l; then
        echo -e "${GREEN}✓ Alert sent successfully${NC}"
        if command -v jq &> /dev/null; then
            echo -e "${PURPLE}Payload preview:${NC}"
            echo "$payload" | jq -C '.' | head -5 | sed 's/^/  /'
        fi
    else
        echo -e "${RED}✗ Failed to send alert${NC}"
        return 1
    fi
    echo ""
}

# Main sending logic
echo -e "${GREEN}Starting alert transmission...${NC}"
echo ""

for ((i=1; i<=COUNT; i++)); do
    case "$TEST_TYPE" in
        critical)
            payload=$(generate_alert "critical" "security" "Security Breach Detected" "Unauthorized access attempt from unknown IP address" "security-system")
            send_alert "$payload" "Critical security alert ($i/$COUNT)"
            ;;
        warning)
            payload=$(generate_alert "warning" "system" "High CPU Usage" "System CPU usage has exceeded 85% for the last 5 minutes" "monitoring-system")
            send_alert "$payload" "Warning system alert ($i/$COUNT)"
            ;;
        info)
            payload=$(generate_alert "info" "system" "Service Started" "Alert Manager service has started successfully" "alert-manager")
            send_alert "$payload" "Info status alert ($i/$COUNT)"
            ;;
        debug)
            payload=$(generate_alert "debug" "application" "Debug Message" "Processing queue size: 42 items pending" "debug-logger")
            send_alert "$payload" "Debug message ($i/$COUNT)"
            ;;
        random)
            # Random alert generator
            levels=("critical" "warning" "info" "debug")
            categories=("security" "system" "network" "application" "automation")

            level=${levels[$RANDOM % ${#levels[@]}]}
            category=${categories[$RANDOM % ${#categories[@]}]}

            case "$level" in
                critical)
                    titles=("System Failure" "Security Breach" "Network Down" "Database Error")
                    messages=("Critical system component has failed" "Unauthorized access detected" "Network connectivity lost" "Database connection timeout")
                    ;;
                warning)
                    titles=("High Load" "Disk Space Low" "Performance Degraded" "Service Slow")
                    messages=("System load is above normal" "Disk usage is at 90%" "Response time increased" "Service responding slowly")
                    ;;
                info)
                    titles=("Status Update" "Task Completed" "Service Ready" "Backup Complete")
                    messages=("System status is normal" "Scheduled task completed successfully" "All services are operational" "Backup completed successfully")
                    ;;
                debug)
                    titles=("Debug Info" "Process Status" "Queue Update" "Memory Stats")
                    messages=("Debug information logged" "Process is running normally" "Queue has 10 items" "Memory usage: 45%")
                    ;;
            esac

            title_idx=$((RANDOM % ${#titles[@]}))
            title=${titles[$title_idx]}
            message=${messages[$title_idx]}

            payload=$(generate_alert "$level" "$category" "$title" "$message" "random-generator")
            send_alert "$payload" "Random $level alert ($i/$COUNT)"
            ;;
        sequence)
            # Send sequence of all levels
            case $(( (i-1) % 4 )) in
                0)
                    payload=$(generate_alert "debug" "application" "Sequence Test - Debug" "Debug level message in sequence" "sequence-test")
                    send_alert "$payload" "Sequence: Debug ($i/$COUNT)"
                    ;;
                1)
                    payload=$(generate_alert "info" "system" "Sequence Test - Info" "Info level message in sequence" "sequence-test")
                    send_alert "$payload" "Sequence: Info ($i/$COUNT)"
                    ;;
                2)
                    payload=$(generate_alert "warning" "network" "Sequence Test - Warning" "Warning level message in sequence" "sequence-test")
                    send_alert "$payload" "Sequence: Warning ($i/$COUNT)"
                    ;;
                3)
                    payload=$(generate_alert "critical" "security" "Sequence Test - Critical" "Critical level message in sequence" "sequence-test")
                    send_alert "$payload" "Sequence: Critical ($i/$COUNT)"
                    ;;
            esac
            ;;
        custom)
            payload=$(generate_custom_alert)
            send_alert "$payload" "Custom alert ($i/$COUNT)"
            ;;
        *)
            echo -e "${RED}Unknown test type: $TEST_TYPE${NC}"
            exit 1
            ;;
    esac

    # Wait between messages if sending multiple
    if [[ $i -lt $COUNT && $INTERVAL -gt 0 ]]; then
        echo -e "${BLUE}Waiting ${INTERVAL}s before next alert...${NC}"
        sleep "$INTERVAL"
    fi
done

echo -e "${GREEN}✅ Completed sending $COUNT alert(s)${NC}"