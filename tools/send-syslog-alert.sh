#!/bin/bash

# Send Syslog Alert via UDP
# Tests the syslog gateway endpoint with RFC3164/RFC5424 syslog messages

set -e

# Configuration
SYSLOG_HOST="alert.d-t.pw"
SYSLOG_PORT="51514"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

show_help() {
    echo -e "${BLUE}=== Syslog Alert Sender ===${NC}"
    echo ""
    echo "Usage: $0 [OPTIONS] [ALERT_TYPE]"
    echo ""
    echo "Options:"
    echo "  -h, --host HOST      Syslog gateway host (default: alert.d-t.pw)"
    echo "  -p, --port PORT      Syslog gateway port (default: 51514)"
    echo "  -c, --count NUM      Number of alerts to send (default: 1)"
    echo "  -i, --interval SEC   Interval between alerts in seconds (default: 1)"
    echo "  -f, --format FORMAT  Syslog format: rfc3164 or rfc5424 (default: rfc3164)"
    echo "  --help               Show this help"
    echo ""
    echo "Alert Types:"
    echo "  critical             Send critical security alert (priority 130)"
    echo "  error                Send error system alert (priority 131)"
    echo "  warning              Send warning system alert (priority 132)"
    echo "  notice               Send notice operational alert (priority 133)"
    echo "  info                 Send info status alert (default, priority 134)"
    echo "  debug                Send debug message (priority 135)"
    echo "  test                 Send test alert (priority 134)"
    echo ""
    echo "Examples:"
    echo "  $0                   # Send single info alert"
    echo "  $0 critical          # Send critical alert"
    echo "  $0 -c 3 warning      # Send 3 warning alerts"
    echo "  $0 -f rfc5424 info   # Send RFC5424 format info alert"
}

# Default settings
COUNT=1
INTERVAL=1
ALERT_TYPE="info"
SYSLOG_FORMAT="rfc3164"

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--host)
            SYSLOG_HOST="$2"
            shift 2
            ;;
        -p|--port)
            SYSLOG_PORT="$2"
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
        -f|--format)
            SYSLOG_FORMAT="$2"
            shift 2
            ;;
        --help)
            show_help
            exit 0
            ;;
        critical|error|warning|notice|info|debug|test)
            ALERT_TYPE="$1"
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            show_help
            exit 1
            ;;
    esac
done

echo -e "${BLUE}=== Syslog Alert Sender ===${NC}"
echo -e "${CYAN}Gateway: ${SYSLOG_HOST}:${SYSLOG_PORT}/udp${NC}"
echo -e "${CYAN}Format: ${SYSLOG_FORMAT}${NC}"
echo -e "${CYAN}Alert Type: ${ALERT_TYPE}${NC}"
echo -e "${CYAN}Count: ${COUNT}${NC}"
echo ""

# Check if nc (netcat) is available
if ! command -v nc &> /dev/null; then
    echo -e "${RED}Error: nc (netcat) not found${NC}"
    echo -e "${YELLOW}Install with: apt-get install netcat or brew install netcat${NC}"
    exit 1
fi

# Get hostname
HOSTNAME=$(hostname -s 2>/dev/null || echo "test-host")

# Generate RFC3164 syslog message
generate_rfc3164() {
    local priority="$1"
    local tag="$2"
    local message="$3"
    local timestamp=$(date '+%b %d %H:%M:%S')

    echo "<${priority}>${timestamp} ${HOSTNAME} ${tag}: ${message}"
}

# Generate RFC5424 syslog message
generate_rfc5424() {
    local priority="$1"
    local app_name="$2"
    local message="$3"
    local timestamp=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
    local proc_id="$$"
    local msg_id="TEST-$(date +%s)"

    echo "<${priority}>1 ${timestamp} ${HOSTNAME} ${app_name} ${proc_id} ${msg_id} - ${message}"
}

# Send syslog message via UDP
send_syslog() {
    local message="$1"
    local description="$2"

    echo -e "${YELLOW}Sending: ${description}${NC}"
    echo -e "${CYAN}Message: ${message}${NC}"

    # Send via netcat UDP
    echo "$message" | nc -u -w1 "${SYSLOG_HOST}" "${SYSLOG_PORT}"

    if [[ $? -eq 0 ]]; then
        echo -e "${GREEN}✓ Syslog message sent successfully${NC}"
    else
        echo -e "${RED}✗ Failed to send syslog message${NC}"
        return 1
    fi
    echo ""
}

echo -e "${GREEN}Starting syslog message transmission...${NC}"
echo ""

for ((i=1; i<=COUNT; i++)); do
    case "$ALERT_TYPE" in
        critical)
            # Priority 130 = facility 16 (local0), severity 2 (critical)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "130" "security-monitor" "CRITICAL: Unauthorized access detected - immediate attention required")
            else
                message=$(generate_rfc3164 "130" "security-monitor" "CRITICAL: Unauthorized access detected - immediate attention required")
            fi
            send_syslog "$message" "Critical security alert ($i/$COUNT)"
            ;;
        error)
            # Priority 131 = facility 16 (local0), severity 3 (error)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "131" "app-service" "ERROR: Database connection failed - retrying")
            else
                message=$(generate_rfc3164 "131" "app-service" "ERROR: Database connection failed - retrying")
            fi
            send_syslog "$message" "Error system alert ($i/$COUNT)"
            ;;
        warning)
            # Priority 132 = facility 16 (local0), severity 4 (warning)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "132" "system-monitor" "WARNING: System resource usage above threshold")
            else
                message=$(generate_rfc3164 "132" "system-monitor" "WARNING: System resource usage above threshold")
            fi
            send_syslog "$message" "Warning system alert ($i/$COUNT)"
            ;;
        notice)
            # Priority 133 = facility 16 (local0), severity 5 (notice)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "133" "backup-service" "NOTICE: Daily backup completed successfully")
            else
                message=$(generate_rfc3164 "133" "backup-service" "NOTICE: Daily backup completed successfully")
            fi
            send_syslog "$message" "Notice operational alert ($i/$COUNT)"
            ;;
        info)
            # Priority 134 = facility 16 (local0), severity 6 (informational)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "134" "app-monitor" "INFO: Application status update - all systems operational")
            else
                message=$(generate_rfc3164 "134" "app-monitor" "INFO: Application status update - all systems operational")
            fi
            send_syslog "$message" "Info status alert ($i/$COUNT)"
            ;;
        debug)
            # Priority 135 = facility 16 (local0), severity 7 (debug)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "135" "debug-logger" "DEBUG: Test message for debugging purposes")
            else
                message=$(generate_rfc3164 "135" "debug-logger" "DEBUG: Test message for debugging purposes")
            fi
            send_syslog "$message" "Debug message ($i/$COUNT)"
            ;;
        test)
            # Priority 134 = facility 16 (local0), severity 6 (informational)
            if [[ "$SYSLOG_FORMAT" == "rfc5424" ]]; then
                message=$(generate_rfc5424 "134" "syslog-test" "TEST: Syslog gateway test message - timestamp $(date +%s)")
            else
                message=$(generate_rfc3164 "134" "syslog-test" "TEST: Syslog gateway test message - timestamp $(date +%s)")
            fi
            send_syslog "$message" "Test message ($i/$COUNT)"
            ;;
        *)
            echo -e "${RED}Unknown alert type: ${ALERT_TYPE}${NC}"
            exit 1
            ;;
    esac

    # Wait between alerts if sending multiple
    if [[ $i -lt $COUNT && $INTERVAL -gt 0 ]]; then
        echo -e "${BLUE}Waiting ${INTERVAL}s before next alert...${NC}"
        sleep "$INTERVAL"
    fi
done

echo -e "${GREEN}✅ Completed sending $COUNT syslog message(s)${NC}"
echo ""
echo -e "${CYAN}Tip: Monitor the alert router logs to see the processed alerts:${NC}"
echo -e "${CYAN}  docker logs -f alert-router${NC}"
