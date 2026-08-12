# URL Monitor Service — Implementation Plan

> **Status:** Implemented in v3.2.0 (2026-08-12). This file is retained as the design record.

## Context

Alert Manager runs on a VPS alongside a fleet of webservers (Jimi's own and clients'). We want first-party awareness of website uptime and latency without a third-party monitoring product. A new standalone service polls a server-configured list of URLs (no UI), does a headers-only HTTP GET, and emits alerts into the existing pipeline. Each monitored URL carries a `client` param so alerts route/log per client like any other alert.

**Decisions made with the user (2026-08-12):**
- **Ingestion**: internal MQTT publish to `alerts/input` (same pattern as feed-aggregator) — no gateway credentials, works even if http-gateway is down.
- **Alerting semantics**: state-change + recovery, **plus a reminder alert every 12 hours while a URL remains unhealthy** so long outages aren't forgotten. Healthy polls fire `debug` each cycle (mostly invisible).
- **Check method**: GET with `stream=True`, read status + headers only, never the body. Per-URL `expected_status` (default 200) and `follow_redirects` (default false — a 301 fails unless expected or followed).
- **Levels/category**: category `application` for all. Healthy → `debug`; slow or wrong status → `warning`; timeout / DNS failure / connection refused / TLS error → `critical`; recovery → `notice`.
- Thresholds (expected status, max response time default 200ms, timeout) configurable server-side, globally with per-URL overrides.

## Architecture

New service `src/url_monitor.py`, modeled directly on `src/feed_aggregator.py` (154 lines — read it first), run as another container from the shared `alert-router:latest` image with a different `command:`. It is an alert *producer* like feed-aggregator, not a pathway.

```
url_monitor.py (APScheduler interval job per URL or one sweep job)
  → check_url() via requests (already in requirements.txt)
  → state machine per URL (OK / DEGRADED / DOWN)
  → build_alert_payload() (src/utils/payload_builder.py)
  → MQTTPublisher.publish_alert() → alerts/input → alert-router → pathways
```

## Components

### 1. `src/url_monitor.py` (new, main service)

Follow feed_aggregator's skeleton: `main()` with `--config`, SIGTERM/SIGINT handler, class `UrlMonitor` with `start()`/`stop()`, `BackgroundScheduler`, `while self.running: time.sleep(1)` loop.

Improvements over feed_aggregator (follow `src/analytics_engine.py` patterns):
- **Heartbeat**: `src/utils/heartbeat.py` — `heartbeat.beat(os.getenv('HEARTBEAT_FILE', ...))` in the main loop, so compose can healthcheck with `heartbeat.check(path, max_age=150)`.
- **Config loading**: `src/utils/config_loader.py`'s `load_config()` is hardcoded to require the `feed_aggregator` key — do NOT reuse it as-is. Either generalize it (`load_config(path, section, required_keys)`) or write a small validated loader inside `url_monitor.py`. Per project conventions ("do not implement failsafes"), invalid/missing config → log critical and exit(1).

**Check logic** (`check_url(url_cfg) -> CheckResult`):
```python
r = requests.get(url, timeout=timeout_seconds, allow_redirects=follow_redirects,
                 stream=True, headers={"User-Agent": f"alertManager-url-monitor/{__version__}"})
elapsed_ms = r.elapsed.total_seconds() * 1000   # time to headers, body never read
status = r.status_code
r.close()
```
Classification:
- `requests.Timeout` / `ConnectionError` / `SSLError` / DNS failure → **DOWN** (critical). Distinguish the failure kind in the message/metadata (`failure: timeout|dns|refused|tls|connection`).
- HTTP status != `expected_status` → **DEGRADED** (warning), status in message.
- Status OK but `elapsed_ms > max_response_ms` → **DEGRADED** (warning).
- Otherwise → **OK** (debug).

**State machine** (per URL, in-memory dict keyed by URL name):
- Track `state`, `since` (epoch), `last_alert_at` (for the 12h reminder).
- Transitions fire alerts:
  - OK→DEGRADED: warning. OK→DOWN / DEGRADED→DOWN: critical. DOWN→DEGRADED: warning ("responding again but degraded").
  - any-unhealthy→OK: **notice** recovery alert with outage duration in message/metadata.
  - unchanged unhealthy state: re-fire the state's alert if `now - last_alert_at >= reminder_interval_seconds` (default 43200 = 12h), title prefixed "STILL DOWN"/"STILL DEGRADED", metadata includes `down_since`, `duration_seconds`.
  - unchanged OK: `debug` alert each poll (the invisible heartbeat the user asked for).
- State is in-memory only: on restart everything is treated as first-seen; first poll of an unhealthy site fires a fresh transition alert. Acceptable and simple — note it in docs.

**Payloads** via `build_alert_payload(level, category, title, message, source, client, metadata)`:
- `category="application"`, `client` = per-URL config value, `source = f"url-monitor/{check_name}"`.
- `metadata`: `{url, status_code, response_ms, threshold_ms, expected_status, state, previous_state, failure, duration_seconds (for recovery/reminders)}` — omit nulls.
- Titles like `"{name} is DOWN"`, `"{name} slow response ({response_ms}ms)"`, `"{name} recovered"`, `"{name} OK"`.

**Scheduling**: one `IntervalTrigger` job per URL (id `url_{name}`, `replace_existing=True`, per-URL `interval_seconds` falling back to a global default of 60). `max_instances=1`, `coalesce=True` so a slow/hung check can't stack. Run one immediate sweep at startup (wrapped in try/except per URL, like feed_aggregator's initial fetch).

### 2. Config — new top-level `url_monitor` section in `data/config/docker.json` (sanitized template) and mirror in `data/config/test.json`

```json
"url_monitor": {
  "enabled": true,
  "mqtt": {"broker": "mqtt-broker", "port": 1883, "topic": "alerts/input"},
  "defaults": {
    "interval_seconds": 60,
    "timeout_seconds": 5,
    "max_response_ms": 200,
    "expected_status": 200,
    "follow_redirects": false,
    "reminder_interval_seconds": 43200
  },
  "urls": [
    {
      "name": "example-site",
      "url": "https://www.example.com/",
      "client": "acme-co",
      "max_response_ms": 500
    }
  ]
}
```
Every `defaults` key is overridable per URL entry. `name` required and unique; `url` and `client` required. Use sanitized placeholder domains/clients in the tracked repo copy (repo is public); real URLs go only into the server-side `/opt/docker/alert-manager` config.

### 3. `docker-compose.yml` — new `url-monitor` service

Copy the `feed-aggregator` block (lines ~338–364) with analytics-engine's healthcheck:
- `image: alert-router:latest`, `container_name: alert-url-monitor`, `command: ["python3", "src/url_monitor.py", "--config", "/data/config/docker.json"]`
- Volumes: `./data/config:/data/config:ro`, `./data/logs/ops:/data/logs/ops:rw`
- Env: `MQTT_BROKER`, `MQTT_PORT`, `LOG_LEVEL`, `TZ`, `HEARTBEAT_FILE=/data/logs/ops/url-monitor.heartbeat`
- Healthcheck: heartbeat-check one-liner (copy from analytics-engine, lines ~367–400)
- `depends_on: [alert-router, mqtt-broker]`, `profiles: ["secondary"]` (fits "content producers"; feed-aggregator is currently intentionally stopped in prod, so bringing `--profile secondary up -d url-monitor` targets just this service)
- Do NOT copy feed-aggregator's hardcoded `API_NINJAS_KEY` pattern — no secrets needed here anyway.

### 4. Routing (server-side config note, not repo code)

Alerts land nowhere without matching `routing_rules`. Debug-level heartbeats should hit only the file pathway (per-client logs); warning/critical flow to email per existing rules. Add/verify a rule in the sanitized `docker.json` template as an example, and remind the user to update the real prod config by hand.

### 5. Tests — `tests/test_url_monitor.py`

Follow existing pytest conventions (mocked MQTT/requests, no live network). Cover:
- Classification: 200-fast→OK, 200-slow→DEGRADED, 404→DEGRADED, Timeout/ConnectionError/SSLError→DOWN, redirect fail vs `follow_redirects`/`expected_status: 301` pass.
- State machine: transition alerts, recovery notice with duration, 12h reminder firing (inject clock), no duplicate alerts within reminder window, debug on every OK poll.
- Payload shape: required fields, category `application`, per-URL client, metadata contents.
- Config: defaults merging, per-URL overrides, missing required key → exit.

### 6. Docs + version

- `docs/DOCKER.md`: add service to the service list + profile table.
- `CLAUDE.md`: short section under the feed-aggregator one ("URL Monitor (Content Source, not a pathway)").
- `CHANGELOG.md` + `./tools/bump-version.sh minor` (new feature → 3.2.0).
- `docs/CLIENTNODE.md`: no payload structure changes, so no update needed.

## Files

| File | Action |
|------|--------|
| `src/url_monitor.py` | new — service (~250 lines) |
| `src/utils/config_loader.py` | generalize or leave alone (loader inline in url_monitor) |
| `data/config/docker.json`, `data/config/test.json` | add sanitized `url_monitor` section |
| `docker-compose.yml` | add `url-monitor` service (profile `secondary`) |
| `tests/test_url_monitor.py` | new |
| `docs/DOCKER.md`, `CLAUDE.md`, `CHANGELOG.md`, `VERSION` | docs/version |

Reuse: `src/utils/payload_builder.py::build_alert_payload`, `src/utils/mqtt_publisher.py::MQTTPublisher`, `src/utils/heartbeat.py`, APScheduler patterns from `src/feed_aggregator.py`, healthcheck pattern from `src/analytics_engine.py` / compose.

## Verification

1. `pip install -r requirements-dev.txt && python3 -m pytest tests/test_url_monitor.py -q` (and full suite `python3 -m pytest tests/ -q` stays green).
2. Local smoke: add a `url_monitor` section to `data/config/test.json` pointing at a real fast URL + a guaranteed-dead one (`https://localhost:9`), run `python3 src/url_monitor.py --config data/config/test.json` against a local mosquitto, watch alert-router route the debug + critical alerts.
3. Server-side (user runs manually — never via Bash tool): `docker-compose build alert-router && docker-compose --profile secondary up -d url-monitor`, then check `/data/logs/ops/url-monitor.{date}.log` and per-client files in `/data/logs/file/{client}/`. Remember prod config at `/opt/docker/alert-manager` must get the real `url_monitor` section by hand.

## Deliberately out of scope (YAGNI)

- No UI/dashboard page, no DB persistence of check history (database-logger already records the alerts), no cross-restart state persistence, no content/keyword checks, no TLS-cert-expiry checks (natural future extension — note in CHANGELOG "future"), no per-URL routing overrides.
