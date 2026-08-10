#!/bin/bash

set -e

# Configuration - Detect if running in container or on host
if [ -d "/data/config" ]; then
    # Running inside container
    CONFIG_DIR="/data/config"
else
    # Running on host - use relative path from script location
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
    CONFIG_DIR="$PROJECT_ROOT/data/config"
fi

AUTH_DIR="$CONFIG_DIR/snmp-gateway"
USERS_FILE="$AUTH_DIR/users.json"
AUDIT_FILE="$AUTH_DIR/audit.log"

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

audit_log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$AUDIT_FILE"
}

# Initialize SNMP authentication system
init_auth() {
    log "Initializing SNMP Gateway authentication system..."

    # Create directory structure
    mkdir -p "$AUTH_DIR"

    # Initialize users file if it doesn't exist
    if [ ! -f "$USERS_FILE" ]; then
        echo '{"users": {}}' > "$USERS_FILE"
        success "Created users database: $USERS_FILE"
    fi

    # Create audit log
    touch "$AUDIT_FILE"
    audit_log "SNMP authentication system initialized"

    success "SNMP Gateway authentication system initialized"
    echo ""
    echo "Users will be stored in: $USERS_FILE"
    echo "Audit log: $AUDIT_FILE"
}

# Generate secure random password (8 chars, lowercase alphanumeric)
generate_password() {
    # Use /dev/urandom for random lowercase alphanumeric (a-z, 0-9)
    tr -dc 'a-z0-9' < /dev/urandom | head -c 8
}

# Validate username format
validate_username() {
    local username="$1"

    # Check if empty
    if [ -z "$username" ]; then
        error "Username cannot be empty"
        return 1
    fi

    # Check length (3-32 characters)
    if [ ${#username} -lt 3 ] || [ ${#username} -gt 32 ]; then
        error "Username must be 3-32 characters long"
        return 1
    fi

    # Check format: lowercase alphanumeric and hyphens only
    if ! [[ "$username" =~ ^[a-z0-9-]+$ ]]; then
        error "Username must contain only lowercase letters, numbers, and hyphens"
        return 1
    fi

    # Cannot start or end with hyphen
    if [[ "$username" =~ ^- ]] || [[ "$username" =~ -$ ]]; then
        error "Username cannot start or end with a hyphen"
        return 1
    fi

    return 0
}

# Validate password format
validate_password() {
    local password="$1"

    # Check if empty
    if [ -z "$password" ]; then
        error "Password cannot be empty"
        return 1
    fi

    # Check length (minimum 8 characters)
    if [ ${#password} -lt 8 ]; then
        error "Password must be at least 8 characters long"
        return 1
    fi

    return 0
}

# Create SNMPv3 user account
create_user() {
    local username="$1"
    local security_level="${2:-noAuthNoPriv}"  # Default to noAuthNoPriv
    local password="$3"

    # Validate username
    if ! validate_username "$username"; then
        exit 1
    fi

    # Initialize if needed
    [ ! -f "$USERS_FILE" ] && init_auth

    # Check if user exists
    if jq -e ".users.\"$username\"" "$USERS_FILE" > /dev/null 2>&1; then
        error "User '$username' already exists"
        exit 1
    fi

    # Normalize username (lowercase, trimmed)
    username=$(echo "$username" | tr '[:upper:]' '[:lower:]' | xargs)

    # Client name = username (direct mapping)
    local client="$username"

    # Create user record based on security level
    local user_record
    case "$security_level" in
        noAuthNoPriv)
            # Username-only authentication (maximum compatibility)
            user_record=$(jq -n \
                --arg username "$username" \
                --arg client "$client" \
                --arg created "$(date -Iseconds)" \
                --arg status "active" \
                --arg security_level "noAuthNoPriv" \
                '{
                    username: $username,
                    security_level: $security_level,
                    client: $client,
                    created: $created,
                    status: $status,
                    last_used: null
                }')

            audit_log "User created: $username (security=noAuthNoPriv, client: $client)"

            # Add user to database
            jq ".users.\"$username\" = $user_record" "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

            success "SNMPv3 user '$username' created successfully"
            echo ""
            echo "Username:       $username"
            echo "Security Level: noAuthNoPriv (username-only)"
            echo "Client:         $client"
            echo "Status:         active"
            echo ""
            echo "Device Configuration (SNMPv3):"
            echo "--------------------"
            echo "username:         $username"
            echo "security level:   noAuthNoPriv"
            echo "trap destination: alert.d-t.pw:162"
            echo ""
            echo "Compatible with most vendor SNMP implementations"
            ;;

        authNoPriv)
            # Authentication without encryption
            if [ -z "$password" ]; then
                password=$(generate_password)
                log "Generated password: $password"
            else
                if ! validate_password "$password"; then
                    exit 1
                fi
            fi

            user_record=$(jq -n \
                --arg username "$username" \
                --arg auth_protocol "SHA" \
                --arg auth_key "$password" \
                --arg client "$client" \
                --arg created "$(date -Iseconds)" \
                --arg status "active" \
                --arg security_level "authNoPriv" \
                '{
                    username: $username,
                    security_level: $security_level,
                    auth_protocol: $auth_protocol,
                    auth_key: $auth_key,
                    client: $client,
                    created: $created,
                    status: $status,
                    last_used: null
                }')

            audit_log "User created: $username (security=authNoPriv, client: $client)"

            # Add user to database
            jq ".users.\"$username\" = $user_record" "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

            success "SNMPv3 user '$username' created successfully"
            echo ""
            echo "Username:       $username"
            echo "Password:       $password"
            echo "Security Level: authNoPriv (authenticated, no encryption)"
            echo "Auth Protocol:  SHA"
            echo "Client:         $client"
            echo "Status:         active"
            echo ""
            echo "Device Configuration (SNMPv3):"
            echo "--------------------"
            echo "username:         $username"
            echo "password:         $password"
            echo "security level:   authNoPriv"
            echo "auth protocol:    SHA/SHA-1"
            echo "trap destination: alert.d-t.pw:162"
            ;;

        authPriv)
            # Full authentication and encryption (legacy/high security)
            if [ -z "$password" ]; then
                password=$(generate_password)
                log "Generated password: $password"
            else
                if ! validate_password "$password"; then
                    exit 1
                fi
            fi

            user_record=$(jq -n \
                --arg username "$username" \
                --arg auth_protocol "SHA" \
                --arg auth_key "$password" \
                --arg priv_protocol "AES" \
                --arg priv_key "$password" \
                --arg client "$client" \
                --arg created "$(date -Iseconds)" \
                --arg status "active" \
                --arg security_level "authPriv" \
                '{
                    username: $username,
                    security_level: $security_level,
                    auth_protocol: $auth_protocol,
                    auth_key: $auth_key,
                    priv_protocol: $priv_protocol,
                    priv_key: $priv_key,
                    client: $client,
                    created: $created,
                    status: $status,
                    last_used: null
                }')

            audit_log "User created: $username (security=authPriv, client: $client)"

            # Add user to database
            jq ".users.\"$username\" = $user_record" "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

            success "SNMPv3 user '$username' created successfully"
            echo ""
            echo "Username:       $username"
            echo "Password:       $password"
            echo "Security Level: authPriv (authenticated + encrypted)"
            echo "Auth Protocol:  SHA"
            echo "Priv Protocol:  AES"
            echo "Client:         $client"
            echo "Status:         active"
            echo ""
            echo "Device Configuration (SNMPv3):"
            echo "--------------------"
            echo "username:         $username"
            echo "password:         $password"
            echo "security level:   authPriv"
            echo "auth protocol:    SHA/SHA-1"
            echo "priv protocol:    AES/AES-128"
            echo "trap destination: alert.d-t.pw:162"
            ;;

        *)
            error "Invalid security level: $security_level. Must be noAuthNoPriv, authNoPriv, or authPriv"
            exit 1
            ;;
    esac
}

# List all users
list_users() {
    if [ ! -f "$USERS_FILE" ]; then
        warning "No users database found. Run 'init' first."
        return
    fi

    echo "SNMPv3 Users:"
    echo "============="
    echo ""
    printf "%-20s %-15s %-15s %-20s %-20s\n" "USERNAME" "CLIENT" "STATUS" "CREATED" "LAST USED"
    printf "%-20s %-15s %-15s %-20s %-20s\n" "--------" "------" "------" "-------" "---------"

    jq -r '
        .users | to_entries[] |
        "\(.key)\t\(.value.client)\t\(.value.status)\t\(.value.created)\t\(.value.last_used // "never")"
    ' "$USERS_FILE" | while IFS=$'\t' read -r username client status created last_used; do
        local status_color=""
        case "$status" in
            active) status_color="$GREEN" ;;
            revoked) status_color="$RED" ;;
        esac

        printf "%-20s %-15s %s%-15s${NC} %-20s %-20s\n" \
            "$username" "$client" "$status_color" "$status" "$created" "$last_used"
    done
}

# Show user details
show_user() {
    local username="$1"

    if [ -z "$username" ]; then
        error "Username is required"
        exit 1
    fi

    if [ ! -f "$USERS_FILE" ]; then
        error "No users database found"
        exit 1
    fi

    local user_data
    user_data=$(jq -e ".users.\"$username\"" "$USERS_FILE" 2>/dev/null)
    if [ $? -ne 0 ]; then
        error "User '$username' not found"
        exit 1
    fi

    echo "SNMPv3 User Details: $username"
    echo "=============================="
    echo ""
    echo "$user_data" | jq -r '
        "Username:       \(.username)",
        "Client:         \(.client)",
        "Status:         \(.status)",
        "Auth Protocol:  \(.auth_protocol)",
        "Priv Protocol:  \(.priv_protocol)",
        "Created:        \(.created)",
        "Last Used:      \(.last_used // "Never")"
    '
    echo ""
    echo "Password (unified auth/priv):"
    echo "$user_data" | jq -r '"  \(.auth_key)"'
}

# Reset user password
reset_password() {
    local username="$1"
    local new_password="$2"

    if [ -z "$username" ]; then
        error "Username is required"
        exit 1
    fi

    if [ ! -f "$USERS_FILE" ]; then
        error "No users database found"
        exit 1
    fi

    # Check if user exists
    if ! jq -e ".users.\"$username\"" "$USERS_FILE" > /dev/null 2>&1; then
        error "User '$username' not found"
        exit 1
    fi

    # Generate password if not provided
    if [ -z "$new_password" ]; then
        new_password=$(generate_password)
        log "Generated new password: $new_password"
    else
        # Validate provided password
        if ! validate_password "$new_password"; then
            exit 1
        fi
    fi

    # Update user record (unified auth: auth_key = priv_key)
    jq ".users.\"$username\".auth_key = \"$new_password\" |
        .users.\"$username\".priv_key = \"$new_password\"" \
        "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

    audit_log "Password reset for user: $username"
    success "Password reset for user '$username'"
    echo ""
    echo "New password: $new_password"
    echo ""
    echo "Update device configuration:"
    echo "readonlypassword: $new_password"
}

# Revoke user access
revoke_user() {
    local username="$1"

    if [ -z "$username" ]; then
        error "Username is required"
        exit 1
    fi

    if [ ! -f "$USERS_FILE" ]; then
        error "No users database found"
        exit 1
    fi

    # Check if user exists
    if ! jq -e ".users.\"$username\"" "$USERS_FILE" > /dev/null 2>&1; then
        error "User '$username' not found"
        exit 1
    fi

    # Update user status
    jq ".users.\"$username\".status = \"revoked\"" \
        "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

    audit_log "User revoked: $username"
    warning "User '$username' access revoked"
    echo ""
    echo "The user can no longer authenticate."
    echo "To restore access, use: reset-password $username"
}

# Restore revoked user
restore_user() {
    local username="$1"

    if [ -z "$username" ]; then
        error "Username is required"
        exit 1
    fi

    if [ ! -f "$USERS_FILE" ]; then
        error "No users database found"
        exit 1
    fi

    # Check if user exists
    if ! jq -e ".users.\"$username\"" "$USERS_FILE" > /dev/null 2>&1; then
        error "User '$username' not found"
        exit 1
    fi

    # Update user status
    jq ".users.\"$username\".status = \"active\"" \
        "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

    audit_log "User restored: $username"
    success "User '$username' access restored"
}

# Delete user permanently
delete_user() {
    local username="$1"

    if [ -z "$username" ]; then
        error "Username is required"
        exit 1
    fi

    if [ ! -f "$USERS_FILE" ]; then
        error "No users database found"
        exit 1
    fi

    # Check if user exists
    if ! jq -e ".users.\"$username\"" "$USERS_FILE" > /dev/null 2>&1; then
        error "User '$username' not found"
        exit 1
    fi

    # Confirm deletion
    read -p "Are you sure you want to permanently delete user '$username'? (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        echo "Deletion cancelled."
        exit 0
    fi

    # Delete user from database
    jq "del(.users.\"$username\")" \
        "$USERS_FILE" > "$USERS_FILE.tmp" && mv "$USERS_FILE.tmp" "$USERS_FILE"

    audit_log "User deleted: $username"
    warning "User '$username' permanently deleted"
}

# Show usage
show_usage() {
    echo "SNMPv3 Gateway Authentication Manager"
    echo "===================================="
    echo ""
    echo "Usage: $0 <command> [arguments]"
    echo ""
    echo "Commands:"
    echo "  init                           Initialize SNMP authentication system"
    echo ""
    echo "User Management:"
    echo "  create-user <username> [password]"
    echo "                                 Create new SNMPv3 user"
    echo "  list                           List all users"
    echo "  show <username>                Show user details (including password)"
    echo "  reset-password <username> [password]"
    echo "                                 Reset user password"
    echo "  revoke <username>              Revoke user access"
    echo "  restore <username>             Restore revoked user"
    echo "  delete <username>              Permanently delete user"
    echo ""
    echo "Username Rules:"
    echo "  - 3-32 characters"
    echo "  - Lowercase letters, numbers, hyphens only"
    echo "  - Cannot start/end with hyphen"
    echo "  - Username becomes the client name directly"
    echo ""
    echo "Password:"
    echo "  - Auto-generated: 8 chars, lowercase alphanumeric (e.g., 'a7k9m3p2')"
    echo "  - Unified: Same password for auth and priv (authPriv mode)"
    echo "  - Protocol: SHA authentication + AES encryption"
    echo ""
    echo "Examples:"
    echo "  $0 init                        # Initialize system"
    echo "  $0 create-user acme-co        # Create user 'acme-co' (auto-gen password)"
    echo "  $0 create-user office abc12345 # Create user with specific password"
    echo "  $0 list                        # List all users"
    echo "  $0 show acme-co               # Show user details"
    echo "  $0 reset-password acme-co     # Reset password (auto-gen)"
    echo "  $0 revoke acme-co             # Revoke access"
    echo "  $0 restore acme-co            # Restore access"
    echo "  $0 delete acme-co             # Permanently delete"
    echo ""
    echo "Configuration:"
    echo "  Users file: $USERS_FILE"
    echo "  Audit log:  $AUDIT_FILE"
}

# Main command handler
case "${1:-}" in
    init)
        init_auth
        ;;
    create-user)
        create_user "$2" "$3"
        ;;
    list)
        list_users
        ;;
    show)
        show_user "$2"
        ;;
    reset-password)
        reset_password "$2" "$3"
        ;;
    revoke)
        revoke_user "$2"
        ;;
    restore)
        restore_user "$2"
        ;;
    delete)
        delete_user "$2"
        ;;
    *)
        show_usage
        exit 1
        ;;
esac
