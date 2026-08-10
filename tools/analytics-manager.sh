#!/bin/bash

set -e

# Configuration - Detect if running in container or on host
if [ -d "/data/config" ]; then
    # Running inside container
    CONFIG_DIR="/data/config"
    CONFIG_FILE="/data/config/docker.json"
    IN_CONTAINER=true
else
    # Running on host - use relative path from script location
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
    CONFIG_DIR="$PROJECT_ROOT/data/config"
    CONFIG_FILE="$CONFIG_DIR/docker.json"
    IN_CONTAINER=false
fi

CONTAINER_NAME="alert-analytics-engine"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
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

info() {
    echo -e "${CYAN}[INFO]${NC} $1"
}

# Check if jq is available
check_jq() {
    if ! command -v jq &> /dev/null; then
        error "jq is required but not installed. Install with: apt-get install jq"
        exit 1
    fi
}

# Check if config file exists
check_config() {
    if [ ! -f "$CONFIG_FILE" ]; then
        error "Config file not found: $CONFIG_FILE"
        exit 1
    fi
}

# Check if analytics container is running
check_container() {
    if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        error "Analytics container '$CONTAINER_NAME' is not running"
        echo ""
        echo "Start it with:"
        echo "  docker-compose --profile analytics up -d"
        exit 1
    fi
}

# List all analyzers
list_analyzers() {
    check_jq
    check_config

    echo ""
    echo -e "${BOLD}Available Analyzers${NC}"
    echo "==================="
    echo ""

    local analyzers
    analyzers=$(jq -r '.analytics.analyzers // {} | keys[]' "$CONFIG_FILE" 2>/dev/null)

    if [ -z "$analyzers" ]; then
        warning "No analyzers configured in $CONFIG_FILE"
        exit 0
    fi

    local idx=1
    while IFS= read -r name; do
        local enabled type client schedule_freq schedule_day schedule_time
        enabled=$(jq -r ".analytics.analyzers[\"$name\"].enabled // false" "$CONFIG_FILE")
        type=$(jq -r ".analytics.analyzers[\"$name\"].type // \"unknown\"" "$CONFIG_FILE")
        client=$(jq -r ".analytics.analyzers[\"$name\"].client // \"\"" "$CONFIG_FILE")
        schedule_freq=$(jq -r ".analytics.analyzers[\"$name\"].schedule.frequency // \"\"" "$CONFIG_FILE")
        schedule_day=$(jq -r ".analytics.analyzers[\"$name\"].schedule.day_of_week // \"\"" "$CONFIG_FILE")
        schedule_time=$(jq -r ".analytics.analyzers[\"$name\"].schedule.time // \"\"" "$CONFIG_FILE")

        # Status indicator
        local status_color status_text
        if [ "$enabled" = "true" ]; then
            status_color="${GREEN}"
            status_text="enabled"
        else
            status_color="${RED}"
            status_text="disabled"
        fi

        # Build schedule string
        local schedule_str=""
        if [ -n "$schedule_freq" ]; then
            schedule_str="$schedule_freq"
            [ -n "$schedule_day" ] && schedule_str="$schedule_str ($schedule_day)"
            [ -n "$schedule_time" ] && schedule_str="$schedule_str @ $schedule_time"
        fi

        echo -e "  ${BOLD}$idx. $name${NC}"
        echo -e "     Type: ${CYAN}$type${NC}"
        echo -e "     Status: ${status_color}$status_text${NC}"
        [ -n "$client" ] && echo -e "     Client: $client"
        [ -n "$schedule_str" ] && echo -e "     Schedule: $schedule_str"
        echo ""

        ((idx++))
    done <<< "$analyzers"
}

# Run a specific analyzer
run_analyzer() {
    local analyzer_name="$1"

    check_jq
    check_config
    check_container

    # Validate analyzer exists
    local exists
    exists=$(jq -r ".analytics.analyzers[\"$analyzer_name\"] // \"null\"" "$CONFIG_FILE")

    if [ "$exists" = "null" ]; then
        error "Analyzer '$analyzer_name' not found in configuration"
        echo ""
        echo "Available analyzers:"
        jq -r '.analytics.analyzers // {} | keys[]' "$CONFIG_FILE" | sed 's/^/  - /'
        exit 1
    fi

    log "Running analyzer: $analyzer_name"
    echo ""

    docker exec "$CONTAINER_NAME" python3 src/analytics_engine.py \
        --config /data/config/docker.json \
        --run-once "$analyzer_name"
}

# Run all enabled analyzers
run_all() {
    check_jq
    check_config
    check_container

    log "Running all enabled analyzers..."
    echo ""

    local analyzers
    analyzers=$(jq -r '.analytics.analyzers | to_entries[] | select(.value.enabled == true) | .key' "$CONFIG_FILE" 2>/dev/null)

    if [ -z "$analyzers" ]; then
        warning "No enabled analyzers found"
        exit 0
    fi

    while IFS= read -r name; do
        echo -e "${BOLD}Running: $name${NC}"
        echo "---"
        docker exec "$CONTAINER_NAME" python3 src/analytics_engine.py \
            --config /data/config/docker.json \
            --run-once "$name"
        echo ""
    done <<< "$analyzers"

    success "All analyzers completed"
}

# Interactive mode - select analyzer to run
interactive() {
    check_jq
    check_config

    echo ""
    echo -e "${BOLD}Analytics Manager - Interactive Mode${NC}"
    echo "====================================="
    echo ""

    # Get analyzer list
    local analyzers
    mapfile -t analyzers < <(jq -r '.analytics.analyzers // {} | keys[]' "$CONFIG_FILE" 2>/dev/null)

    if [ ${#analyzers[@]} -eq 0 ]; then
        warning "No analyzers configured"
        exit 0
    fi

    # Display menu
    echo "Select an analyzer to run (dry run):"
    echo ""

    local idx=1
    for name in "${analyzers[@]}"; do
        local enabled type
        enabled=$(jq -r ".analytics.analyzers[\"$name\"].enabled // false" "$CONFIG_FILE")
        type=$(jq -r ".analytics.analyzers[\"$name\"].type // \"unknown\"" "$CONFIG_FILE")

        local status_indicator
        if [ "$enabled" = "true" ]; then
            status_indicator="${GREEN}●${NC}"
        else
            status_indicator="${RED}○${NC}"
        fi

        echo -e "  ${BOLD}$idx)${NC} $status_indicator $name ${CYAN}[$type]${NC}"
        ((idx++))
    done

    echo ""
    echo -e "  ${BOLD}a)${NC} Run all enabled analyzers"
    echo -e "  ${BOLD}q)${NC} Quit"
    echo ""

    read -r -p "Enter selection: " choice

    case "$choice" in
        q|Q)
            echo "Goodbye!"
            exit 0
            ;;
        a|A)
            echo ""
            run_all
            ;;
        *)
            # Check if it's a number
            if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le ${#analyzers[@]} ]; then
                local selected="${analyzers[$((choice-1))]}"
                echo ""
                run_analyzer "$selected"
            else
                error "Invalid selection: $choice"
                exit 1
            fi
            ;;
    esac
}

# Show analyzer details
show_details() {
    local analyzer_name="$1"

    check_jq
    check_config

    local config
    config=$(jq ".analytics.analyzers[\"$analyzer_name\"]" "$CONFIG_FILE" 2>/dev/null)

    if [ "$config" = "null" ] || [ -z "$config" ]; then
        error "Analyzer '$analyzer_name' not found"
        exit 1
    fi

    echo ""
    echo -e "${BOLD}Analyzer: $analyzer_name${NC}"
    echo "========================="
    echo ""
    echo "$config" | jq '.'
}

# View recent logs
view_logs() {
    local lines="${1:-50}"

    check_container

    log "Showing last $lines lines of analytics logs..."
    echo ""

    docker logs --tail "$lines" "$CONTAINER_NAME"
}

# Show usage
usage() {
    echo ""
    echo -e "${BOLD}Analytics Manager${NC} - Manage and trigger analytics dry runs"
    echo ""
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  list                    List all configured analyzers"
    echo "  run <analyzer-name>     Run a specific analyzer (dry run)"
    echo "  run-all                 Run all enabled analyzers"
    echo "  details <analyzer-name> Show analyzer configuration details"
    echo "  logs [lines]            View recent analytics engine logs (default: 50)"
    echo "  interactive, -i         Interactive mode - select analyzer from menu"
    echo ""
    echo "Examples:"
    echo "  $0 list"
    echo "  $0 run acme-barcode-not-found"
    echo "  $0 run-all"
    echo "  $0 details acme-barcode-not-found"
    echo "  $0 logs 100"
    echo "  $0 -i"
    echo ""
    echo "Note: Dry runs execute the analyzer immediately and send email reports."
    echo "      Make sure the analytics container is running:"
    echo "      docker-compose --profile analytics up -d"
    echo ""
}

# Main entry point
main() {
    case "${1:-}" in
        list)
            list_analyzers
            ;;
        run)
            if [ -z "${2:-}" ]; then
                error "Analyzer name required"
                echo "Usage: $0 run <analyzer-name>"
                exit 1
            fi
            run_analyzer "$2"
            ;;
        run-all)
            run_all
            ;;
        details)
            if [ -z "${2:-}" ]; then
                error "Analyzer name required"
                echo "Usage: $0 details <analyzer-name>"
                exit 1
            fi
            show_details "$2"
            ;;
        logs)
            view_logs "${2:-50}"
            ;;
        interactive|-i)
            interactive
            ;;
        help|-h|--help)
            usage
            ;;
        "")
            usage
            ;;
        *)
            error "Unknown command: $1"
            usage
            exit 1
            ;;
    esac
}

main "$@"
