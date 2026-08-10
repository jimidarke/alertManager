#!/bin/bash

# Send HTTP Alert via curl
# Tests the HTTP gateway endpoint with JSON payloads

set -e

# Configuration
HTTP_HOST="alert.d-t.pw"
HTTP_PORT="443"
HTTP_SCHEME="https"
ENDPOINT="alert"

# Authentication -- read from the environment, never hardcoded.
# Export these before running, e.g.:
#   export ALERT_USER=my-sender ALERT_PASSWORD='...'
# (This script previously shipped a real gateway password in git. It has
# been revoked along with every other credential in the v3.0.0 auth reset.)
AUTH_USERNAME="${ALERT_USER:?set ALERT_USER (gateway username)}"
AUTH_PASSWORD="${ALERT_PASSWORD:?set ALERT_PASSWORD (gateway password)}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

show_help() {
    echo -e "${BLUE}=== HTTP Alert Sender ===${NC}"
    echo ""
    echo "Usage: $0 [OPTIONS] [ALERT_TYPE]"
    echo ""
    echo "Options:"
    echo "  -h, --host HOST      HTTP gateway host (default: alert.d-t.pw)"
    echo "  -p, --port PORT      HTTP gateway port (default: 443)"
    echo "  -s, --scheme SCHEME  URL scheme: http or https (default: https)"
    echo "  -u, --user USER      Username for basic auth"
    echo "  -P, --pass PASS      Password for basic auth"
    echo "  -c, --count NUM      Number of alerts to send (default: 1)"
    echo "  -i, --interval SEC   Interval between alerts in seconds (default: 1)"
    echo "  -C, --client NAME    Client name/identifier"
    echo "  -d, --debug          Enable debug mode (show curl headers and verbose output)"
    echo "  --help               Show this help"
    echo ""
    echo "Alert Types:"
    echo "  critical             Send critical security alert"
    echo "  warning              Send warning system alert"
    echo "  notice               Send notice operational alert"
    echo "  info                 Send info status alert (default)"
    echo "  debug                Send debug message"
    echo "  test                 Send test alert"
    echo ""
    echo "Examples:"
    echo "  $0                                    # Send single info alert via HTTPS"
    echo "  $0 -u user123 -P pass456 critical     # Send critical alert with auth"
    echo "  $0 -u sensor01 -P mypass -C prod-web-01 warning  # With client ID"
    echo "  $0 -u sensor01 -P mypass notice       # Send notice operational alert"
    echo "  $0 -d -u user -P pass critical        # Debug mode (show headers and verbose output)"
    echo "  $0 -s http -p 8080 test               # Use HTTP instead of HTTPS"
}

# Default settings
COUNT=1
INTERVAL=1
ALERT_TYPE="info"
CLIENT_NAME=""
DEBUG=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--host)
            HTTP_HOST="$2"
            shift 2
            ;;
        -p|--port)
            HTTP_PORT="$2"
            shift 2
            ;;
        -s|--scheme)
            HTTP_SCHEME="$2"
            shift 2
            ;;
        -u|--user)
            AUTH_USERNAME="$2"
            shift 2
            ;;
        -P|--pass)
            AUTH_PASSWORD="$2"
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
        -C|--client)
            CLIENT_NAME="$2"
            shift 2
            ;;
        -d|--debug)
            DEBUG=true
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        critical|warning|notice|info|debug|test)
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

# Build base URL (omit :443 for HTTPS, omit :80 for HTTP)
if [[ "$HTTP_SCHEME" == "https" && "$HTTP_PORT" == "443" ]]; then
    BASE_URL="https://${HTTP_HOST}"
elif [[ "$HTTP_SCHEME" == "http" && "$HTTP_PORT" == "80" ]]; then
    BASE_URL="http://${HTTP_HOST}"
else
    BASE_URL="${HTTP_SCHEME}://${HTTP_HOST}:${HTTP_PORT}"
fi

# Build auth options for curl
AUTH_OPTS=""
if [[ -n "$AUTH_USERNAME" && -n "$AUTH_PASSWORD" ]]; then
    AUTH_OPTS="-u $AUTH_USERNAME:$AUTH_PASSWORD"
    AUTH_STATUS="${GREEN}Enabled (user: $AUTH_USERNAME)${NC}"
else
    AUTH_STATUS="${YELLOW}Disabled${NC}"
fi

echo -e "${BLUE}=== HTTP Alert Sender ===${NC}"
echo -e "${CYAN}Gateway: $BASE_URL${NC}"
echo -e "${CYAN}Authentication: $AUTH_STATUS${NC}"
echo -e "${CYAN}Alert Type: $ALERT_TYPE${NC}"
echo -e "${CYAN}Count: $COUNT${NC}"
if [[ "$DEBUG" == true ]]; then
    echo -e "${MAGENTA}Debug Mode: ENABLED${NC}"
fi
if [[ -n "$CLIENT_NAME" ]]; then
    echo -e "${CYAN}Client: $CLIENT_NAME${NC}"
fi
echo ""

# Check if curl is available
if ! command -v curl &> /dev/null; then
    echo -e "${RED}Error: curl not found${NC}"
    exit 1
fi

# Generate alert payload
generate_alert() {
    local level="$1"
    local category="$2"
    local title="$3"
    local message="$4"
    local source_id="${5:-http-test}"

    local timestamp=$(date +%s)

    # Build JSON payload with optional client field
    local payload="{
  \"timestamp\": $timestamp,
  \"level\": \"$level\",
  \"category\": \"$category\",
  \"title\": \"$title\",
  \"message\": \"$message\",
  \"source\": \"$source_id\""

    # Add client field if provided
    if [[ -n "$CLIENT_NAME" ]]; then
        payload="$payload,
  \"client\": \"$CLIENT_NAME\""
    fi

    # Add metadata
    payload="$payload,
  \"metadata\": {
    \"http_test\": true,
    \"test_id\": \"$(date +%s)-$$\",
    \"sent_via\": \"curl\"
  }
}"

    echo "$payload"
}

# Send HTTP request
send_http_alert() {
    local payload="$1"
    local description="$2"

    echo -e "${YELLOW}Sending: $description${NC}"

    # Build curl debug options
    local curl_opts="-s -w \\n%{http_code}"
    local stderr_redirect="2>/dev/null"

    if [[ "$DEBUG" == true ]]; then
        echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${CYAN}Transmitted Payload:${NC}"
        if command -v jq &> /dev/null && echo "$payload" | jq . &> /dev/null; then
            echo "$payload" | jq '.' | sed 's/^/  /'
        else
            echo "$payload" | sed 's/^/  /'
        fi
        echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
        curl_opts="-v -w \\n%{http_code}"
        stderr_redirect="2>&1"
    fi

    # Send POST request
    response=$(eval "curl $curl_opts \
        -X POST \
        -H 'Content-Type: application/json' \
        $AUTH_OPTS \
        -d '$payload' \
        '$BASE_URL/$ENDPOINT' $stderr_redirect")

    # Parse response
    http_code=$(echo "$response" | tail -n1 | sed 's/^n//' | grep -o '[0-9]\+')
    body=$(echo "$response" | head -n -1)

    # Show debug output
    if [[ "$DEBUG" == true ]]; then
        # Extract only the JSON response (last complete JSON object)
        clean_body=$(echo "$body" | grep -E '^\{.*\}$' | tail -n1)
        if [[ -z "$clean_body" ]]; then
            # Fallback: try to extract any line that looks like JSON
            clean_body=$(echo "$body" | grep '^\s*{' | tail -n1)
        fi

        echo ""
        echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo -e "${CYAN}HTTP Response Status Code:${NC} ${YELLOW}$http_code${NC}"
        echo -e "${CYAN}Response Body:${NC}"
        if command -v jq &> /dev/null && echo "$clean_body" | jq . &> /dev/null 2>&1; then
            echo "$clean_body" | jq '.' | sed 's/^/  /'
        else
            echo "$clean_body" | sed 's/^/  /'
        fi
        echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
        echo ""
    fi

    if [[ "$http_code" == "200" ]]; then
        echo -e "${GREEN}✓ Alert sent successfully (HTTP $http_code)${NC}"

        # Show response in non-debug mode
        if [[ "$DEBUG" != true ]]; then
            # Pretty print response if it's JSON
            if command -v jq &> /dev/null && echo "$body" | jq . &> /dev/null 2>&1; then
                echo -e "${CYAN}Response:${NC}"
                echo "$body" | jq '.' | sed 's/^/  /'
            else
                echo -e "${CYAN}Response:${NC} $body"
            fi
        fi
    else
        echo -e "${RED}✗ Request failed (HTTP $http_code)${NC}"
        if [[ "$DEBUG" != true ]]; then
            echo -e "${RED}Response:${NC} $body"
        fi
        return 1
    fi
    echo ""
}

# Main sending logic
echo -e "${GREEN}Starting HTTP alert transmission...${NC}"
echo ""

for ((i=1; i<=COUNT; i++)); do
    case "$ALERT_TYPE" in
        critical)
            payload=$(generate_alert "critical" "security" "HTTP Test: Critical Alert" "This is a critical test alert sent via HTTP gateway" "http-critical-test")
            send_http_alert "$payload" "Critical alert via HTTP ($i/$COUNT)"
            ;;
        warning)
            payload=$(generate_alert "warning" "system" "HTTP Test: Warning Alert" "This is a warning test alert sent via HTTP gateway" "http-warning-test")
            send_http_alert "$payload" "Warning alert via HTTP ($i/$COUNT)"
            ;;
        notice)
            payload=$(generate_alert "notice" "system" "HTTP Test: Notice Alert" "This is a notice operational alert sent via HTTP gateway" "http-notice-test")
            send_http_alert "$payload" "Notice alert via HTTP ($i/$COUNT)"
            ;;
        info)
            payload=$(generate_alert "info" "network" "HTTP Test: Info Alert" "This is an info test alert sent via HTTP gateway" "http-info-test")
            send_http_alert "$payload" "Info alert via HTTP ($i/$COUNT)"
            ;;
        debug)
            payload=$(generate_alert "debug" "application" "HTTP Test: Debug Message" "This is a debug message sent via HTTP gateway" "http-debug-test")
            send_http_alert "$payload" "Debug message via HTTP ($i/$COUNT)"
            ;;
        test)
            # Use the /test endpoint
            echo -e "${YELLOW}Using /test endpoint...${NC}"

            # Build curl debug options
            local test_curl_opts="-s -w \\n%{http_code}"
            local test_stderr_redirect="2>/dev/null"

            if [[ "$DEBUG" == true ]]; then
                echo -e "${MAGENTA}Debug mode enabled - showing verbose curl output${NC}"
                echo ""
                test_curl_opts="-v -w \\n%{http_code}"
                test_stderr_redirect="2>&1"
            fi

            response=$(eval "curl $test_curl_opts \
                -X POST \
                $AUTH_OPTS \
                '$BASE_URL/test' $test_stderr_redirect")

            http_code=$(echo "$response" | tail -n1 | sed 's/^n//' | grep -o '[0-9]\+')
            body=$(echo "$response" | head -n -1)

            # Show debug output
            if [[ "$DEBUG" == true ]]; then
                # Extract only the JSON response (last complete JSON object)
                clean_body=$(echo "$body" | grep -E '^\{.*\}$' | tail -n1)
                if [[ -z "$clean_body" ]]; then
                    # Fallback: try to extract any line that looks like JSON
                    clean_body=$(echo "$body" | grep '^\s*{' | tail -n1)
                fi

                echo ""
                echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo -e "${CYAN}HTTP Response Status Code:${NC} ${YELLOW}$http_code${NC}"
                echo -e "${CYAN}Response Body:${NC}"
                if command -v jq &> /dev/null && echo "$clean_body" | jq . &> /dev/null 2>&1; then
                    echo "$clean_body" | jq '.' | sed 's/^/  /'
                else
                    echo "$clean_body" | sed 's/^/  /'
                fi
                echo -e "${MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
                echo ""
            fi

            if [[ "$http_code" == "200" ]]; then
                echo -e "${GREEN}✓ Test alert sent successfully (HTTP $http_code)${NC}"
                if [[ "$DEBUG" != true ]]; then
                    if command -v jq &> /dev/null; then
                        echo "$body" | jq '.' | sed 's/^/  /'
                    else
                        echo "$body"
                    fi
                fi
            else
                echo -e "${RED}✗ Test alert failed (HTTP $http_code)${NC}"
                if [[ "$DEBUG" != true ]]; then
                    echo "$body"
                fi
            fi
            echo ""
            ;;
        *)
            echo -e "${RED}Unknown alert type: $ALERT_TYPE${NC}"
            exit 1
            ;;
    esac

    # Wait between alerts if sending multiple
    if [[ $i -lt $COUNT && $INTERVAL -gt 0 ]]; then
        echo -e "${BLUE}Waiting ${INTERVAL}s before next alert...${NC}"
        sleep "$INTERVAL"
    fi
done

echo -e "${GREEN}✅ Completed sending $COUNT HTTP alert(s)${NC}"