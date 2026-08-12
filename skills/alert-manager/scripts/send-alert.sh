#!/usr/bin/env bash
#
# send-alert.sh -- portable Alert Manager sender for shells with no SDK
# available. Validates level/category/required fields locally (mirroring
# sdk/python/src/alertmanager_sdk/validation.py) before ever making a
# network call, so a bad payload fails fast and locally instead of
# silently vanishing into the server's async validation stage (see
# ../SKILL.md gotcha 1: "HTTP 200 is not delivery confirmation").
#
# JSON is built with `python3 -c` (the stdlib json module) rather than
# hand-interpolated shell strings, to avoid the quoting/injection footgun
# of building JSON by string concatenation (a title or message containing
# a double-quote or backslash would corrupt a hand-built JSON string).
# python3 is already a dependency of every Python path this skill teaches
# (installing the SDK), so this isn't a new dependency in context.
#
# Usage:
#   send-alert.sh --health
#   send-alert.sh --whoami
#   send-alert.sh --test
#   send-alert.sh send LEVEL CATEGORY TITLE MESSAGE SOURCE [CLIENT]
#
# Env:
#   ALERT_MANAGER_URL       base URL, default https://alert.d-t.pw
#   ALERT_MANAGER_USERNAME  \_ Basic auth (used together)
#   ALERT_MANAGER_PASSWORD  /
#   ALERT_MANAGER_TOKEN     Bearer token -- takes precedence if also set
#
# Exit codes: 0 success, 2 local validation/usage error, 1 network/HTTP error.

set -euo pipefail

BASE_URL="${ALERT_MANAGER_URL:-https://alert.d-t.pw}"
VALID_LEVELS="critical warning notice info debug"
VALID_CATEGORIES="security system weather automation personal network application news business hardware environmental"

usage() {
  cat <<'EOF' >&2
Usage:
  send-alert.sh --health
  send-alert.sh --whoami
  send-alert.sh --test
  send-alert.sh send LEVEL CATEGORY TITLE MESSAGE SOURCE [CLIENT]

Env:
  ALERT_MANAGER_URL       base URL, default https://alert.d-t.pw
  ALERT_MANAGER_USERNAME  \_ Basic auth (used together)
  ALERT_MANAGER_PASSWORD  /
  ALERT_MANAGER_TOKEN     Bearer token -- takes precedence if also set

Levels:     critical warning notice info debug
Categories: security system weather automation personal network application news business hardware environmental
EOF
  exit 2
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "error: '$1' is required but not found on PATH" >&2; exit 2; }
}

trim_lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//'
}

normalize_level() {
  # mirrors LEVEL_ALIASES in sdk/python/src/alertmanager_sdk/validation.py
  local lvl
  lvl="$(trim_lower "$1")"
  case "$lvl" in
    error|err|crit) echo critical ;;
    warn) echo warning ;;
    inf) echo info ;;
    dbg) echo debug ;;
    *) echo "$lvl" ;;
  esac
}

# Populates the global AUTH_ARGS array consumed by do_request.
require_auth_args() {
  AUTH_ARGS=()
  if [[ -n "${ALERT_MANAGER_TOKEN:-}" ]]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${ALERT_MANAGER_TOKEN}")
  elif [[ -n "${ALERT_MANAGER_USERNAME:-}" && -n "${ALERT_MANAGER_PASSWORD:-}" ]]; then
    AUTH_ARGS=(-u "${ALERT_MANAGER_USERNAME}:${ALERT_MANAGER_PASSWORD}")
  else
    echo "error: set ALERT_MANAGER_TOKEN, or ALERT_MANAGER_USERNAME + ALERT_MANAGER_PASSWORD" >&2
    exit 2
  fi
}

# do_request METHOD PATH [JSON_DATA] -- prints the response body, returns
# non-zero on a transport failure or a non-2xx response (see usage note on
# exit codes at the top of this file).
do_request() {
  local method="$1" path="$2" data="${3:-}"
  local body_file status
  body_file="$(mktemp)"

  local -a curl_args=(-sS -o "$body_file" -w '%{http_code}' -X "$method" "${BASE_URL}${path}")
  if [[ -n "$data" ]]; then
    curl_args+=(-H "Content-Type: application/json" -d "$data")
  fi
  if [[ ${#AUTH_ARGS[@]} -gt 0 ]]; then
    curl_args+=("${AUTH_ARGS[@]}")
  fi

  if ! status="$(curl "${curl_args[@]}")"; then
    rm -f "$body_file"
    echo "error: request to ${BASE_URL}${path} failed (network/connection error)" >&2
    return 1
  fi

  cat "$body_file"
  echo
  rm -f "$body_file"

  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    echo "HTTP $status" >&2
    return 1
  fi
}

cmd_health() {
  AUTH_ARGS=()
  do_request GET /health
}

cmd_whoami() {
  require_auth_args
  do_request GET /api/whoami
}

cmd_test() {
  require_auth_args
  do_request POST /test
}

cmd_send() {
  if [[ $# -lt 5 || $# -gt 6 ]]; then
    echo "usage: send LEVEL CATEGORY TITLE MESSAGE SOURCE [CLIENT]" >&2
    exit 2
  fi
  local raw_level="$1" raw_category="$2" title="$3" message="$4" source="$5" client="${6:-}"
  local level category
  level="$(normalize_level "$raw_level")"
  category="$(trim_lower "$raw_category")"

  for field_name_value in "title:$title" "message:$message" "source:$source"; do
    if [[ -z "${field_name_value#*:}" ]]; then
      echo "error: ${field_name_value%%:*} must not be empty" >&2
      exit 2
    fi
  done

  if [[ -z "$level" ]] || ! grep -qw "$level" <<<"$VALID_LEVELS"; then
    echo "error: invalid level '$raw_level' -- valid levels: $VALID_LEVELS" >&2
    exit 2
  fi
  if [[ -z "$category" ]] || ! grep -qw "$category" <<<"$VALID_CATEGORIES"; then
    echo "error: invalid category '$raw_category' -- valid categories: $VALID_CATEGORIES" >&2
    exit 2
  fi

  need_cmd python3
  local payload
  payload="$(python3 -c '
import json, sys, time
level, category, title, message, source, client = sys.argv[1:7]
alert = {
    "timestamp": int(time.time()),
    "level": level,
    "category": category,
    "title": title,
    "message": message,
    "source": source,
}
if client:
    alert["client"] = client
print(json.dumps(alert))
' "$level" "$category" "$title" "$message" "$source" "$client")"

  require_auth_args
  do_request POST /alert "$payload"
}

main() {
  need_cmd curl
  case "${1:-}" in
    --health) cmd_health ;;
    --whoami) cmd_whoami ;;
    --test) cmd_test ;;
    send) shift; cmd_send "$@" ;;
    -h|--help|"") usage ;;
    *) echo "unknown command: $1" >&2; usage ;;
  esac
}

main "$@"
