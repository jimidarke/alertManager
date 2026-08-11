#!/usr/bin/env bash
#
# alert-agent.sh - Linux host + Docker health agent for Alert Manager
#
# Sends proof-of-life snapshots (host telemetry, docker container summary)
# every run and escalating threshold alerts (disk/load/memory/container
# state/kernel errors) with debounce driven by a local state file.
#
# Designed for a 30-minute root cron or systemd timer. See README.md.
#
# Dependencies: bash, jq, curl, coreutils. Optional: docker, journalctl/dmesg.

set -euo pipefail

AGENT_VERSION="1.0.0"
CONFIG_FILE="${ALERT_AGENT_CONFIG:-/etc/alert-agent/alert-agent.env}"
DRY_RUN=0

LEVELS="critical warning notice info debug"
CATEGORIES="security system weather automation personal network application news business hardware environmental"

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
usage() {
    cat <<EOF
alert-agent.sh v${AGENT_VERSION} - Linux health agent for Alert Manager

Usage: alert-agent.sh [options]

Options:
  -c, --config FILE   Config file (default: ${CONFIG_FILE})
  -n, --dry-run       Print payloads instead of sending; do not advance state
  -V, --version       Print version and exit
  -h, --help          Show this help
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -c|--config)  CONFIG_FILE="$2"; shift 2 ;;
        -n|--dry-run) DRY_RUN=1; shift ;;
        -V|--version) echo "$AGENT_VERSION"; exit 0 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

log() { echo "[alert-agent] $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

# ---------------------------------------------------------------------------
# Preflight: dependencies + config
# ---------------------------------------------------------------------------
command -v jq   >/dev/null 2>&1 || die "jq is required"
command -v curl >/dev/null 2>&1 || die "curl is required"

[[ -f "$CONFIG_FILE" ]] || die "config file not found: $CONFIG_FILE"
# shellcheck source=/dev/null
source "$CONFIG_FILE"

ALERT_URL="${ALERT_URL:-https://alert.d-t.pw}"
ALERT_URL="${ALERT_URL%/}"
ALERT_CLIENT="${ALERT_CLIENT:-}"
ALERT_TOKEN="${ALERT_TOKEN:-}"
ALERT_USER="${ALERT_USER:-}"
ALERT_PASSWORD="${ALERT_PASSWORD:-}"

DISK_WARN_PCT="${DISK_WARN_PCT:-90}"
DISK_CRIT_PCT="${DISK_CRIT_PCT:-95}"
DISK_WARN_REFIRE="${DISK_WARN_REFIRE:-86400}"
DISK_CRIT_REFIRE="${DISK_CRIT_REFIRE:-3600}"
LOAD_WARN_MULT="${LOAD_WARN_MULT:-2}"
LOAD_CRIT_MULT="${LOAD_CRIT_MULT:-4}"
MEM_WARN_PCT="${MEM_WARN_PCT:-90}"
MEM_CRIT_PCT="${MEM_CRIT_PCT:-95}"
KERNEL_MAX_LINES="${KERNEL_MAX_LINES:-40}"
STATE_DIR="${STATE_DIR:-/var/lib/alert-agent}"
SPOOL_MAX="${SPOOL_MAX:-50}"
CURL_TIMEOUT="${CURL_TIMEOUT:-15}"

[[ -n "$ALERT_CLIENT" ]] || die "ALERT_CLIENT is required (set it in $CONFIG_FILE)"
if [[ -z "$ALERT_TOKEN" && ( -z "$ALERT_USER" || -z "$ALERT_PASSWORD" ) ]]; then
    die "auth required: set ALERT_TOKEN or ALERT_USER+ALERT_PASSWORD in $CONFIG_FILE"
fi

AUTH_ARGS=()
if [[ -n "$ALERT_TOKEN" ]]; then
    AUTH_ARGS=(-H "Authorization: Bearer ${ALERT_TOKEN}")
else
    AUTH_ARGS=(-u "${ALERT_USER}:${ALERT_PASSWORD}")
fi

HOSTNAME_S="$(hostname -s 2>/dev/null || hostname)"
NOW="$(date +%s)"

# ---------------------------------------------------------------------------
# State + lock
# ---------------------------------------------------------------------------
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true
STATE_FILE="$STATE_DIR/state.json"
KCURSOR="$STATE_DIR/kernel.cursor"

exec 9>"$STATE_DIR/lock"
flock -n 9 || { log "another instance holds the lock; exiting"; exit 0; }

FIRST_RUN=0
if [[ -f "$STATE_FILE" ]] && STATE="$(jq -c . "$STATE_FILE" 2>/dev/null)"; then
    :
else
    FIRST_RUN=1
    STATE='{"disks":{},"load":{"tier":"ok","last_fired":0},"memory":{"tier":"ok","last_fired":0},"docker":{"daemon_tier":"ok","last_fired":0,"containers":{}},"kernel":{"dmesg_watermark":0},"spool":[],"last_run":0}'
    [[ -f "$STATE_FILE" ]] && log "state file unreadable, starting fresh"
fi

save_state() {
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    local tmp
    tmp="$(mktemp "$STATE_DIR/.state.XXXXXX")"
    jq --argjson now "$NOW" '.last_run = $now' <<<"$STATE" >"$tmp"
    chmod 600 "$tmp"
    mv "$tmp" "$STATE_FILE"
}

# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
AUTH_FAILED=0

# http_send <payload-json>
# Return: 0 sent, 1 retryable (5xx/network), 2 permanent (4xx), 3 auth (401)
http_send() {
    local payload="$1" body http_code
    body="$(mktemp)"
    http_code="$(curl -sS --max-time "$CURL_TIMEOUT" -o "$body" -w '%{http_code}' \
        -X POST -H 'Content-Type: application/json' "${AUTH_ARGS[@]}" \
        --data-binary "$payload" "${ALERT_URL}/alert" 2>/dev/null)" || http_code="000"
    local resp; resp="$(head -c 300 "$body" 2>/dev/null || true)"; rm -f "$body"
    case "$http_code" in
        2*) return 0 ;;
        401) log "send failed 401 (auth) - check credentials/lockout: $resp"; return 3 ;;
        400|403|404|413|422) log "send failed $http_code (permanent, dropping): $resp"; return 2 ;;
        *) log "send failed $http_code (will retry via spool): $resp"; return 1 ;;
    esac
}

spool_add() {
    STATE="$(jq -c --argjson p "$1" --argjson now "$NOW" --argjson max "$SPOOL_MAX" \
        '.spool += [{payload:$p, queued_at:$now}] | .spool |= .[-$max:]' <<<"$STATE")"
}

# send_alert <level> <category> <title> <message> <metadata-json> <source>
send_alert() {
    local level="$1" category="$2" title="$3" message="$4" meta="$5" source="$6" payload
    case " $LEVELS " in *" $level "*) ;; *) log "BUG: invalid level '$level', dropping alert '$title'"; return 0 ;; esac
    case " $CATEGORIES " in *" $category "*) ;; *) log "BUG: invalid category '$category', dropping alert '$title'"; return 0 ;; esac

    payload="$(jq -nc \
        --argjson ts "$NOW" --arg level "$level" --arg category "$category" \
        --arg title "$title" --arg message "$message" --arg source "$source" \
        --arg client "$ALERT_CLIENT" --argjson meta "$meta" \
        '{timestamp:$ts, level:$level, category:$category, title:$title,
          message:$message, source:$source, client:$client, metadata:$meta}')"

    if [[ "$DRY_RUN" -eq 1 ]]; then
        echo "--- DRY RUN payload ---"
        jq . <<<"$payload"
        return 0
    fi
    if [[ "$AUTH_FAILED" -eq 1 ]]; then
        log "auth already failed this run, dropping alert '$title'"
        return 0
    fi
    local rc=0
    http_send "$payload" || rc=$?
    case "$rc" in
        1) spool_add "$payload" ;;
        3) AUTH_FAILED=1 ;;
    esac
    return 0
}

flush_spool() {
    local entries kept="[]" entry qa payload rc
    entries="$(jq -c '.spool[]?' <<<"$STATE")"
    [[ -z "$entries" ]] && return 0
    [[ "$DRY_RUN" -eq 1 ]] && return 0
    while IFS= read -r entry; do
        [[ -z "$entry" ]] && continue
        qa="$(jq -r '.queued_at' <<<"$entry")"
        (( NOW - qa > 86400 )) && continue          # too old, drop
        if [[ "$AUTH_FAILED" -eq 1 ]]; then
            kept="$(jq -c --argjson e "$entry" '. + [$e]' <<<"$kept")"
            continue
        fi
        payload="$(jq -c '.payload' <<<"$entry")"
        rc=0; http_send "$payload" || rc=$?
        case "$rc" in
            0|2) ;;                                  # sent or permanent: drop from spool
            3) AUTH_FAILED=1; kept="$(jq -c --argjson e "$entry" '. + [$e]' <<<"$kept")" ;;
            *) kept="$(jq -c --argjson e "$entry" '. + [$e]' <<<"$kept")" ;;
        esac
    done <<<"$entries"
    STATE="$(jq -c --argjson k "$kept" '.spool = $k' <<<"$STATE")"
}

# Retry previously failed sends before collecting anything new
flush_spool

# ---------------------------------------------------------------------------
# Tier engine (debounce/unflap)
# ---------------------------------------------------------------------------
# tier_action <prev> <new> <last_fired> <warn_refire> <crit_refire>
# Echoes: fire | recover | none
tier_action() {
    local prev="$1" new="$2" last="$3" warn_re="$4" crit_re="$5" window
    if [[ "$new" == "ok" ]]; then
        if [[ "$prev" == "warn" || "$prev" == "crit" ]]; then echo recover; else echo none; fi
        return 0
    fi
    if [[ "$new" != "$prev" ]]; then echo fire; return 0; fi
    [[ "$new" == "crit" ]] && window="$crit_re" || window="$warn_re"
    if (( NOW - last >= window )); then echo fire; else echo none; fi
}

tier_level() { [[ "$1" == "crit" ]] && echo critical || echo warning; }

# ---------------------------------------------------------------------------
# Host telemetry
# ---------------------------------------------------------------------------
UPTIME_S="$(awk '{print int($1)}' /proc/uptime)"
read -r LOAD1 LOAD5 LOAD15 _ < /proc/loadavg
NPROC="$(nproc)"
MEM_TOTAL_KB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
MEM_AVAIL_KB="$(awk '/^MemAvailable:/{print $2}' /proc/meminfo)"
SWAP_TOTAL_KB="$(awk '/^SwapTotal:/{print $2}' /proc/meminfo)"
SWAP_FREE_KB="$(awk '/^SwapFree:/{print $2}' /proc/meminfo)"
MEM_USED_PCT="$(awk -v t="$MEM_TOTAL_KB" -v a="$MEM_AVAIL_KB" 'BEGIN{printf "%.1f", (t>0)?(100*(t-a)/t):0}')"

# df: local real filesystems only. Columns: fs 1k-blocks used avail cap% mount
DF_OUT="$(df -P -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs 2>/dev/null | tail -n +2 || true)"
DISKS_JSON="$(awk '$1 ~ /^\// {gsub(/%/,"",$5); printf "%s\t%s\t%s\t%s\t%s\n", $1, $6, $2, $3, $5}' <<<"$DF_OUT" \
    | jq -Rsc '[splits("\n")] | map(select(length>0) | split("\t") |
        {device:.[0], mount:.[1], size_kb:(.[2]|tonumber), used_kb:(.[3]|tonumber), used_pct:(.[4]|tonumber)})' \
    2>/dev/null || echo '[]')"

MAX_DISK_PCT="$(jq -r 'map(.used_pct) | max // 0' <<<"$DISKS_JSON")"
UP_DAYS=$(( UPTIME_S / 86400 )); UP_HRS=$(( (UPTIME_S % 86400) / 3600 ))

host_meta="$(jq -nc \
    --argjson uptime "$UPTIME_S" --argjson boot "$(( NOW - UPTIME_S ))" \
    --arg l1 "$LOAD1" --arg l5 "$LOAD5" --arg l15 "$LOAD15" --argjson nproc "$NPROC" \
    --argjson mem_total_kb "$MEM_TOTAL_KB" --argjson mem_avail_kb "$MEM_AVAIL_KB" \
    --argjson swap_total_kb "$SWAP_TOTAL_KB" --argjson swap_free_kb "$SWAP_FREE_KB" \
    --arg mem_used_pct "$MEM_USED_PCT" --argjson disks "$DISKS_JSON" --arg ver "$AGENT_VERSION" \
    '{uptime_seconds:$uptime, boot_epoch:$boot,
      load:{avg1:($l1|tonumber), avg5:($l5|tonumber), avg15:($l15|tonumber), cores:$nproc},
      memory:{total_kb:$mem_total_kb, available_kb:$mem_avail_kb, used_pct:($mem_used_pct|tonumber),
              swap_total_kb:$swap_total_kb, swap_free_kb:$swap_free_kb},
      disks:$disks, agent_version:$ver}')"

send_alert info system "Host health: ${HOSTNAME_S}" \
    "up ${UP_DAYS}d ${UP_HRS}h, load ${LOAD5}/${NPROC} cores (5m), mem ${MEM_USED_PCT}% used, max disk ${MAX_DISK_PCT}%" \
    "$host_meta" "$HOSTNAME_S"

# --- Disk thresholds (per filesystem) --------------------------------------
while IFS=$'\t' read -r dev mount pct; do
    [[ -z "$dev" ]] && continue
    key="${dev}:${mount}"
    tier=ok
    (( pct >= DISK_WARN_PCT )) && tier=warn
    (( pct >= DISK_CRIT_PCT )) && tier=crit
    prev="$(jq -r --arg k "$key" '.disks[$k].tier // "ok"' <<<"$STATE")"
    last="$(jq -r --arg k "$key" '.disks[$k].last_fired // 0' <<<"$STATE")"
    action="$(tier_action "$prev" "$tier" "$last" "$DISK_WARN_REFIRE" "$DISK_CRIT_REFIRE")"
    meta="$(jq -nc --arg d "$dev" --arg m "$mount" --argjson p "$pct" \
        --argjson w "$DISK_WARN_PCT" --argjson c "$DISK_CRIT_PCT" \
        '{device:$d, mount:$m, used_pct:$p, warn_pct:$w, crit_pct:$c}')"
    case "$action" in
        fire)
            send_alert "$(tier_level "$tier")" hardware \
                "Disk usage ${pct}% on ${HOSTNAME_S}:${mount}" \
                "Filesystem ${dev} mounted at ${mount} is ${pct}% full (warn ${DISK_WARN_PCT}%, critical ${DISK_CRIT_PCT}%)." \
                "$meta" "$HOSTNAME_S"
            STATE="$(jq -c --arg k "$key" --arg t "$tier" --argjson now "$NOW" \
                '.disks[$k] = {tier:$t, last_fired:$now}' <<<"$STATE")"
            ;;
        recover)
            send_alert notice hardware \
                "Disk usage recovered on ${HOSTNAME_S}:${mount}" \
                "Filesystem ${dev} mounted at ${mount} is back to ${pct}% (below warn threshold ${DISK_WARN_PCT}%)." \
                "$meta" "$HOSTNAME_S"
            STATE="$(jq -c --arg k "$key" '.disks[$k] = {tier:"ok", last_fired:0}' <<<"$STATE")"
            ;;
        none)
            STATE="$(jq -c --arg k "$key" --arg t "$tier" --argjson last "$last" \
                '.disks[$k] = {tier:$t, last_fired:$last}' <<<"$STATE")"
            ;;
    esac
done < <(awk '$1 ~ /^\// {gsub(/%/,"",$5); printf "%s\t%s\t%s\n", $1, $6, $5}' <<<"$DF_OUT")

# Drop state for filesystems that no longer exist
STATE="$(jq -c --argjson disks "$DISKS_JSON" \
    '.disks |= with_entries(select(.key as $k | $disks | map(.device + ":" + .mount) | index($k)))' <<<"$STATE")"

# --- Load threshold (5-min avg vs cores) -----------------------------------
load_tier="$(awk -v l="$LOAD5" -v n="$NPROC" -v w="$LOAD_WARN_MULT" -v c="$LOAD_CRIT_MULT" \
    'BEGIN{ if (l >= n*c) print "crit"; else if (l >= n*w) print "warn"; else print "ok" }')"
prev="$(jq -r '.load.tier' <<<"$STATE")"
last="$(jq -r '.load.last_fired' <<<"$STATE")"
action="$(tier_action "$prev" "$load_tier" "$last" "$DISK_WARN_REFIRE" "$DISK_CRIT_REFIRE")"
load_meta="$(jq -nc --arg l5 "$LOAD5" --argjson n "$NPROC" --argjson w "$LOAD_WARN_MULT" --argjson c "$LOAD_CRIT_MULT" \
    '{load_avg5:($l5|tonumber), cores:$n, warn_mult:$w, crit_mult:$c}')"
case "$action" in
    fire)
        send_alert "$(tier_level "$load_tier")" system \
            "High load on ${HOSTNAME_S}: ${LOAD5}" \
            "5-minute load average ${LOAD5} on ${NPROC} cores (warn at ${LOAD_WARN_MULT}x, critical at ${LOAD_CRIT_MULT}x cores)." \
            "$load_meta" "$HOSTNAME_S"
        STATE="$(jq -c --arg t "$load_tier" --argjson now "$NOW" '.load = {tier:$t, last_fired:$now}' <<<"$STATE")"
        ;;
    recover)
        send_alert notice system "Load recovered on ${HOSTNAME_S}" \
            "5-minute load average back to ${LOAD5} on ${NPROC} cores." "$load_meta" "$HOSTNAME_S"
        STATE="$(jq -c '.load = {tier:"ok", last_fired:0}' <<<"$STATE")"
        ;;
    none)
        STATE="$(jq -c --arg t "$load_tier" '.load.tier = $t' <<<"$STATE")"
        ;;
esac

# --- Memory threshold -------------------------------------------------------
mem_tier="$(awk -v p="$MEM_USED_PCT" -v w="$MEM_WARN_PCT" -v c="$MEM_CRIT_PCT" \
    'BEGIN{ if (p >= c) print "crit"; else if (p >= w) print "warn"; else print "ok" }')"
prev="$(jq -r '.memory.tier' <<<"$STATE")"
last="$(jq -r '.memory.last_fired' <<<"$STATE")"
action="$(tier_action "$prev" "$mem_tier" "$last" "$DISK_WARN_REFIRE" "$DISK_CRIT_REFIRE")"
mem_meta="$(jq -nc --arg p "$MEM_USED_PCT" --argjson t "$MEM_TOTAL_KB" --argjson a "$MEM_AVAIL_KB" \
    --argjson w "$MEM_WARN_PCT" --argjson c "$MEM_CRIT_PCT" \
    '{used_pct:($p|tonumber), total_kb:$t, available_kb:$a, warn_pct:$w, crit_pct:$c}')"
case "$action" in
    fire)
        send_alert "$(tier_level "$mem_tier")" system \
            "High memory use on ${HOSTNAME_S}: ${MEM_USED_PCT}%" \
            "Memory ${MEM_USED_PCT}% used (MemAvailable-based; warn ${MEM_WARN_PCT}%, critical ${MEM_CRIT_PCT}%)." \
            "$mem_meta" "$HOSTNAME_S"
        STATE="$(jq -c --arg t "$mem_tier" --argjson now "$NOW" '.memory = {tier:$t, last_fired:$now}' <<<"$STATE")"
        ;;
    recover)
        send_alert notice system "Memory use recovered on ${HOSTNAME_S}" \
            "Memory back to ${MEM_USED_PCT}% used." "$mem_meta" "$HOSTNAME_S"
        STATE="$(jq -c '.memory = {tier:"ok", last_fired:0}' <<<"$STATE")"
        ;;
    none)
        STATE="$(jq -c --arg t "$mem_tier" '.memory.tier = $t' <<<"$STATE")"
        ;;
esac

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
    if ! docker info >/dev/null 2>&1; then
        prev="$(jq -r '.docker.daemon_tier' <<<"$STATE")"
        last="$(jq -r '.docker.last_fired' <<<"$STATE")"
        action="$(tier_action "$prev" "crit" "$last" "$DISK_WARN_REFIRE" "$DISK_CRIT_REFIRE")"
        if [[ "$action" == "fire" ]]; then
            send_alert critical system "Docker daemon unreachable on ${HOSTNAME_S}" \
                "docker CLI is installed but the daemon did not respond to 'docker info'. Container checks skipped." \
                '{"check":"docker_daemon"}' "${HOSTNAME_S}/docker"
            STATE="$(jq -c --argjson now "$NOW" '.docker.daemon_tier = "crit" | .docker.last_fired = $now' <<<"$STATE")"
        fi
    else
        if [[ "$(jq -r '.docker.daemon_tier' <<<"$STATE")" == "crit" ]]; then
            send_alert notice system "Docker daemon recovered on ${HOSTNAME_S}" \
                "The docker daemon is responding again." '{"check":"docker_daemon"}' "${HOSTNAME_S}/docker"
        fi
        STATE="$(jq -c '.docker.daemon_tier = "ok" | .docker.last_fired = 0' <<<"$STATE")"

        # Inspect all containers -> normalized array
        CIDS="$(docker ps -aq 2>/dev/null || true)"
        if [[ -n "$CIDS" ]]; then
            # shellcheck disable=SC2086
            CONTAINERS="$(docker inspect $CIDS 2>/dev/null | jq -c '[.[] | {
                name: (.Name | ltrimstr("/")),
                image: .Config.Image,
                state: .State.Status,
                health: (.State.Health.Status // "none"),
                restarts: (.RestartCount // 0),
                oom: (.State.OOMKilled // false),
                exit_code: (.State.ExitCode // 0),
                started_at: .State.StartedAt,
                ignore: (((.Config.Labels // {})["alert-agent.ignore"] // "") == "true")
            }]' 2>/dev/null || echo '[]')"
        else
            CONTAINERS='[]'
        fi

        # Live resource stats for running containers (name -> {cpu, mem})
        STATS="$(docker stats --no-stream --format '{{json .}}' 2>/dev/null \
            | jq -sc 'map({key:.Name, value:{cpu:.CPUPerc, mem:.MemUsage, mem_pct:.MemPerc}}) | from_entries' \
            2>/dev/null || echo '{}')"

        docker_meta="$(jq -nc --argjson c "$CONTAINERS" --argjson s "$STATS" '
            ($c | map(select(.ignore | not))) as $vis |
            {containers: ($vis | map(. + {stats: ($s[.name] // null)} | del(.ignore))),
             counts: {total: ($vis|length),
                      running: ($vis | map(select(.state=="running")) | length),
                      unhealthy: ($vis | map(select(.health=="unhealthy")) | length),
                      exited: ($vis | map(select(.state=="exited")) | length)}}')"
        n_total="$(jq -r '.counts.total' <<<"$docker_meta")"
        n_run="$(jq -r '.counts.running' <<<"$docker_meta")"
        n_unhealthy="$(jq -r '.counts.unhealthy' <<<"$docker_meta")"

        send_alert info system "Docker summary: ${HOSTNAME_S}" \
            "${n_run}/${n_total} containers running, ${n_unhealthy} unhealthy" \
            "$docker_meta" "${HOSTNAME_S}/docker"

        # Transition detection vs cached container state
        CACHED="$(jq -c '.docker.containers' <<<"$STATE")"
        CHANGES="$(jq -nc --argjson cur "$CONTAINERS" --argjson old "$CACHED" '
            [ $cur[] | select(.ignore | not) | . as $c
              | ($old[$c.name] // null) as $o
              | if $o == null then empty else
                  ([ (if $c.state != $o.state and
                         (($c.state == "exited" and $c.exit_code == 0) | not)
                      then {r:"state \($o.state) -> \($c.state)\(if $c.state=="exited" then " (exit \($c.exit_code))" else "" end)",
                            bad:($c.state != "running")} else empty end),
                     (if $c.health != $o.health and $c.health != "none"
                      then {r:"health \($o.health) -> \($c.health)", bad:($c.health == "unhealthy")} else empty end),
                     (if $c.restarts > ($o.restarts // 0)
                      then {r:"restarted \($c.restarts - ($o.restarts // 0))x", bad:true} else empty end),
                     (if $c.oom and (($o.oom // false) | not)
                      then {r:"OOM-killed", bad:true} else empty end)
                   ]) as $rs
                  | if ($rs|length) > 0
                    then {name:$c.name, image:$c.image, state:$c.state, health:$c.health,
                          exit_code:$c.exit_code, oom:$c.oom,
                          reasons:($rs|map(.r)), bad:($rs|any(.bad))}
                    else empty end
                end ]')"

        if [[ "$(jq 'length' <<<"$CHANGES")" -gt 0 && "$FIRST_RUN" -eq 0 ]]; then
            level=notice
            jq -e 'any(.bad)' <<<"$CHANGES" >/dev/null && level=warning
            jq -e 'any(.oom or .health == "unhealthy")' <<<"$CHANGES" >/dev/null && level=critical
            changed_msg="$(jq -r 'map("\(.name): \(.reasons | join(", "))") | join("; ")' <<<"$CHANGES")"
            send_alert "$level" system \
                "Container changes on ${HOSTNAME_S}: $(jq 'length' <<<"$CHANGES") container(s)" \
                "$changed_msg" "$(jq -c '{changes:.}' <<<"$CHANGES")" "${HOSTNAME_S}/docker"
        fi

        # Update cache (non-ignored containers only)
        STATE="$(jq -c --argjson cur "$CONTAINERS" \
            '.docker.containers = ($cur | map(select(.ignore | not)
                | {key:.name, value:{state:.state, health:.health, restarts:.restarts, oom:.oom}}) | from_entries)' \
            <<<"$STATE")"
    fi
fi

# ---------------------------------------------------------------------------
# Kernel errors/warnings since last run
# ---------------------------------------------------------------------------
kernel_report() {
    local lines="$1" total="$2" via="$3" shown level
    [[ -z "$lines" || "$total" -eq 0 ]] && return 0
    shown="$(head -n "$KERNEL_MAX_LINES" <<<"$lines")"
    level=warning
    kmeta="$(jq -nc --argjson total "$total" --argjson max "$KERNEL_MAX_LINES" --arg via "$via" \
        '{total_lines:$total, shown_lines:([$total,$max]|min), collected_via:$via}')"
    send_alert "$level" system \
        "Kernel warnings/errors on ${HOSTNAME_S}: ${total} line(s)" \
        "$shown" "$kmeta" "${HOSTNAME_S}/kernel"
}

if command -v journalctl >/dev/null 2>&1 && journalctl -k -n 1 >/dev/null 2>&1; then
    KC="$KCURSOR"
    if [[ "$DRY_RUN" -eq 1 ]]; then
        KC="$(mktemp)"; [[ -f "$KCURSOR" ]] && cp "$KCURSOR" "$KC"
    fi
    # NOTE: the cursor read below is deliberately UNFILTERED (-p filtering is
    # done in jq). journalctl's --cursor-file misbehaves when the stored
    # cursor entry doesn't match the active filter: the first matching entry
    # after it is skipped instead of shown (observed on systemd 249). An
    # unfiltered read means the cursor always points at an entry the next
    # read can see, so nothing is ever swallowed.
    if [[ ! -s "$KC" ]]; then
        # First run: establish cursor at the current tail, report nothing retroactive
        journalctl -k -n 1 -o cat -q --cursor-file="$KC" >/dev/null 2>&1 || true
        log "kernel journal cursor established (no retroactive alerts)"
    else
        KLINES="$(journalctl -k -o json -q --no-pager --cursor-file="$KC" 2>/dev/null \
            | jq -r 'select(.MESSAGE != null and ((.PRIORITY // "6") | tonumber) <= 4)
                | "[\((.__REALTIME_TIMESTAMP | tonumber / 1000000 | floor | todate))] \(.MESSAGE | if type == "array" then implode else . end)"' \
            || true)"
        if [[ -n "$KLINES" ]]; then
            kernel_report "$KLINES" "$(wc -l <<<"$KLINES")" "journalctl"
        fi
    fi
    [[ "$DRY_RUN" -eq 1 && "$KC" != "$KCURSOR" ]] && rm -f "$KC"
elif command -v dmesg >/dev/null 2>&1 && dmesg -h 2>&1 | grep -q -- '--time-format'; then
    WATERMARK="$(jq -r '.kernel.dmesg_watermark' <<<"$STATE")"
    if [[ "$WATERMARK" -eq 0 ]]; then
        STATE="$(jq -c --argjson now "$NOW" '.kernel.dmesg_watermark = $now' <<<"$STATE")"
        log "dmesg watermark established (no retroactive alerts)"
    else
        DLINES=""
        DMAX="$WATERMARK"
        while IFS= read -r line; do
            ts="${line%% *}"
            epoch="$(date -d "${ts/,/.}" +%s 2>/dev/null || echo 0)"
            if (( epoch > WATERMARK )); then
                DLINES+="${line}"$'\n'
                (( epoch > DMAX )) && DMAX="$epoch"
            fi
        done < <(dmesg --level=warn,err,crit,alert,emerg --time-format=iso 2>/dev/null || true)
        DLINES="${DLINES%$'\n'}"
        if [[ -n "$DLINES" ]]; then
            kernel_report "$DLINES" "$(wc -l <<<"$DLINES")" "dmesg"
        fi
        STATE="$(jq -c --argjson w "$DMAX" '.kernel.dmesg_watermark = $w' <<<"$STATE")"
    fi
else
    log "no journalctl or usable dmesg; kernel check skipped"
fi

# ---------------------------------------------------------------------------
# Persist state
# ---------------------------------------------------------------------------
save_state
[[ "$DRY_RUN" -eq 1 ]] && log "dry run complete (state not saved)"
exit 0
