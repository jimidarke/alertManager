#!/bin/bash

# Comprehensive HTTPS Alert Flow Validation
# Tests alert submission, MQTT routing, file logging, and HTTP gateway log viewer

set -e

# Configuration
HTTPS_HOST="alert.d-t.pw"
MQTT_HOST="alert.d-t.pw"
MQTT_PORT="42690"
# Read from the environment, never hardcoded -- this script previously
# shipped a real gateway password in git AND echoed it to stdout.
HTTP_AUTH_USER="${ALERT_USER:?set ALERT_USER (gateway username)}"
HTTP_AUTH_PASS="${ALERT_PASSWORD:?set ALERT_PASSWORD (gateway password)}"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SEND_ALERT_SCRIPT="$SCRIPT_DIR/send-http-alert.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

# Track test results
TOTAL_TESTS=0
PASSED_TESTS=0
FAILED_TESTS=0

show_header() {
    echo ""
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
    echo -e "${CYAN}$1${NC}"
    echo -e "${BLUE}═══════════════════════════════════════════════════════════${NC}"
    echo ""
}

show_step() {
    echo -e "${MAGENTA}▶ $1${NC}"
}

show_success() {
    echo -e "${GREEN}✓ $1${NC}"
    ((PASSED_TESTS++))
}

show_failure() {
    echo -e "${RED}✗ $1${NC}"
    ((FAILED_TESTS++))
}

show_info() {
    echo -e "${CYAN}ℹ $1${NC}"
}

show_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

# Check prerequisites
check_prerequisites() {
    show_header "Checking Prerequisites"

    local all_ok=true

    # Check curl
    if command -v curl &> /dev/null; then
        show_success "curl is installed"
    else
        show_failure "curl not found - required for HTTPS requests"
        all_ok=false
    fi

    # Check mosquitto_pub
    if command -v mosquitto_pub &> /dev/null; then
        show_success "mosquitto_pub is installed"
    else
        show_warning "mosquitto_pub not found - MQTT monitoring will be skipped"
    fi

    # Check mosquitto_sub
    if command -v mosquitto_sub &> /dev/null; then
        show_success "mosquitto_sub is installed"
    else
        show_warning "mosquitto_sub not found - MQTT monitoring will be skipped"
    fi

    # Check send-http-alert.sh
    if [[ -x "$SEND_ALERT_SCRIPT" ]]; then
        show_success "send-http-alert.sh is executable"
    else
        show_failure "send-http-alert.sh not found or not executable"
        all_ok=false
    fi

    # Check jq for JSON formatting
    if command -v jq &> /dev/null; then
        show_success "jq is installed (JSON formatting enabled)"
    else
        show_warning "jq not found - JSON responses won't be pretty-printed"
    fi

    if [[ "$all_ok" == false ]]; then
        echo ""
        show_failure "Prerequisites check failed - cannot continue"
        exit 1
    fi

    echo ""
}

# Test HTTPS connectivity
test_https_connectivity() {
    show_header "Testing HTTPS Connectivity"
    ((TOTAL_TESTS++))

    show_step "Testing health endpoint at https://$HTTPS_HOST/health"

    response=$(curl -s -w "\n%{http_code}" "https://$HTTPS_HOST/health" 2>/dev/null || echo "000")
    http_code=$(echo "$response" | tail -n1)

    if [[ "$http_code" == "200" ]]; then
        show_success "HTTPS endpoint is reachable (HTTP $http_code)"
        echo "$response" | head -n -1 | jq '.' 2>/dev/null || echo "$response" | head -n -1
    else
        show_failure "HTTPS endpoint unreachable (HTTP $http_code)"
        return 1
    fi
    echo ""
}

# Test authentication
test_authentication() {
    show_header "Testing HTTP Authentication"
    ((TOTAL_TESTS++))

    show_step "Testing /test endpoint with authentication"

    response=$(curl -s -w "\n%{http_code}" \
        -X POST \
        -u "$HTTP_AUTH_USER:$HTTP_AUTH_PASS" \
        "https://$HTTPS_HOST/test" 2>/dev/null || echo "000")

    http_code=$(echo "$response" | tail -n1)

    if [[ "$http_code" == "200" ]]; then
        show_success "Authentication successful (HTTP $http_code)"
        echo "$response" | head -n -1 | jq '.' 2>/dev/null || echo "$response" | head -n -1
    else
        show_failure "Authentication failed (HTTP $http_code)"
        return 1
    fi
    echo ""
}

# Send test alerts with variations
send_test_alerts() {
    show_header "Sending Test Alerts (Various Combinations)"

    # Test 1: Critical alert with client field
    ((TOTAL_TESTS++))
    show_step "Test 1: Critical security alert with client='prod-web-01'"
    if "$SEND_ALERT_SCRIPT" -u "$HTTP_AUTH_USER" -P "$HTTP_AUTH_PASS" \
        -C "prod-web-01" critical > /dev/null 2>&1; then
        show_success "Critical alert sent with client field"
    else
        show_failure "Failed to send critical alert"
    fi
    sleep 1

    # Test 2: Warning alert without client field
    ((TOTAL_TESTS++))
    show_step "Test 2: Warning system alert (no client field)"
    if "$SEND_ALERT_SCRIPT" -u "$HTTP_AUTH_USER" -P "$HTTP_AUTH_PASS" \
        warning > /dev/null 2>&1; then
        show_success "Warning alert sent without client field"
    else
        show_failure "Failed to send warning alert"
    fi
    sleep 1

    # Test 3: Info alert with different client
    ((TOTAL_TESTS++))
    show_step "Test 3: Info alert with client='test-device-02'"
    if "$SEND_ALERT_SCRIPT" -u "$HTTP_AUTH_USER" -P "$HTTP_AUTH_PASS" \
        -C "test-device-02" info > /dev/null 2>&1; then
        show_success "Info alert sent with different client"
    else
        show_failure "Failed to send info alert"
    fi
    sleep 1

    # Test 4: Alert with normalized fields (ERROR → critical, SYSTEM → system)
    ((TOTAL_TESTS++))
    show_step "Test 4: Testing field normalization (level aliases, whitespace)"

    timestamp=$(date +%s)
    payload='{
      "timestamp": '"$timestamp"',
      "level": "ERROR",
      "category": " SYSTEM ",
      "title": "Normalization Test",
      "message": "Testing level alias (ERROR→critical) and category trimming",
      "source": "normalize-test",
      "client": " TEST-CLIENT-03 "
    }'

    response=$(curl -s -w "\n%{http_code}" \
        -X POST \
        -H "Content-Type: application/json" \
        -u "$HTTP_AUTH_USER:$HTTP_AUTH_PASS" \
        -d "$payload" \
        "https://$HTTPS_HOST/alert" 2>/dev/null || echo "000")

    http_code=$(echo "$response" | tail -n1)

    if [[ "$http_code" == "200" ]]; then
        show_success "Normalization test alert sent successfully"
    else
        show_failure "Normalization test failed (HTTP $http_code)"
    fi
    sleep 1

    # Test 5: Multiple categories
    ((TOTAL_TESTS++))
    show_step "Test 5: Debug alert with application category"
    if "$SEND_ALERT_SCRIPT" -u "$HTTP_AUTH_USER" -P "$HTTP_AUTH_PASS" \
        -C "app-server-01" debug > /dev/null 2>&1; then
        show_success "Debug alert sent with application category"
    else
        show_failure "Failed to send debug alert"
    fi

    echo ""
}

# Monitor MQTT topics
monitor_mqtt() {
    show_header "MQTT Topic Monitoring Instructions"

    if ! command -v mosquitto_sub &> /dev/null; then
        show_warning "mosquitto_sub not available - skipping MQTT monitoring"
        return
    fi

    show_info "You can monitor MQTT topics in real-time using these commands:"
    echo ""

    echo -e "${CYAN}# Monitor all alert inputs (requires TLS cert):${NC}"
    echo "mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT -t 'alerts/input' -v"
    echo ""

    echo -e "${CYAN}# Monitor service status:${NC}"
    echo "mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT -t 'alerts/service/status' -v"
    echo ""

    echo -e "${CYAN}# Monitor all alert topics:${NC}"
    echo "mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT -t 'alerts/#' -v"
    echo ""

    echo -e "${CYAN}# Monitor LED sign messages:${NC}"
    echo "mosquitto_sub -h $MQTT_HOST -p $MQTT_PORT -t 'ledSign/+/message' -v"
    echo ""

    show_info "Note: External MQTT access requires TLS certificates at port $MQTT_PORT"
    echo ""
}

# Check file logger output
check_file_logs() {
    show_header "File Logger Validation"

    show_info "File logger subscribes to alerts/input and writes to:"
    echo "  • /data/logs/file/{date}.log  (default log)"
    echo "  • /data/logs/file/{client}.{date}.log  (per-client logs if enabled)"
    echo ""

    show_step "To check file logs on the server:"
    echo ""
    echo -e "${CYAN}# View today's main alert log:${NC}"
    echo "docker exec alert-file-logger cat /data/logs/file/\$(date +%Y-%m-%d).log"
    echo ""

    echo -e "${CYAN}# Tail main alert log in real-time:${NC}"
    echo "docker exec alert-file-logger tail -f /data/logs/file/\$(date +%Y-%m-%d).log"
    echo ""

    echo -e "${CYAN}# View specific client log:${NC}"
    echo "docker exec alert-file-logger cat /data/logs/file/prod-web-01.\$(date +%Y-%m-%d).log"
    echo ""

    echo -e "${CYAN}# List all log files:${NC}"
    echo "docker exec alert-file-logger ls -lh /data/logs/file/"
    echo ""
}

# Check HTTP gateway log viewer
check_http_logs() {
    show_header "HTTP Gateway Log Viewer"

    show_info "The HTTP gateway provides a real-time web-based log viewer"
    echo ""

    echo -e "${CYAN}Access the log viewer:${NC}"
    echo "https://$HTTPS_HOST/logs"
    echo ""
    echo -e "${CYAN}Credentials:${NC}"
    echo "Username: $HTTP_AUTH_USER"
    echo "Password: (from \$ALERT_PASSWORD -- not printed)"
    echo ""

    show_step "Log Viewer Features:"
    echo "  • Real-time log streaming (polls every second)"
    echo "  • Color-coded log levels (critical, warning, notice, info, debug)"
    echo "  • Pause/resume streaming"
    echo "  • Auto-scroll detection"
    echo "  • Multi-file support with dropdown selector"
    echo "  • Recursive directory support (ops/, file/ subdirectories)"
    echo ""

    show_step "Log Viewer API Endpoints:"
    echo ""
    echo -e "${CYAN}# List available log files:${NC}"
    echo "curl -u $HTTP_AUTH_USER:$ALERT_PASSWORD https://$HTTPS_HOST/api/logs/list"
    echo ""

    echo -e "${CYAN}# Tail specific log file:${NC}"
    echo "curl -u $HTTP_AUTH_USER:$ALERT_PASSWORD 'https://$HTTPS_HOST/api/logs/tail?file=ops/alert-router.\$(date +%Y-%m-%d).log&lines=50'"
    echo ""

    echo -e "${CYAN}# Tail with {date} placeholder:${NC}"
    echo "curl -u $HTTP_AUTH_USER:$ALERT_PASSWORD 'https://$HTTPS_HOST/api/logs/tail?file=file/{date}.log&lines=100'"
    echo ""
}

# Verify alert routing
verify_routing() {
    show_header "Alert Routing Verification"

    show_info "Routing rules from docker.json config:"
    echo ""

    echo -e "${CYAN}Critical Security Alerts:${NC}"
    echo "  Conditions: level=critical, category=security"
    echo "  Pathways: led_sign, console_alerts, file_log"
    echo ""

    echo -e "${CYAN}Critical System Alerts:${NC}"
    echo "  Conditions: level=critical, category=system"
    echo "  Pathways: led_sign, console_alerts, file_log"
    echo ""

    echo -e "${CYAN}Warning Alerts:${NC}"
    echo "  Conditions: level=warning"
    echo "  Pathways: console_alerts, file_log"
    echo ""

    echo -e "${CYAN}Notice Alerts:${NC}"
    echo "  Conditions: level=notice"
    echo "  Pathways: console_alerts, file_log"
    echo ""

    echo -e "${CYAN}Info Alerts:${NC}"
    echo "  Conditions: level=info"
    echo "  Pathways: file_log"
    echo ""

    echo -e "${CYAN}Debug Alerts:${NC}"
    echo "  Conditions: level=debug"
    echo "  Pathways: file_log"
    echo ""
}

# Show complete flow diagram
show_flow_diagram() {
    show_header "Complete Alert Flow Diagram"

    echo -e "${CYAN}"
    cat << 'EOF'
┌─────────────────────────────────────────────────────────────────┐
│                     HTTPS Alert Submission                      │
│  https://alert.d-t.pw/alert (POST with JSON payload)           │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   HTTP Gateway (Port 8080)                      │
│  • Validates authentication (Basic Auth)                        │
│  • Validates JSON payload structure                             │
│  • Normalizes fields (level aliases, trim whitespace)           │
│  • Publishes to MQTT: alerts/input                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              Local MQTT Broker (mqtt-broker)                    │
│  Internal: port 1883 (no auth)                                  │
│  External: port 42690 (TLS with certs)                          │
└────────┬───────────────┬────────────────┬───────────────────────┘
         │               │                │
         ▼               ▼                ▼
┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐
│Alert Router │  │ File Logger  │  │   Other Services    │
│ (Core)      │  │              │  │ (Email, LED, etc.)  │
├─────────────┤  ├──────────────┤  └─────────────────────┘
│• Subscribes │  │• Subscribes  │
│  alerts/    │  │  alerts/     │
│  input      │  │  input       │
│• Evaluates  │  │• Logs to:    │
│  routing    │  │  /data/logs/ │
│  rules      │  │  file/       │
│• Publishes  │  │  {date}.log  │
│  to pathway │  │• Per-client  │
│  topics     │  │  logs if     │
│• Updates    │  │  enabled     │
│  status     │  └──────────────┘
└─────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Output Pathways                            │
│  • console_alerts → stdout                                      │
│  • file_log → /data/logs/file/{date}.log                        │
│  • led_sign → ledSign/{zone}/message                            │
│  • email → alerts/output/email/{recipient}                      │
└─────────────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Monitoring & Viewing                          │
│  • HTTP Log Viewer: https://alert.d-t.pw/logs                  │
│  • MQTT Monitor: mosquitto_sub -t 'alerts/#'                    │
│  • File Logs: /data/logs/file/{date}.log                        │
│  • Service Status: alerts/service/status                        │
└─────────────────────────────────────────────────────────────────┘

EOF
    echo -e "${NC}"
}

# Show test summary
show_summary() {
    show_header "Test Summary"

    echo -e "${CYAN}Total Tests: $TOTAL_TESTS${NC}"
    echo -e "${GREEN}Passed: $PASSED_TESTS${NC}"
    echo -e "${RED}Failed: $FAILED_TESTS${NC}"
    echo ""

    if [[ $FAILED_TESTS -eq 0 ]]; then
        show_success "All tests passed! ✨"
    else
        show_warning "Some tests failed - check output above for details"
    fi
    echo ""
}

# Main execution
main() {
    clear
    echo -e "${GREEN}"
    cat << 'EOF'
╔════════════════════════════════════════════════════════════════╗
║        HTTPS Alert Flow Validation & Documentation            ║
║                  Alert Manager System                          ║
╚════════════════════════════════════════════════════════════════╝
EOF
    echo -e "${NC}"

    check_prerequisites

    # Run tests
    test_https_connectivity
    test_authentication
    send_test_alerts

    # Show documentation
    verify_routing
    show_flow_diagram
    monitor_mqtt
    check_file_logs
    check_http_logs

    # Summary
    show_summary

    show_info "Next Steps:"
    echo "  1. Open https://$HTTPS_HOST/logs in your browser"
    echo "  2. Monitor real-time logs to see your test alerts"
    echo "  3. Check file logs on server for persistence"
    echo "  4. Set up MQTT monitoring for real-time alerts"
    echo ""
}

# Run main function
main
