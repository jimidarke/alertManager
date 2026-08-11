# Linux Health Agent — Planning Tracker

**Status:** IMPLEMENTED (v1.0.0, 2026-08-10) — agent lives at `clients/linux-agent/`; remaining items tracked in Phase 5
**Created:** 2026-08-10
**Goal:** A lightweight agent (shell script + systemd timer) for Linux boxes that reports host + Docker container health to the Alert Manager via `POST /alert` (the sole supported ingestion path). No app-level monitoring inside containers — system/container health only.

---

## 1. Scope

### In scope (per scan)
- **Host telemetry:** disk usage (per mounted filesystem, not just `/`), CPU (load average vs. core count), memory (`MemAvailable`, swap), uptime.
- **Per-container:** running/exited/restarting state, health-check status (if defined), CPU %, memory usage/limit, restart count, OOM-killed flag.
- **Docker daemon itself:** unreachable daemon is a finding, not a script failure.
- **Kernel ring:** errors/warnings from `journalctl -k -p warning` (or `dmesg` fallback) since the last scan.

### Explicitly out of scope
- App-level errors inside containers (containers self-report via their own integrations).
- Time-series metrics/graphing — see §3 "Alerting vs. telemetry", this is the biggest design decision.

---

## 2. Payload mapping (fits existing schema, no server changes)

| Thing | level | category | source | notes |
|---|---|---|---|---|
| Disk ≥ warn/crit threshold | `warning`/`critical` | `hardware` | `<hostname>` | per-filesystem, threshold in config |
| Load avg > N × cores | `warning`/`critical` | `system` | `<hostname>` | sustained, not instantaneous |
| Low MemAvailable / swap thrash | `warning`/`critical` | `system` | `<hostname>` | |
| Host (re)boot detected | `notice` | `system` | `<hostname>` | uptime < scan interval |
| Container unhealthy / exited / restart-looping | `warning`/`critical` | `system` | `<hostname>/<container>` | |
| Container OOM-killed | `critical` | `hardware` | `<hostname>/<container>` | |
| Docker daemon unreachable | `critical` | `system` | `<hostname>` | |
| Kernel err/warn lines | `warning` | `system` | `<hostname>` | batched into one alert per scan |
| Periodic heartbeat/summary (optional) | `info` | `system` | `<hostname>` | full snapshot in `metadata` |

- `timestamp`: Unix epoch integer (schema requirement — no ISO strings).
- `client`: see open question Q1 (per-host client vs. one fleet client).
- Full raw numbers go in `metadata` (structured), human summary in `message`.
- Kernel log lines are multiline → rely on server-side file-pathway sanitization, but also cap/batch client-side (one alert per scan with first N lines, not one alert per line).

---

## 3. Gaps you'd hit if we skipped straight to a script (the "am I missing anything" list)

- [ ] **Alerting vs. telemetry mismatch.** Alert Manager is an alert router, not a metrics store — logs are flat files/Postgres rows, not queryable time-series. A scan every 5 min sending "CPU is 12%" as `info` is log noise with no graphs. **Recommendation:** threshold-based alerts only (send when abnormal, and on state *change*), plus one low-frequency `info` heartbeat (e.g. hourly or daily) carrying the full snapshot in `metadata`. If real dashboards/trends are ever wanted, that's a Prometheus/node_exporter job, not this.
- [ ] **Flap/repeat suppression.** Disk at 91% would fire `critical` every scan forever. The agent needs local state (`/var/lib/alertagent/state.json` or similar): remember last-reported state per check, alert on transitions (ok→warn, warn→crit, →recovered), optionally re-alert every N hours while still bad. Also send an explicit **recovery `notice`** when a condition clears.
- [ ] **Dead-host detection.** A hung/dead box can't report its own death, and Alert Manager has **no absence/dead-man detection** — nothing notices a client that stops sending. Options: (a) accept the gap for now, (b) later add a small server-side checker (cron/analytics-engine analyzer that alerts when a host's heartbeat is stale). Flag this explicitly — it's the classic push-monitoring blind spot.
- [ ] **Kernel-log position tracking.** "Errors since last scan" requires a persisted journal cursor (`journalctl --cursor-file`) or last-timestamp; otherwise every scan re-reports the same lines (or misses lines between scans). `dmesg` fallback for non-systemd boxes needs a timestamp watermark. Filter known-noisy sources (e.g. some drivers spew warnings).
- [ ] **Auth provisioning per host.** Each box needs a Bearer token: a service account per host (or per fleet, see Q1) with `alerts:write` scope for its client, created via `tools/user-manager.py create-user` + `add-scope` + `create-token`. Token stored root-only (`/etc/alertagent/config`, mode 600). Tokens are revocable per-host if a box is compromised — argument for per-host accounts.
- [ ] **Delivery failure behavior.** If `alert.d-t.pw` is unreachable, do we drop or spool? Recommendation: tiny local spool (retry queue with cap, e.g. last 50 alerts) flushed next scan — otherwise the outage window is invisible. Curl with `--fail`, timeout, and TLS verification **on**.
- [ ] **Storm behavior server-side.** 10 containers dying at once = 10+ criticals in one scan. Email pathway storm-prevention already aggregates criticals — verify the routing rule for this client goes through a storm-prevention-enabled email pathway, or batch client-side (one alert per scan summarizing all container findings).
- [ ] **Container churn.** Ephemeral/one-shot containers (`docker run --rm`, compose recreations) shouldn't alert as "exited". Need an ignore-list/label convention (e.g. skip containers with label `alertagent.ignore=true`, skip `Exited (0)`).
- [ ] **Permissions.** Reading `docker` requires root or docker-group; `journalctl -k` may require root/`systemd-journal` group. Simplest: run the systemd service as root, read-only operations only.
- [ ] **Portability.** Target: bash + coreutils + curl + docker CLI; use `jq` if acceptable as a dependency, else printf-built JSON (escaping hostnames/log lines correctly is the risk — jq strongly preferred). Detect and degrade: no docker → host-only mode; no systemd → cron + dmesg mode.
- [ ] **Scheduling.** Prefer systemd timer (+ `RandomizedDelaySec` so a fleet doesn't stampede the gateway at :00) over cron; cron fallback documented.
- [ ] **Clock sanity.** Payload `timestamp` must be epoch seconds; a box with broken NTP produces misleading alert times. Cheap check: warn if clock differs wildly from HTTP `Date` response header (optional, low priority).
- [ ] **Config file.** `/etc/alertagent/config` (sourced shell or flat KEY=VAL): gateway URL, token, client name, thresholds (disk warn/crit %, load multiplier, mem %), scan interval, ignore lists, heartbeat interval.
- [ ] **Self-health.** The agent script failing silently is its own blind spot — trap errors and attempt a `warning` alert about itself; systemd `OnFailure=` as backstop.

---

## 4. Open questions — RESOLVED 2026-08-10

- **Q1 — Client naming:** `ALERT_CLIENT` is a **required per-host config value** — operator picks fleet-wide or per-host naming at install time; the agent doesn't hardcode either.
- **Q2 — Heartbeat cadence:** every run (30 min), `level=info` host + docker snapshots.
- **Q3 — Scan interval:** 30-minute cron/systemd timer; all debounce is timestamp-based so the interval can change freely.
- **Q4 — Placement:** `clients/linux-agent/` (script + `alert-agent.env.example` + README), sanitized examples only.
- **Q5 — Shell vs. Python:** bash + `jq` + `curl` (zero-pip portability).
- **Q6 — Thresholds:** disk 90%/95% (re-fire 24h/1h), load 2×/4× cores (5-min avg), memory 90%/95% used — all overridable in the env file.

---

## 5. Implementation phases (tracker)

### Phase 0 — Decisions ✅
- [x] Resolve Q1–Q6 above

### Phase 1 — Agent MVP ✅
- [x] Config file + token handling (600 perms, root-only) — `/etc/alert-agent/alert-agent.env`
- [x] Host checks: disk / load / memory / uptime
- [x] JSON build (`jq -n`) + `POST /alert` sender (curl, TLS verify, 15s timeout, local enum validation)
- [x] State file + transition-based alerting (ok/warn/crit/recovered, timestamp-window re-fire, immediate warn→crit escalation)
- [x] systemd service + timer + cron examples (in README; no installer script)
- [x] Spool/retry for gateway outages (cap 50, 24h TTL; 401 short-circuits the run to avoid account lockout)

### Phase 2 — Docker checks ✅
- [x] Daemon reachability check (debounced critical + recovery notice)
- [x] Per-container state/health/restarts/OOM via `docker inspect` + `docker stats --no-stream`
- [x] Churn ignore rules (`alert-agent.ignore=true` label + `Exited (0)` + never-cached containers)
- [x] Batch container findings into one alert per scan

### Phase 3 — Kernel log watch ✅
- [x] `journalctl -k -p warning` with `--cursor-file`; `dmesg` epoch-watermark fallback
- [x] Batch + truncate (`KERNEL_MAX_LINES`, default 40) into single alert per scan
- [ ] Noise filter list (deferred — add if a chatty driver shows up in practice)

### Phase 4 — Server side & rollout (user-run)
- [ ] Register client + service account/token per host (`tools/user-manager.py`) — steps in agent README
- [ ] Routing rule for the chosen client (email w/ storm prevention for criticals, file pathway always)
- [x] Update `docs/CLIENTNODE.md` with agent pointer
- [ ] Deploy to first box (alert-vps itself is a good candidate — it can report on its own Alert Manager containers), observe a week, tune thresholds

### Phase 5 — Later / optional
- [ ] Dead-man switch: server-side staleness checker for heartbeats (analytics-engine analyzer or cron)
- [ ] Kernel noise filter list; self-health alert on agent failure (systemd `OnFailure=` documented instead)
- [ ] SMART/disk-health, temperature sensors (`hardware`/`environmental` categories) if desired
