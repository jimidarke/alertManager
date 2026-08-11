# Linux Health Agent

A single-file bash agent that reports **proof of life** and **escalating health
exceptions** from a Linux host (and its Docker containers) to the Alert Manager
via `POST /alert`. Designed to run from a 30-minute root cron or systemd timer.

## What it sends

Every run (heartbeats, `level=info`):

| Alert | Source | Contents (`metadata`) |
|---|---|---|
| Host health snapshot | `<hostname>` | uptime, load avg vs cores, memory/swap, per-filesystem disk usage |
| Docker summary | `<hostname>/docker` | per-container state, health, restart count, CPU/mem stats, counts |

Only on threshold breach / state change (debounced via a local state file):

| Condition | Level | Behavior |
|---|---|---|
| Disk ≥ warn% (default 90) | `warning` | fires once, then at most every 24h while it persists |
| Disk ≥ crit% (default 95) | `critical` | fires immediately on escalation, then hourly |
| Load / memory over thresholds | `warning`/`critical` | same tier engine as disk |
| Recovery below thresholds | `notice` | one alert when a condition clears |
| Container exited (non-zero) / unhealthy / restart loop / OOM-killed | `warning`/`critical` | one batched alert per run listing changed containers |
| Docker daemon unreachable | `critical` | debounced like a critical tier |
| New kernel warnings/errors | `warning` | one batched alert; journal cursor ensures each line is reported exactly once |

Not covered by design: app-level errors inside containers (containers report
themselves), trending/metrics (use Prometheus if you need graphs), and
dead-host detection (a dead box cannot report itself — watch for missing
heartbeats server-side).

## Requirements

- `bash`, `jq`, `curl`, coreutils
- Runs as **root** (needs `docker` socket access and `journalctl -k`;
  without docker it runs host-only, without systemd it falls back to `dmesg`)

## Install

```bash
# On the target host:
install -m 755 alert-agent.sh /usr/local/bin/alert-agent.sh
install -d -m 700 /etc/alert-agent
install -m 600 alert-agent.env.example /etc/alert-agent/alert-agent.env
vi /etc/alert-agent/alert-agent.env   # set ALERT_URL, ALERT_CLIENT, ALERT_TOKEN
```

### Server-side provisioning (admin, once per host)

```bash
# Register the client name (if new) and a service account with write scope:
python3 tools/user-manager.py create-user svc-acme-host01 --default-client acme-co
python3 tools/user-manager.py add-scope svc-acme-host01 --client acme-co --permission alerts:write
python3 tools/user-manager.py create-token svc-acme-host01   # -> amt_... (shown once)
```

Verify from the host before scheduling:

```bash
curl -s -H "Authorization: Bearer amt_..." https://alert.example.com/api/whoami
curl -s -X POST -H "Authorization: Bearer amt_..." https://alert.example.com/test
alert-agent.sh --dry-run        # prints every payload it would send
alert-agent.sh                  # one real run
```

### Schedule — cron

```
# /etc/cron.d/alert-agent
*/30 * * * * root /usr/local/bin/alert-agent.sh >/dev/null
```

### Schedule — systemd timer (alternative)

```ini
# /etc/systemd/system/alert-agent.service
[Unit]
Description=Alert Manager Linux health agent

[Service]
Type=oneshot
ExecStart=/usr/local/bin/alert-agent.sh
```

```ini
# /etc/systemd/system/alert-agent.timer
[Unit]
Description=Run alert-agent every 30 minutes

[Timer]
OnCalendar=*:0/30
RandomizedDelaySec=300
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now alert-agent.timer
```

`RandomizedDelaySec` spreads a fleet's runs so they don't all hit the gateway
at the same instant.

## Behavior notes

- **First run** establishes the kernel journal cursor / dmesg watermark and
  fires nothing retroactive from the kernel log. Current threshold breaches
  (e.g. a disk already at 96%) **do** alert immediately — that's current
  state, not history.
- **State** lives in `/var/lib/alert-agent/state.json` (debounce timers, tier
  per filesystem, container cache, retry spool) plus `kernel.cursor`. Delete
  the directory to reset to first-run behavior.
- **Debounce is timestamp-based**, never run-count-based — the schedule can be
  tightened or loosened without touching the logic. A 30-min cron means an
  "hourly" critical re-fire lands every other run (worst case ~90 min after
  the breach moment).
- **Gateway outages**: failed sends (5xx/network) are spooled (last
  `SPOOL_MAX`, dropped after 24h) and retried at the start of the next run.
  `401` stops sending for the rest of the run to avoid tripping the account
  lockout (5 failures → 900 s); `400`/`403` are dropped and logged — fix the
  payload/scope, retrying won't help.
- **Container churn**: `Exited (0)` transitions, containers labeled
  `alert-agent.ignore=true`, and containers never seen running by the agent
  are not alerted on.
- **Concurrent runs** are prevented with `flock`; a second instance exits
  silently.

## Options

```
alert-agent.sh [-c|--config FILE] [-n|--dry-run] [-V|--version] [-h|--help]
```

`--dry-run` prints every payload instead of sending and leaves all state
(including the kernel cursor) untouched. Config path can also be set via
`ALERT_AGENT_CONFIG`. Any config value can be overridden per-invocation via
the environment, e.g. `DISK_WARN_PCT=50 alert-agent.sh --dry-run`.
