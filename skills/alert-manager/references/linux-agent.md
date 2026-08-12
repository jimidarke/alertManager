# Linux host health agent

The main repo ships a turnkey bash agent (`clients/linux-agent/alert-agent.sh`)
for "is this box and its Docker containers alive and healthy" reporting.
Deploy it instead of writing custom polling/monitoring code whenever the
task is host- or container-level health, not application-level error
reporting.

## When to use it vs. custom code

**Use it for**: proof-of-life heartbeats, disk/load/memory threshold
alerts, Docker container state changes (exited/unhealthy/restart-looping/
OOM-killed), and new kernel warnings/errors.

**Don't use it for**: application-level errors inside your own code (that's
the Python SDK's job — see `references/sdk-python.md`, especially
`install_exception_hooks()`), trending/metrics/graphs (use a real metrics
system if you need those), or detecting a fully dead host (a dead box
can't report itself — watch for *missing* heartbeats on the receiving end
instead).

## What it sends

Every run (heartbeats, `level=info`): a host health snapshot (uptime, load
vs. core count, memory/swap, per-filesystem disk usage) and a Docker
container summary (per-container state/health/restart count/resource
stats).

Only on threshold breach or state change (debounced via a local state
file): disk/load/memory warnings and criticals, a recovery notice when a
condition clears, a batched alert for container state changes, a critical
if the Docker daemon itself is unreachable, and a batched alert for new
kernel warnings/errors (tracked by journal cursor so each line reports
exactly once).

## Requirements

`bash`, `jq`, `curl`, coreutils. Runs as **root** — needs the Docker socket
and `journalctl -k` access. Without Docker it still works host-only;
without systemd it falls back to `dmesg`.

## Install

```bash
# On the target host:
install -m 755 alert-agent.sh /usr/local/bin/alert-agent.sh
install -d -m 700 /etc/alert-agent
install -m 600 alert-agent.env.example /etc/alert-agent/alert-agent.env
vi /etc/alert-agent/alert-agent.env   # set ALERT_URL, ALERT_CLIENT, ALERT_TOKEN
```

Note the agent's own dotenv uses `ALERT_URL`/`ALERT_CLIENT`/`ALERT_TOKEN` —
a different naming convention than this skill's other examples. See the
env-var table in `references/auth-and-provisioning.md` if that's confusing;
match the agent's own file, don't "fix" it.

## Provisioning

Needs a scoped `alerts:write` service account for this host's client name
(and that client name must already be registered — see
`references/auth-and-provisioning.md` for the full walkthrough and the
"no CLI to register a client" trap). A representative provisioning sequence
on the server:

```bash
docker exec -it <gateway-container> python3 tools/user-manager.py create-user svc-<host>-agent --default-client <client>
docker exec -it <gateway-container> python3 tools/user-manager.py add-scope <role> <client> alerts:write
docker exec -it <gateway-container> python3 tools/user-manager.py create-token svc-<host>-agent
```

## Verify before scheduling it

```bash
curl -s -H "Authorization: Bearer amt_..." https://alert.example.com/api/whoami
curl -s -X POST -H "Authorization: Bearer amt_..." https://alert.example.com/test
alert-agent.sh --dry-run        # prints every payload it would send, touches no state
alert-agent.sh                  # one real run
```

## Scheduling

**cron** (`/etc/cron.d/alert-agent`):

```
*/30 * * * * root /usr/local/bin/alert-agent.sh >/dev/null
```

**systemd timer** (alternative — `alert-agent.service` + `alert-agent.timer`):

```ini
[Unit]
Description=Alert Manager Linux health agent

[Service]
Type=oneshot
ExecStart=/usr/local/bin/alert-agent.sh
```

```ini
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

`RandomizedDelaySec` spreads a fleet's runs so they don't all hit the
gateway in the same instant.

## Behavior notes worth knowing before you deploy it

- **First run** establishes the kernel-log watermark and doesn't retroactively
  alert on old kernel log lines — but a threshold already breached (e.g.
  disk already at 96%) alerts immediately, since that's current state.
- **State** lives in `/var/lib/alert-agent/state.json` plus a kernel-log
  cursor. Delete the state directory to reset to first-run behavior.
- **Debounce is timestamp-based**, not run-count-based, so tightening or
  loosening the schedule doesn't change the debounce logic.
- **Gateway outages**: failed sends are spooled and retried on the next
  run. A `401` stops the agent from sending for the rest of that run
  specifically to avoid tripping account lockout from repeated bad-auth
  attempts; `400`/`403` responses are logged and dropped, not retried
  (retrying won't fix a payload or scope problem).
- **Container churn**: clean `Exited (0)` transitions, containers labeled
  to be ignored, and containers the agent never saw running are not
  alerted on.
- **Concurrent runs** are prevented with a lock file — a second instance
  exits silently rather than double-reporting.
