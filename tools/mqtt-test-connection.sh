#!/bin/bash

# MQTT Connection Test Script
# Tests MQTT broker connectivity with available authentication methods
# Automatically detects certificates and credentials in the current directory

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# MQTT Broker Configuration
MQTT_HOST="alert.d-t.pw"
MQTT_PORT=42690
MQTT_TOPIC_TEST="alerts/test"
MQTT_TOPIC_INPUT="alerts/input"
MQTT_TOPIC_STATUS="alerts/service/status"

# Script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Certificate file paths (relative to script directory)
CA_CERT="$SCRIPT_DIR/ca.crt"
CLIENT_CERT="$SCRIPT_DIR/client.crt"
CLIENT_KEY="$SCRIPT_DIR/client.key"

# Check for alternate locations
if [[ ! -f "$CA_CERT" ]]; then
    # Check in parent certs directory
    if [[ -f "$SCRIPT_DIR/../data/certs/ca/ca.crt" ]]; then
        CA_CERT="$SCRIPT_DIR/../data/certs/ca/ca.crt"
    fi
fi

if [[ ! -f "$CLIENT_CERT" ]]; then
    if [[ -f "$SCRIPT_DIR/../data/certs/clients/client.crt" ]]; then
        CLIENT_CERT="$SCRIPT_DIR/../data/certs/clients/client.crt"
        CLIENT_KEY="$SCRIPT_DIR/../data/certs/clients/client.key"
    fi
fi

# Logging functions
log() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

step() {
    echo -e "${CYAN}[STEP]${NC} $1"
}

# Check if mosquitto_pub/sub are installed
check_mosquitto_clients() {
    if ! command -v mosquitto_pub &> /dev/null; then
        error "mosquitto_pub not found"
        echo ""
        echo "Install mosquitto clients:"
        echo "  Ubuntu/Debian: sudo apt-get install mosquitto-clients"
        echo "  macOS: brew install mosquitto"
        echo "  Arch Linux: sudo pacman -S mosquitto"
        exit 1
    fi

    if ! command -v mosquitto_sub &> /dev/null; then
        error "mosquitto_sub not found"
        exit 1
    fi
}

# Detect available authentication methods
detect_auth_method() {
    echo ""
    step "Detecting available authentication files..."
    echo ""

    local auth_methods=()

    # Check for CA certificate
    if [[ -f "$CA_CERT" ]]; then
        success "Found CA certificate: $CA_CERT"
        auth_methods+=("ca")
    else
        warning "CA certificate not found: $CA_CERT"
    fi

    # Check for client certificate
    if [[ -f "$CLIENT_CERT" ]] && [[ -f "$CLIENT_KEY" ]]; then
        success "Found client certificate: $CLIENT_CERT"
        success "Found client key: $CLIENT_KEY"
        auth_methods+=("cert")
    else
        warning "Client certificate/key not found"
    fi

    echo ""

    # Determine authentication method
    if [[ " ${auth_methods[*]} " =~ " cert " ]] && [[ " ${auth_methods[*]} " =~ " ca " ]]; then
        echo "📋 Available authentication methods:"
        echo "  1) Client Certificate (recommended for services)"
        echo "  2) Username/Password (recommended for ESP32/IoT)"
        echo ""
        return 0
    elif [[ " ${auth_methods[*]} " =~ " ca " ]]; then
        echo "📋 Available authentication methods:"
        echo "  1) Username/Password only (ESP32/IoT mode)"
        echo ""
        return 0
    else
        error "No authentication files found!"
        echo ""
        echo "Options:"
        echo "  1. Place certificates in script directory:"
        echo "     - ca.crt (required)"
        echo "     - client.crt + client.key (optional, for certificate auth)"
        echo ""
        echo "  2. Generate certificates:"
        echo "     cd $SCRIPT_DIR/.."
        echo "     ./tools/cert-manager.sh init"
        echo "     ./tools/cert-manager.sh server alert.d-t.pw"
        echo ""
        exit 1
    fi
}

# Test with client certificate
test_certificate_auth() {
    echo ""
    step "Testing connection with CLIENT CERTIFICATE..."
    echo ""
    log "Broker: $MQTT_HOST:$MQTT_PORT"
    log "Using: CA cert + Client cert + Client key"
    echo ""

    # Extract client name from certificate
    local client_name=$(openssl x509 -in "$CLIENT_CERT" -noout -subject | sed -n 's/.*CN = \(.*\)/\1/p' | xargs)
    log "Client identity: $client_name"
    echo ""

    # Test publish
    step "Test 1: Publishing test message..."
    local timestamp=$(date +%s)
    local payload="{\"timestamp\":$timestamp,\"level\":\"info\",\"category\":\"system\",\"title\":\"MQTT Test\",\"message\":\"Certificate auth test from $client_name\",\"source\":\"mqtt-test-script\",\"client\":\"test\"}"

    if mosquitto_pub \
        --cafile "$CA_CERT" \
        --cert "$CLIENT_CERT" \
        --key "$CLIENT_KEY" \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -t "$MQTT_TOPIC_INPUT" \
        -m "$payload" \
        -d 2>&1; then
        success "Message published successfully"
    else
        error "Failed to publish message"
        return 1
    fi

    echo ""

    # Test subscribe (with timeout)
    step "Test 2: Subscribing to status topic (5 second timeout)..."
    echo ""

    if timeout 5 mosquitto_sub \
        --cafile "$CA_CERT" \
        --cert "$CLIENT_CERT" \
        --key "$CLIENT_KEY" \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -t "$MQTT_TOPIC_STATUS" \
        -C 1 \
        -v 2>&1; then
        echo ""
        success "Successfully received status message"
    else
        warning "Subscribe test timed out (may be normal if no status published yet)"
    fi

    echo ""
    success "Certificate authentication test completed!"
    return 0
}

# Test with username/password
test_password_auth() {
    local username="$1"
    local password="$2"

    echo ""
    step "Testing connection with USERNAME/PASSWORD..."
    echo ""
    log "Broker: $MQTT_HOST:$MQTT_PORT"
    log "Username: $username"
    log "Using: CA cert for TLS verification"
    echo ""

    # Check if CA cert exists
    if [[ ! -f "$CA_CERT" ]]; then
        error "CA certificate required for TLS: $CA_CERT"
        return 1
    fi

    # Test publish
    step "Test 1: Publishing test message..."
    local timestamp=$(date +%s)
    local payload="{\"timestamp\":$timestamp,\"level\":\"info\",\"category\":\"system\",\"title\":\"MQTT Test\",\"message\":\"Username/password auth test from $username\",\"source\":\"mqtt-test-script\",\"client\":\"test\"}"

    if mosquitto_pub \
        --cafile "$CA_CERT" \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -u "$username" \
        -P "$password" \
        -t "$MQTT_TOPIC_INPUT" \
        -m "$payload" \
        -d 2>&1; then
        success "Message published successfully"
    else
        error "Failed to publish message (check credentials or ACL permissions)"
        return 1
    fi

    echo ""

    # Test subscribe (with timeout)
    step "Test 2: Subscribing to status topic (5 second timeout)..."
    echo ""

    if timeout 5 mosquitto_sub \
        --cafile "$CA_CERT" \
        -h "$MQTT_HOST" \
        -p "$MQTT_PORT" \
        -u "$username" \
        -P "$password" \
        -t "$MQTT_TOPIC_STATUS" \
        -C 1 \
        -v 2>&1; then
        echo ""
        success "Successfully received status message"
    else
        warning "Subscribe test timed out (may be normal if no status published yet)"
    fi

    echo ""
    success "Username/password authentication test completed!"
    return 0
}

# Interactive menu
show_menu() {
    echo ""
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "    MQTT Connection Test - Alert Manager"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo ""
    echo "Select authentication method:"
    echo ""

    if [[ -f "$CLIENT_CERT" ]] && [[ -f "$CLIENT_KEY" ]] && [[ -f "$CA_CERT" ]]; then
        echo "  1) Test with CLIENT CERTIFICATE"
    else
        echo "  1) Test with CLIENT CERTIFICATE (unavailable - files not found)"
    fi

    if [[ -f "$CA_CERT" ]]; then
        echo "  2) Test with USERNAME/PASSWORD"
    else
        echo "  2) Test with USERNAME/PASSWORD (unavailable - CA cert not found)"
    fi

    echo "  3) Show certificate information"
    echo "  4) Test raw TLS connection (openssl)"
    echo "  5) Exit"
    echo ""
    echo -n "Enter choice [1-5]: "
}

# Show certificate information
show_cert_info() {
    echo ""
    step "Certificate Information"
    echo ""

    if [[ -f "$CA_CERT" ]]; then
        echo "━━━ CA Certificate ━━━"
        openssl x509 -in "$CA_CERT" -noout -subject -issuer -dates
        echo ""
    fi

    if [[ -f "$CLIENT_CERT" ]]; then
        echo "━━━ Client Certificate ━━━"
        openssl x509 -in "$CLIENT_CERT" -noout -subject -issuer -dates
        echo ""
    fi

    if [[ ! -f "$CA_CERT" ]] && [[ ! -f "$CLIENT_CERT" ]]; then
        warning "No certificates found"
    fi
}

# Test raw TLS connection
test_tls_connection() {
    echo ""
    step "Testing raw TLS connection with openssl..."
    echo ""

    if [[ ! -f "$CA_CERT" ]]; then
        error "CA certificate required: $CA_CERT"
        return 1
    fi

    log "Connecting to $MQTT_HOST:$MQTT_PORT"
    log "This will show TLS handshake details..."
    echo ""

    echo "Q" | timeout 5 openssl s_client \
        -connect "$MQTT_HOST:$MQTT_PORT" \
        -CAfile "$CA_CERT" \
        -showcerts \
        2>&1 | grep -E "(CONNECTED|Verify return code|subject=|issuer=|Protocol|Cipher)"

    local exit_code=$?
    echo ""

    if [[ $exit_code -eq 0 ]]; then
        success "TLS connection successful"
    else
        error "TLS connection failed"
    fi
}

# Main script
main() {
    check_mosquitto_clients
    detect_auth_method

    # If command line arguments provided, use them
    if [[ $# -ge 2 ]]; then
        test_password_auth "$1" "$2"
        exit 0
    fi

    # Interactive mode
    while true; do
        show_menu
        read -r choice

        case $choice in
            1)
                if [[ -f "$CLIENT_CERT" ]] && [[ -f "$CLIENT_KEY" ]] && [[ -f "$CA_CERT" ]]; then
                    test_certificate_auth
                else
                    error "Client certificate files not available"
                fi
                ;;
            2)
                if [[ -f "$CA_CERT" ]]; then
                    echo ""
                    read -p "Enter MQTT username: " username
                    read -s -p "Enter MQTT password: " password
                    echo ""
                    test_password_auth "$username" "$password"
                else
                    error "CA certificate not available"
                fi
                ;;
            3)
                show_cert_info
                ;;
            4)
                test_tls_connection
                ;;
            5)
                echo ""
                log "Exiting..."
                exit 0
                ;;
            *)
                error "Invalid choice"
                ;;
        esac

        echo ""
        read -p "Press Enter to continue..."
    done
}

# Show usage if --help
if [[ "$1" == "--help" ]] || [[ "$1" == "-h" ]]; then
    cat <<EOF
MQTT Connection Test Script

USAGE:
    $0                          # Interactive mode
    $0 <username> <password>    # Direct test with credentials

DESCRIPTION:
    Tests MQTT broker connectivity with available authentication methods.
    Automatically detects certificates in the script directory or parent certs directory.

REQUIRED FILES:
    ca.crt              # CA certificate (always required for TLS)
    client.crt          # Client certificate (optional, for cert auth)
    client.key          # Client private key (optional, for cert auth)

CERTIFICATE LOCATIONS:
    1. Script directory (same as this script)
    2. Parent certs directory (../data/certs/ca/ and ../data/certs/clients/)

EXAMPLES:
    # Interactive mode
    $0

    # Direct test with username/password
    $0 esp-kitchen MySecurePassword123

    # Run from certificate bundle directory
    cd data/certs/clients/my-device-bundle
    ../../../../tools/mqtt-test-connection.sh

MQTT BROKER:
    Host: $MQTT_HOST
    Port: $MQTT_PORT
    TLS: Required (TLSv1.2)

EOF
    exit 0
fi

# Run main
main "$@"
