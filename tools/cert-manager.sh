#!/bin/bash

# Alert Manager Certificate Management System
# Provisions and revokes certificates for MQTT clients
#
# ESP32 Compatibility: Uses 2048-bit RSA keys instead of 4096-bit
# ESP32 devices have limited memory and crypto acceleration, making
# 4096-bit RSA operations slow or impossible. 2048-bit provides
# adequate security while maintaining compatibility.

set -e

# Configuration - Detect if running in container or on host
if [ -d "/data/certs" ]; then
    # Running inside container
    CERT_DIR="/data/certs"
    CONFIG_DIR="/data/config"
else
    # Running on host - use relative path from script location
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
    CERT_DIR="$PROJECT_ROOT/data/certs"
    CONFIG_DIR="$PROJECT_ROOT/data/config"
fi

CA_DIR="$CERT_DIR/ca"
CLIENT_DIR="$CERT_DIR/clients"
CRL_DIR="$CERT_DIR/crl"
BACKUP_DIR="$CERT_DIR/backups"

# Certificate validity periods (set to maximum - effectively no expiration)
CA_VALIDITY=36500     # 100 years (effectively permanent)
CLIENT_VALIDITY=36500 # 100 years (effectively permanent)
SERVER_VALIDITY=36500 # 100 years (effectively permanent)

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging
log() {
    echo -e "${BLUE}[$(date '+%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

# Initialize certificate infrastructure
init_ca() {
    log "Initializing Certificate Authority..."

    # Create directory structure
    mkdir -p "$CA_DIR" "$CLIENT_DIR" "$CRL_DIR" "$BACKUP_DIR"

    # Generate CA private key
    if [[ ! -f "$CA_DIR/ca.key" ]]; then
        log "Generating CA private key..."
        openssl genrsa -out "$CA_DIR/ca.key" 2048
        chmod 600 "$CA_DIR/ca.key"
    fi

    # Generate CA certificate
    if [[ ! -f "$CA_DIR/ca.crt" ]]; then
        log "Generating CA certificate..."
        openssl req -new -x509 -days $CA_VALIDITY -key "$CA_DIR/ca.key" -out "$CA_DIR/ca.crt" \
            -subj "/C=US/ST=State/L=City/O=AlertManager/OU=CertificateAuthority/CN=AlertManager-CA"
        chmod 644 "$CA_DIR/ca.crt"
    fi

    # Initialize certificate database
    if [[ ! -f "$CA_DIR/index.txt" ]]; then
        touch "$CA_DIR/index.txt"
        echo "1000" > "$CA_DIR/serial"
        echo "1000" > "$CA_DIR/crlnumber"
    fi

    # Create CA configuration file
    cat > "$CA_DIR/ca.conf" <<EOF
[ ca ]
default_ca = CA_default

[ CA_default ]
dir               = $CA_DIR
certs             = \$dir
crl_dir           = $CRL_DIR
new_certs_dir     = \$dir/newcerts
database          = \$dir/index.txt
serial            = \$dir/serial
RANDFILE          = \$dir/private/.rand
private_key       = \$dir/ca.key
certificate       = \$dir/ca.crt
crlnumber         = \$dir/crlnumber
crl               = $CRL_DIR/ca.crl
crl_extensions    = crl_ext
default_crl_days  = 30
default_md        = sha256
name_opt          = ca_default
cert_opt          = ca_default
default_days      = $CLIENT_VALIDITY
preserve          = no
policy            = policy_loose

[ policy_loose ]
countryName             = optional
stateOrProvinceName     = optional
localityName            = optional
organizationName        = optional
organizationalUnitName  = optional
commonName              = supplied
emailAddress            = optional

[ req ]
default_bits        = 2048
distinguished_name  = req_distinguished_name
string_mask         = utf8only
default_md          = sha256

[ req_distinguished_name ]
countryName                     = Country Name (2 letter code)
stateOrProvinceName             = State or Province Name
localityName                    = Locality Name
0.organizationName              = Organization Name
organizationalUnitName          = Organizational Unit Name
commonName                      = Common Name
emailAddress                    = Email Address

[ crl_ext ]
authorityKeyIdentifier=keyid:always

[ client_cert ]
basicConstraints = CA:FALSE
nsCertType = client
keyUsage = critical, nonRepudiation, digitalSignature, keyEncipherment
extendedKeyUsage = clientAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF

    mkdir -p "$CA_DIR/newcerts"
    success "Certificate Authority initialized"
}

# Generate server certificate (for MQTT broker)
generate_server_cert() {
    local server_name="${1:-alert.d-t.pw}"

    log "Generating server certificate for $server_name..."

    # Generate server private key
    openssl genrsa -out "$CA_DIR/server.key" 2048

    # Create server certificate request
    cat > "$CA_DIR/server.conf" <<EOF
[req]
distinguished_name = req_distinguished_name
req_extensions = v3_req
prompt = no

[req_distinguished_name]
C = US
ST = State
L = City
O = AlertManager
OU = MQTT
CN = $server_name

[v3_req]
keyUsage = keyEncipherment, dataEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt_names

[alt_names]
DNS.1 = $server_name
DNS.2 = *.d-t.pw
EOF

    # Generate certificate request
    openssl req -new -key "$CA_DIR/server.key" -out "$CA_DIR/server.csr" -config "$CA_DIR/server.conf"

    # Sign server certificate
    openssl ca -config "$CA_DIR/ca.conf" -extensions v3_req -extfile "$CA_DIR/server.conf" \
        -days $SERVER_VALIDITY -notext -md sha256 -in "$CA_DIR/server.csr" -out "$CA_DIR/server.crt" -batch

    # Clean up
    rm "$CA_DIR/server.csr" "$CA_DIR/server.conf"

    success "Server certificate generated for $server_name"
}

# Generate client certificate
generate_client_cert() {
    local client_name="$1"
    local client_type="${2:-client}"
    local validity="$CLIENT_VALIDITY"  # Always use permanent validity

    if [[ -z "$client_name" ]]; then
        error "Client name is required"
        exit 1
    fi

    log "Generating client certificate for $client_name (type: $client_type)..."

    local client_key="$CLIENT_DIR/${client_name}.key"
    local client_csr="$CLIENT_DIR/${client_name}.csr"
    local client_crt="$CLIENT_DIR/${client_name}.crt"

    # Check if certificate already exists
    if [[ -f "$client_crt" ]]; then
        warning "Certificate for $client_name already exists"
        read -p "Overwrite existing certificate? (y/N): " -r
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            log "Skipping certificate generation"
            return 0
        fi
        # Revoke existing certificate
        revoke_certificate "$client_name"
    fi

    # Generate client private key
    openssl genrsa -out "$client_key" 2048
    chmod 600 "$client_key"

    # Generate certificate request
    openssl req -new -key "$client_key" -out "$client_csr" \
        -subj "/C=US/ST=State/L=City/O=AlertManager/OU=MQTT-$client_type/CN=$client_name"

    # Sign client certificate
    openssl ca -config "$CA_DIR/ca.conf" -extensions client_cert \
        -days "$validity" -notext -md sha256 -in "$client_csr" -out "$client_crt" -batch

    # Clean up
    rm "$client_csr"

    # Create client bundle
    create_client_bundle "$client_name"

    success "Client certificate generated for $client_name"
    log "Certificate files:"
    log "  Private Key: $client_key"
    log "  Certificate: $client_crt"
    log "  Bundle: $CLIENT_DIR/${client_name}-bundle.tar.gz"
}

# Create client certificate bundle for distribution
create_client_bundle() {
    local client_name="$1"
    local bundle_dir="$CLIENT_DIR/${client_name}-bundle"
    local bundle_file="$CLIENT_DIR/${client_name}-bundle.tar.gz"

    # Create temporary bundle directory
    rm -rf "$bundle_dir"
    mkdir -p "$bundle_dir"

    # Copy required files
    cp "$CA_DIR/ca.crt" "$bundle_dir/"
    cp "$CLIENT_DIR/${client_name}.crt" "$bundle_dir/client.crt"
    cp "$CLIENT_DIR/${client_name}.key" "$bundle_dir/client.key"

    # Create connection config
    cat > "$bundle_dir/mqtt-config.json" <<EOF
{
  "mqtt": {
    "broker": "alert.d-t.pw",
    "port": 42690,
    "use_tls": true,
    "ca_cert": "ca.crt",
    "client_cert": "client.crt",
    "client_key": "client.key",
    "tls_version": "tlsv1.3"
  },
  "client_info": {
    "name": "$client_name",
    "generated": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")",
    "validity": "permanent (100 years)",
    "serial": "$(openssl x509 -in "$CLIENT_DIR/${client_name}.crt" -noout -serial | cut -d= -f2)"
  }
}
EOF

    # Create installation script
    cat > "$bundle_dir/install.sh" <<'EOF'
#!/bin/bash
# Alert Manager MQTT Client Certificate Installation

INSTALL_DIR="/opt/alert-manager/certs"
CONFIG_DIR="/opt/alert-manager/config"

echo "Installing Alert Manager MQTT client certificates..."

# Create directories
sudo mkdir -p "$INSTALL_DIR" "$CONFIG_DIR"

# Install certificates
sudo cp ca.crt "$INSTALL_DIR/"
sudo cp client.crt "$INSTALL_DIR/"
sudo cp client.key "$INSTALL_DIR/"
sudo cp mqtt-config.json "$CONFIG_DIR/"

# Set permissions
sudo chmod 644 "$INSTALL_DIR/ca.crt" "$INSTALL_DIR/client.crt"
sudo chmod 600 "$INSTALL_DIR/client.key"
sudo chmod 644 "$CONFIG_DIR/mqtt-config.json"

# Set ownership (adjust user as needed)
sudo chown -R alert:alert "$INSTALL_DIR" "$CONFIG_DIR" 2>/dev/null || true

echo "Installation complete!"
echo "CA Certificate: $INSTALL_DIR/ca.crt"
echo "Client Certificate: $INSTALL_DIR/client.crt"
echo "Client Key: $INSTALL_DIR/client.key"
echo "MQTT Config: $CONFIG_DIR/mqtt-config.json"
EOF

    chmod +x "$bundle_dir/install.sh"

    # Create test script
    cat > "$bundle_dir/test-connection.sh" <<'EOF'
#!/bin/bash
# Test MQTT connection with certificates

echo "Testing MQTT connection to alert.d-t.pw:42690..."

# Test publish
mosquitto_pub \
    --cafile ca.crt \
    --cert client.crt \
    --key client.key \
    -h alert.d-t.pw \
    -p 42690 \
    -t "alerts/test" \
    -m "{\"test\": true, \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\", \"client\": \"$(basename $(pwd))\"}"

if [ $? -eq 0 ]; then
    echo "✓ Connection test successful!"
else
    echo "✗ Connection test failed!"
    exit 1
fi
EOF

    chmod +x "$bundle_dir/test-connection.sh"

    # Create bundle archive
    tar -czf "$bundle_file" -C "$CLIENT_DIR" "${client_name}-bundle"

    # Clean up temporary directory
    rm -rf "$bundle_dir"

    log "Client bundle created: $bundle_file"
}

# List certificates
list_certificates() {
    log "Active certificates:"

    if [[ -f "$CA_DIR/index.txt" ]]; then
        while IFS= read -r line; do
            if [[ $line == V* ]]; then
                local serial=$(echo "$line" | cut -f4)
                local subject=$(echo "$line" | cut -f6)
                local cn=$(echo "$subject" | sed 's/.*CN=\([^/]*\).*/\1/')
                local expiry=$(echo "$line" | cut -f2)
                local exp_date=$(date -d "${expiry:0:8}" +"%Y-%m-%d" 2>/dev/null || echo "Invalid date")
                echo "  ✓ $cn (Serial: $serial, Expires: $exp_date)"
            fi
        done < "$CA_DIR/index.txt"
    else
        warning "No certificate database found"
    fi

    echo
    log "Revoked certificates:"
    if [[ -f "$CRL_DIR/ca.crl" ]]; then
        openssl crl -in "$CRL_DIR/ca.crl" -noout -text | grep "Serial Number:" | while read -r line; do
            local serial=$(echo "$line" | sed 's/.*Serial Number: \(.*\)/\1/')
            echo "  ✗ Serial: $serial"
        done
    else
        echo "  None"
    fi
}

# Revoke certificate
revoke_certificate() {
    local client_name="$1"

    if [[ -z "$client_name" ]]; then
        error "Client name is required for revocation"
        exit 1
    fi

    local client_crt="$CLIENT_DIR/${client_name}.crt"

    if [[ ! -f "$client_crt" ]]; then
        error "Certificate file not found: $client_crt"
        exit 1
    fi

    log "Revoking certificate for $client_name..."

    # Create backup
    local backup_file="$BACKUP_DIR/${client_name}-revoked-$(date +%Y%m%d-%H%M%S).tar.gz"
    tar -czf "$backup_file" -C "$CLIENT_DIR" "${client_name}."*

    # Revoke certificate
    openssl ca -config "$CA_DIR/ca.conf" -revoke "$client_crt"

    # Generate new CRL
    generate_crl

    # Remove certificate files
    rm -f "$CLIENT_DIR/${client_name}."*
    rm -f "$CLIENT_DIR/${client_name}-bundle.tar.gz"

    success "Certificate revoked for $client_name"
    log "Backup created: $backup_file"
    warning "Make sure to update the CRL on your MQTT broker!"
}

# Generate Certificate Revocation List
generate_crl() {
    log "Generating Certificate Revocation List..."

    openssl ca -config "$CA_DIR/ca.conf" -gencrl -out "$CRL_DIR/ca.crl"

    # Convert to PEM format for easier distribution
    openssl crl -in "$CRL_DIR/ca.crl" -outform PEM -out "$CRL_DIR/ca-crl.pem"

    success "CRL generated: $CRL_DIR/ca.crl"
    log "PEM format: $CRL_DIR/ca-crl.pem"
}

# Show certificate details
show_certificate() {
    local client_name="$1"

    if [[ -z "$client_name" ]]; then
        error "Client name is required"
        exit 1
    fi

    local client_crt="$CLIENT_DIR/${client_name}.crt"

    if [[ ! -f "$client_crt" ]]; then
        error "Certificate file not found: $client_crt"
        exit 1
    fi

    log "Certificate details for $client_name:"
    openssl x509 -in "$client_crt" -text -noout
}

# Renew certificate (revoke + regenerate)
renew_certificate() {
    local client_name="$1"
    local validity="$CLIENT_VALIDITY"  # Always use permanent validity

    if [[ -z "$client_name" ]]; then
        error "Client name is required"
        exit 1
    fi

    log "Renewing certificate for $client_name..."

    # Check if certificate exists
    if [[ -f "$CLIENT_DIR/${client_name}.crt" ]]; then
        revoke_certificate "$client_name"
    fi

    # Generate new certificate
    generate_client_cert "$client_name" "client" "$validity"

    success "Certificate renewed for $client_name"
}

# Show help
show_help() {
    cat <<EOF
Alert Manager Certificate Management System

USAGE:
    $0 <command> [options]

COMMANDS:
    init                    Initialize Certificate Authority
    server [hostname]       Generate server certificate (default: alert.d-t.pw)
    client <name> [type]     Generate client certificate (permanent)
    list                    List all certificates
    show <name>            Show certificate details
    revoke <name>          Revoke client certificate
    renew <name>           Renew client certificate (permanent)
    crl                    Generate Certificate Revocation List
    help                   Show this help message

EXAMPLES:
    $0 init                                   # Initialize CA
    $0 server alert.d-t.pw                   # Generate server cert (permanent)
    $0 client home-assistant router          # Generate router cert (permanent)
    $0 client esp32-kitchen device           # Generate device cert (permanent)
    $0 list                                   # List certificates
    $0 revoke compromised-device              # Revoke certificate
    $0 renew home-assistant                   # Renew certificate (permanent)

CERTIFICATE TYPES:
    router      - Alert router services (permanent)
    device      - IoT devices, ESP32, etc (permanent)
    client      - General MQTT clients (permanent)
    service     - Containerized services (permanent)

NOTE: All certificates are issued with 100-year validity (effectively permanent).
Use revocation instead of expiration for security management.

FILES:
    CA Certificate:     $CA_DIR/ca.crt
    Client Certificates: $CLIENT_DIR/
    Client Bundles:     $CLIENT_DIR/*-bundle.tar.gz
    Revocation List:    $CRL_DIR/ca.crl

EOF
}

# Main command processing
case "${1:-help}" in
    init)
        init_ca
        ;;
    server)
        init_ca
        generate_server_cert "$2"
        ;;
    client)
        if [[ -z "$2" ]]; then
            error "Client name is required"
            show_help
            exit 1
        fi
        init_ca
        generate_client_cert "$2" "${3:-client}"
        ;;
    list)
        list_certificates
        ;;
    show)
        show_certificate "$2"
        ;;
    revoke)
        revoke_certificate "$2"
        ;;
    renew)
        renew_certificate "$2"
        ;;
    crl)
        generate_crl
        ;;
    help|--help|-h)
        show_help
        ;;
    *)
        error "Unknown command: $1"
        show_help
        exit 1
        ;;
esac