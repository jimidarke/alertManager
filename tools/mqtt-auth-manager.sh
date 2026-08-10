#!/bin/bash

# MQTT User Authentication Manager
# Manages username/password authentication for Mosquitto MQTT broker
# Uses mosquitto_passwd utility (host or Docker container)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
PASSWORD_FILE="$PROJECT_ROOT/data/mosquitto/config/passwd"
TEMP_PASSWORD_FILE="${PASSWORD_FILE}.tmp"
CONTAINER_NAME="alert-mqtt-broker"
USE_DOCKER=false

# Colors for output (using $'...' syntax to interpret escape sequences)
RED=$'\033[0;31m'
GREEN=$'\033[0;32m'
YELLOW=$'\033[1;33m'
BLUE=$'\033[0;34m'
NC=$'\033[0m' # No Color

# Check if mosquitto_passwd is available (host or Docker)
check_mosquitto_passwd() {
    if command -v mosquitto_passwd &> /dev/null; then
        # mosquitto_passwd available on host
        USE_DOCKER=false
        return 0
    fi

    # Check if Docker container is available
    if command -v docker &> /dev/null; then
        if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
            # Use Docker container
            USE_DOCKER=true
            echo -e "${YELLOW}Note: Using mosquitto_passwd from Docker container${NC}"
            return 0
        fi
    fi

    # Neither host nor Docker available
    echo -e "${RED}Error: mosquitto_passwd not found${NC}"
    echo ""
    echo "Options:"
    echo "  1. Install mosquitto-clients on host:"
    echo "     Ubuntu/Debian: sudo apt-get install mosquitto-clients"
    echo "     macOS: brew install mosquitto"
    echo ""
    echo "  2. Start the MQTT broker container:"
    echo "     docker-compose up -d alert-mqtt-broker"
    exit 1
}

# Wrapper function to run mosquitto_passwd (host or Docker)
run_mosquitto_passwd() {
    if [[ "$USE_DOCKER" == "true" ]]; then
        # Run in Docker container
        # Convert host path to container path
        local container_path="/mosquitto/config/passwd"

        # For interactive mode (no -b flag), we need to handle stdin
        if [[ "$1" == "-b" ]]; then
            # Non-interactive mode: -b <file> <username> <password>
            docker exec "$CONTAINER_NAME" mosquitto_passwd -b "$container_path" "$3" "$4"
        else
            # Interactive mode: <file> <username>
            docker exec -i "$CONTAINER_NAME" mosquitto_passwd "$container_path" "$2"
        fi
    else
        # Run on host
        mosquitto_passwd "$@"
    fi
}

# Ensure password file exists
ensure_password_file() {
    if [[ ! -f "$PASSWORD_FILE" ]]; then
        echo -e "${YELLOW}Creating new password file: $PASSWORD_FILE${NC}"
        touch "$PASSWORD_FILE"
        chmod 600 "$PASSWORD_FILE"
    fi
}

# List all users
list_users() {
    ensure_password_file

    if [[ ! -s "$PASSWORD_FILE" ]]; then
        echo -e "${YELLOW}No users found in password file${NC}"
        return
    fi

    echo -e "${BLUE}=== MQTT Users ===${NC}"
    echo ""

    while IFS=: read -r username hash; do
        echo -e "${GREEN}✓${NC} $username"
    done < "$PASSWORD_FILE"

    echo ""
    echo "Total users: $(wc -l < "$PASSWORD_FILE")"
}

# Add or update a user
add_user() {
    local username="$1"
    local password="$2"

    if [[ -z "$username" ]]; then
        echo -e "${RED}Error: Username required${NC}"
        echo "Usage: $0 add <username> [password]"
        exit 1
    fi

    # Validate username (alphanumeric, dash, underscore only)
    if [[ ! "$username" =~ ^[a-zA-Z0-9_-]+$ ]]; then
        echo -e "${RED}Error: Username must contain only letters, numbers, dash, and underscore${NC}"
        exit 1
    fi

    ensure_password_file

    # Check if user exists
    if grep -q "^${username}:" "$PASSWORD_FILE" 2>/dev/null; then
        echo -e "${YELLOW}User '$username' already exists. Updating password...${NC}"
        action="updated"
    else
        echo -e "${GREEN}Creating new user '$username'...${NC}"
        action="created"
    fi

    # Add/update user with password
    if [[ -n "$password" ]]; then
        # Non-interactive mode with provided password
        run_mosquitto_passwd -b "$PASSWORD_FILE" "$username" "$password"
    else
        # Interactive mode - prompts for password
        run_mosquitto_passwd "$PASSWORD_FILE" "$username"
    fi

    echo -e "${GREEN}✓ User '$username' ${action} successfully${NC}"
    echo ""
    echo -e "${YELLOW}Important: Restart the MQTT broker to apply changes:${NC}"
    echo "  docker-compose restart alert-mqtt-broker"
}

# Delete a user
delete_user() {
    local username="$1"

    if [[ -z "$username" ]]; then
        echo -e "${RED}Error: Username required${NC}"
        echo "Usage: $0 delete <username>"
        exit 1
    fi

    ensure_password_file

    if ! grep -q "^${username}:" "$PASSWORD_FILE" 2>/dev/null; then
        echo -e "${RED}Error: User '$username' not found${NC}"
        exit 1
    fi

    # Remove user from password file
    grep -v "^${username}:" "$PASSWORD_FILE" > "$TEMP_PASSWORD_FILE" || true
    mv "$TEMP_PASSWORD_FILE" "$PASSWORD_FILE"
    chmod 600 "$PASSWORD_FILE"

    echo -e "${GREEN}✓ User '$username' deleted successfully${NC}"
    echo ""
    echo -e "${YELLOW}Important: Restart the MQTT broker to apply changes:${NC}"
    echo "  docker-compose restart alert-mqtt-broker"
}

# Change user password
change_password() {
    local username="$1"
    local password="$2"

    if [[ -z "$username" ]]; then
        echo -e "${RED}Error: Username required${NC}"
        echo "Usage: $0 passwd <username> [password]"
        exit 1
    fi

    ensure_password_file

    if ! grep -q "^${username}:" "$PASSWORD_FILE" 2>/dev/null; then
        echo -e "${RED}Error: User '$username' not found${NC}"
        exit 1
    fi

    echo -e "${YELLOW}Changing password for user '$username'...${NC}"

    # Update password
    if [[ -n "$password" ]]; then
        # Non-interactive mode with provided password
        run_mosquitto_passwd -b "$PASSWORD_FILE" "$username" "$password"
    else
        # Interactive mode - prompts for password
        run_mosquitto_passwd "$PASSWORD_FILE" "$username"
    fi

    echo -e "${GREEN}✓ Password changed successfully${NC}"
    echo ""
    echo -e "${YELLOW}Important: Restart the MQTT broker to apply changes:${NC}"
    echo "  docker-compose restart alert-mqtt-broker"
}

# Show help
show_help() {
    cat << EOF
${BLUE}MQTT User Authentication Manager${NC}

Manages username/password authentication for Mosquitto MQTT broker.

${YELLOW}Usage:${NC}
    $0 <command> [options]

${YELLOW}Commands:${NC}
    ${GREEN}list${NC}                    List all MQTT users
    ${GREEN}add${NC} <username> [pass]   Add new user (prompts for password if not provided)
    ${GREEN}delete${NC} <username>       Delete existing user
    ${GREEN}passwd${NC} <username> [pass] Change user password
    ${GREEN}help${NC}                    Show this help message

${YELLOW}Examples:${NC}
    # List all users
    $0 list

    # Add user with interactive password prompt
    $0 add esp-sensor

    # Add user with password on command line (less secure)
    $0 add esp-sensor MySecurePassword123

    # Change password (interactive)
    $0 passwd esp-sensor

    # Delete user
    $0 delete esp-sensor

${YELLOW}Security Notes:${NC}
    • Passwords are hashed using PBKDF2-SHA512
    • Password file is stored at: $PASSWORD_FILE
    • File permissions are automatically set to 600 (owner read/write only)
    • Restart the MQTT broker after making changes
    • Script works on host or via Docker container (auto-detected)

${YELLOW}MQTT Connection:${NC}
    Host: alert.d-t.pw
    Port: 42690 (TLS) or 1883 (plain, internal only)

    Example connection (with mosquitto_pub):
    mosquitto_pub -h alert.d-t.pw -p 42690 \\
        -u esp-sensor -P MySecurePassword123 \\
        --cafile data/certs/ca/ca.crt \\
        -t alerts/input -m '{"test": "message"}'

${YELLOW}Files:${NC}
    Password file: $PASSWORD_FILE
    Mosquitto config: $PROJECT_ROOT/data/mosquitto/config/mosquitto.conf
    ACL config: $PROJECT_ROOT/data/mosquitto/config/acl.conf

EOF
}

# Main command handler
main() {
    check_mosquitto_passwd

    local command="${1:-help}"

    case "$command" in
        list|ls)
            list_users
            ;;
        add|create)
            add_user "$2" "$3"
            ;;
        delete|del|rm)
            delete_user "$2"
            ;;
        passwd|password|pw)
            change_password "$2" "$3"
            ;;
        help|-h|--help)
            show_help
            ;;
        *)
            echo -e "${RED}Error: Unknown command '$command'${NC}"
            echo "Run '$0 help' for usage information"
            exit 1
            ;;
    esac
}

main "$@"
