#!/bin/bash

# SNMPv3 Trap Testing Tool
# Sends test SNMPv3 traps to the Alert Manager SNMP Gateway

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Default values
TRAP_DESTINATION="alert.d-t.pw"
TRAP_PORT="162"
AUTH_PROTOCOL="SHA"
PRIV_PROTOCOL="AES"
SECURITY_LEVEL="authPriv"

# Show usage
show_usage() {
    echo "SNMPv3 Trap Testing Tool"
    echo "========================"
    echo ""
    echo "Usage: $0 <username> <password> [trap-type] [options]"
    echo ""
    echo "Required Arguments:"
    echo "  username              SNMPv3 username (must exist in Alert Manager)"
    echo "  password              SNMPv3 password (auth and priv use same password)"
    echo ""
    echo "Optional Arguments:"
    echo "  trap-type             Trap type to send (default: linkDown)"
    echo "                        Options: linkDown, linkUp, coldStart, warmStart, authFailure, custom"
    echo ""
    echo "Options:"
    echo "  -d, --destination     Trap destination (default: alert.d-t.pw)"
    echo "  -p, --port            Trap port (default: 162)"
    echo "  -h, --help            Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 acme-co a7k9m3p2"
    echo "                        # Send linkDown trap (default)"
    echo ""
    echo "  $0 acme-co a7k9m3p2 linkUp"
    echo "                        # Send linkUp trap"
    echo ""
    echo "  $0 acme-co a7k9m3p2 coldStart"
    echo "                        # Send coldStart trap (device reboot)"
    echo ""
    echo "  $0 office-network x5k8m2p9 custom"
    echo "                        # Send custom trap with sample data"
    echo ""
    echo "  $0 acme-co a7k9m3p2 linkDown -d 192.168.1.100"
    echo "                        # Send to different destination"
    echo ""
    echo "Requirements:"
    echo "  - snmptrap command (from net-snmp-utils package)"
    echo "  - Install: sudo apt-get install snmp (Debian/Ubuntu)"
    echo "  -          sudo yum install net-snmp-utils (RHEL/CentOS)"
    echo "  -          brew install net-snmp (macOS)"
    echo ""
    echo "Note: This script uses numeric OIDs (no MIB files required for portability)"
}

# Check if snmptrap is installed
check_snmptrap() {
    if ! command -v snmptrap &> /dev/null; then
        echo -e "${RED}Error: snmptrap command not found${NC}" >&2
        echo ""
        echo "Please install net-snmp-utils:"
        echo "  Debian/Ubuntu: sudo apt-get install snmp snmp-mibs-downloader"
        echo "  RHEL/CentOS:   sudo yum install net-snmp-utils"
        echo "  macOS:         brew install net-snmp"
        exit 1
    fi
}

# Send SNMP trap
send_trap() {
    local username="$1"
    local password="$2"
    local trap_oid="$3"
    local trap_name="$4"
    shift 4
    local varbinds=("$@")

    echo -e "${BLUE}Sending SNMPv3 Trap${NC}"
    echo "===================="
    echo "Destination:  ${TRAP_DESTINATION}:${TRAP_PORT}"
    echo "Username:     ${username}"
    echo "Password:     ${password}"
    echo "Auth:         ${AUTH_PROTOCOL}"
    echo "Priv:         ${PRIV_PROTOCOL}"
    echo "Security:     ${SECURITY_LEVEL}"
    echo "Trap:         ${trap_name} (${trap_oid})"
    echo ""

    # Build snmptrap command
    local cmd=(
        snmptrap
        -v 3
        -l "${SECURITY_LEVEL}"
        -u "${username}"
        -a "${AUTH_PROTOCOL}"
        -A "${password}"
        -x "${PRIV_PROTOCOL}"
        -X "${password}"
        "${TRAP_DESTINATION}:${TRAP_PORT}"
        ''
        "${trap_oid}"
    )

    # Add varbinds if provided
    if [ ${#varbinds[@]} -gt 0 ]; then
        cmd+=("${varbinds[@]}")
        echo "Varbinds:"
        for varbind in "${varbinds[@]}"; do
            echo "  ${varbind}"
        done
        echo ""
    fi

    # Execute command
    echo -e "${YELLOW}Executing...${NC}"
    echo ""
    echo "Full command (copy-paste safe):"
    # Use printf with proper quoting for copy-paste
    printf '%q ' "${cmd[@]}"
    echo ""
    echo ""

    # Run with verbose error output
    if output=$("${cmd[@]}" 2>&1); then
        echo "Command output:"
        echo "$output"
        echo ""
        echo -e "${GREEN}✓ Trap sent successfully${NC}"
        echo ""
        echo "Check gateway logs:"
        echo "  docker logs -f alert-snmp-gateway | grep '${username}'"
        echo ""
        echo "Check discovery logs:"
        echo "  tail -f data/logs/snmp-discovery/snmpFULL.*.jsonl | jq '.snmp_username, .assigned_client'"
        echo ""
        echo "Check alert logs:"
        echo "  tail -f data/logs/file/${username}/$(date -u +%Y-%m-%d).log"
        return 0
    else
        echo ""
        echo -e "${RED}✗ Failed to send trap${NC}"
        echo ""
        echo "Common issues:"
        echo "  - Wrong username or password"
        echo "  - User not configured in Alert Manager (use: ./tools/snmp-auth-manager.sh list)"
        echo "  - Network connectivity issue"
        echo "  - SNMP gateway not running (check: docker ps | grep snmp-gateway)"
        return 1
    fi
}

# Main script
main() {
    # Check for help flag
    if [ "$1" = "-h" ] || [ "$1" = "--help" ] || [ $# -eq 0 ]; then
        show_usage
        exit 0
    fi

    # Check if snmptrap is installed
    check_snmptrap

    # Parse required arguments
    if [ $# -lt 2 ]; then
        echo -e "${RED}Error: Missing required arguments${NC}" >&2
        echo ""
        show_usage
        exit 1
    fi

    local username="$1"
    local password="$2"
    local trap_type="${3:-linkDown}"
    shift 2
    [ $# -gt 0 ] && shift  # Remove trap_type if provided

    # Parse optional arguments
    while [ $# -gt 0 ]; do
        case "$1" in
            -d|--destination)
                TRAP_DESTINATION="$2"
                shift 2
                ;;
            -p|--port)
                TRAP_PORT="$2"
                shift 2
                ;;
            *)
                echo -e "${RED}Error: Unknown option: $1${NC}" >&2
                exit 1
                ;;
        esac
    done

    # Define trap OIDs and varbinds based on trap type
    case "$trap_type" in
        linkDown)
            # Standard linkDown trap (interface went down)
            # Using numeric OIDs for portability (no MIB files required)
            send_trap "$username" "$password" \
                "1.3.6.1.6.3.1.1.5.3" \
                "linkDown" \
                "1.3.6.1.2.1.2.2.1.1.2" i "2" \
                "1.3.6.1.2.1.2.2.1.2.2" s "eth0" \
                "1.3.6.1.2.1.2.2.1.3.2" i "6" \
                "1.3.6.1.2.1.2.2.1.7.2" i "1" \
                "1.3.6.1.2.1.2.2.1.8.2" i "2"
            ;;

        linkUp)
            # Standard linkUp trap (interface came up)
            # Using numeric OIDs for portability (no MIB files required)
            send_trap "$username" "$password" \
                "1.3.6.1.6.3.1.1.5.4" \
                "linkUp" \
                "1.3.6.1.2.1.2.2.1.1.2" i "2" \
                "1.3.6.1.2.1.2.2.1.2.2" s "eth0" \
                "1.3.6.1.2.1.2.2.1.3.2" i "6" \
                "1.3.6.1.2.1.2.2.1.7.2" i "1" \
                "1.3.6.1.2.1.2.2.1.8.2" i "1"
            ;;

        coldStart)
            # Device restarted (power cycle)
            send_trap "$username" "$password" \
                "1.3.6.1.6.3.1.1.5.1" \
                "coldStart"
            ;;

        warmStart)
            # SNMP agent restarted (no power cycle)
            send_trap "$username" "$password" \
                "1.3.6.1.6.3.1.1.5.2" \
                "warmStart"
            ;;

        authFailure)
            # SNMP authentication failure
            send_trap "$username" "$password" \
                "1.3.6.1.6.3.1.1.5.5" \
                "authenticationFailure"
            ;;

        custom)
            # Custom trap with sample varbinds
            # Using numeric OIDs for portability (no MIB files required)
            send_trap "$username" "$password" \
                "1.3.6.1.4.1.99999.1.0.1" \
                "customTestTrap" \
                "1.3.6.1.2.1.1.5.0" s "test-device-01" \
                "1.3.6.1.2.1.1.6.0" s "Server Room A" \
                "1.3.6.1.2.1.1.4.0" s "admin@example.com" \
                "1.3.6.1.4.1.99999.1.1.1" i "42" \
                "1.3.6.1.4.1.99999.1.1.2" s "Test alert from snmp testing tool"
            ;;

        *)
            echo -e "${RED}Error: Unknown trap type: ${trap_type}${NC}" >&2
            echo ""
            echo "Valid trap types: linkDown, linkUp, coldStart, warmStart, authFailure, custom"
            exit 1
            ;;
    esac
}

# Run main function
main "$@"
