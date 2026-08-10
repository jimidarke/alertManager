# Alert Manager Overhaul — Web Portal, RBAC, Dashboard, Email v2

> **Status:** Approved 2026-07-20 — planning document only, implementation not yet started.
> Phase 0 (emergency partition repair) can and should be run immediately; it is SQL-only with no repo changes.

## Context

The alert pipeline (MQTT → router → pathways) has been stable for months and the log portal works with clean per-client isolation. Goal: expand usage and get out of the CLI where possible —

1. **Modular RBAC** — users → roles → scopes; a scope = (client, permissions). `logs:read` now; `alerts:write`/`thresholds:write` accommodated for later. Support agents can span multiple clients. `master_admin` role (owner only) gets web-based administration.
2. **Forms-based login** (Flask) for browsers; machines keep Basic/Bearer.
3. **Dashboard landing page** — per-level count cards over a selectable timeframe (1h/6h/12h/**24h default**/1w/2w/1m/forever→90d), plus a **critical-only** table (client, source, truncated description, modal with full details), scoped to the user's permissions.
4. **Log viewer optimization** — keep weekly files + dropdown + live tail; remove row caps, lazy loading, full-content text search.
5. **Email v2** — professional template with client name; record every send attempt with response status.
6. **User management CLI-first**, web UI wraps the same module (CLI = fallback).

## Verified findings (exploration + live prod checks, 2026-07-20)

- `src/http_gateway.py`: single 930-line Flask app on the **dev server**; Basic auth with **single-round salted SHA-256**; tokens plaintext/unused; `rate_limit`/`failed_attempts` in users.json enforced nowhere; no sessions/CSRF/page-templating (log viewer HTML is an inline Python string with a latent `{__version__}` bug). Verdict: restructure, don't rewrite semantics.
- Authorization seam: `is_log_file_authorized()` (`src/http_gateway.py:175`) — admin→all; `ops/`→admin; `file/{client}/`→clientname match. Port this onto scopes.
- Viewer limits to remove: JS `MAX_DOM_LINES=5000`, warn at 10k; server truncates initial load to 10k lines / 50MB; text-mode `f.tell()` offsets + 1MB cap can split lines mid-way.
- Log line format (backfill parser contract): `{epoch10} [{level}] [{category}] {source} [{client}]: {title} - {message}`, single-line sanitized (`" ↵ "`, message truncated 500).
- **Postgres is live in prod** (postgres:16-alpine, healthy 5 months, profile `analytics`), `alerts` table weekly-partitioned with good indexes.
- **PROD BUG:** only 8 weeks of partitions were ever created; nothing schedules more. Every insert since **2026-01-11** fails (`no partition of relation "alerts" found`); failed batches are dropped. 115,699 rows, newest Jan 11. Data safe in file logs.
- **Healthcheck bug:** Dockerfile healthcheck looks for `/data/logs/status.json` which nothing writes → several services permanently "unhealthy" (false alarm).
- Email: Graph sendMail 202=queued-not-delivered; `requests.post` has **no timeout, no retry**; **successful sends produce no record anywhere** (failure-only logging); storm aggregation reports success at queue time so aggregated failures never reach router stats; aggregated template context lacks `client`; default `email_critical` uses a dated hardcoded HTML body.
- `src/pathways/database_pathway.py` is orphaned (broken import, unregistered, invalid `ingestion_source`) — delete.
- No pytest harness exists; only curl shell tests in `tools/tests/`.
- Best practices (researched): argon2id hashing + CSRF + stable SECRET_KEY + login rate limiting; separate permission (*what*) from scope (*where*); never retry a Graph 202 (duplicates mail), retry only 429/5xx honoring `Retry-After`, record `request-id`; email HTML = table-based fully-inline CSS, dark-mode aware (Gmail auto-inverts); log viewers should search in-memory data, not DOM, and lazy-render.

## Decisions locked with owner

| # | Decision |
|---|---|
| 1 | Dashboard reads **Postgres** `alerts` table; Postgres becomes core (leaves `analytics` profile). Partition bug fixed + automated. |
| 2 | Users/roles/scopes in **Postgres**; argon2id hashing; ~60s credential cache in gateway so node ingestion survives DB blips. SHA-256 hashes can't migrate → all passwords reset (tiny user count). |
| 3 | v1 admin web area = **user/scope management only** (see Deferred section for the rest). |
| 4 | **Backfill last 90 days** of file logs into Postgres. |
| 5 | User management **CLI-first** (python, retires bash `http-auth-manager.sh`); web admin wraps the same `userstore` module. |
| 6 | Server-rendered Jinja + vanilla JS/CSS, no SPA/build step; keep the dark theme. |
| 7 | Dashboard table shows **critical only** (`error` is normalized to `critical` at ingestion — 5 canonical levels in DB). |

**Reconciliation calls made during planning:** `src/gateway/` blueprint package (not flat modules); hand-rolled signed-cookie sessions (not Flask-Login) since the real work is one Principal across session/Basic/Bearer; dashboard API uses a window enum matching the dropdown; admin UI is server-rendered forms calling the store directly (no JSON admin API in v1); email template redesign ships with email tracking (v1.7.0) before the gateway cutover; dashboard uses direct indexed queries, not the hour-granular matview.

---

# Implementation phases

| Phase | Content | Version | Depends on |
|---|---|---|---|
| 0 | Emergency partition repair (SQL only, ship immediately) | — | — |
| 1 | Migration runner, partition automation, db-logger hardening, healthchecks, postgres→core, pytest scaffold | 1.6.0 | 0 |
| 2 | Email v2: send tracking, timeout/retry, new templates | 1.7.0 | 1 |
| 3 | 90-day backfill (one-off script run) | — | 1 |
| 4 | Gateway v2: package restructure, gunicorn, user store, sessions/RBAC/CSRF, CLI, login + dashboard + viewer rework + admin UI | 2.0.0 | 1 (not 2/3) |

Phases 2/3 are order-independent. All docker/compose commands are run **manually by the owner** (per CLAUDE.md); each phase ends with exact commands.

---

## Phase 0 — Emergency prod fix: missing partitions (no repo change)

Owner runs on VPS: a `DO $$` block that loops Mondays from `2026-01-05` (ISO week of the last good partition) through `current week + 8`, creating `alerts_YYYY_wNN` partitions via `CREATE TABLE %I PARTITION OF alerts FOR VALUES FROM (week) TO (week+7)` when missing. Same naming/boundary mechanism as `sql/init/02-partitions.sql` so `drop_old_alert_partitions()` retention keeps working.

```bash
cd /opt/docker/alert-manager
docker exec -i alert-postgres psql -U alertmanager -d alertmanager <<'SQL'
DO $$
DECLARE
  v_week DATE := DATE_TRUNC('week', DATE '2026-01-05')::DATE;
  v_end  DATE := (DATE_TRUNC('week', CURRENT_DATE) + INTERVAL '8 weeks')::DATE;
  v_name TEXT;
BEGIN
  WHILE v_week <= v_end LOOP
    v_name := 'alerts_' || EXTRACT(ISOYEAR FROM v_week) || '_w' || LPAD(EXTRACT(WEEK FROM v_week)::TEXT, 2, '0');
    IF NOT EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=v_name) THEN
      EXECUTE format('CREATE TABLE %I PARTITION OF alerts FOR VALUES FROM (%L) TO (%L)',
                     v_name, v_week, v_week + 7);
      RAISE NOTICE 'created %', v_name;
    END IF;
    v_week := v_week + 7;
  END LOOP;
END $$;
SQL
```

Verify: partition count for 2026; send a test alert; `SELECT max(timestamp) FROM alerts` advances; `docker logs alert-database-logger` shows no more CheckViolation.

## Phase 1 — DB foundation (v1.6.0)

### 1.1 Migration runner — `src/db_migrate.py` (new) + `sql/migrations/`
Dead-simple versioned runner (stdlib + psycopg2, no Alembic): `schema_migrations(version, name, applied_at)` table, `pg_advisory_lock` to serialize, applies sorted `sql/migrations/NNNN_*.sql` each in its own transaction, fail-fast exit 1 on error. Flags `--dry-run`, `--dsn`. DSN from `POSTGRES_*` env (names already used by `src/database_logger.py`).
`sql/init/` stays frozen as the empty-volume bootstrap; migrations are the source of truth for everything new. Owner runs the runner as a one-off container before restarting affected services:

```bash
docker run --rm --network WebApps --env-file .env -e POSTGRES_HOST=postgres \
  alert-router:latest python3 src/db_migrate.py
```

### 1.2 `sql/migrations/0001_partition_management.sql`
`ensure_alert_partitions(p_from DATE, p_weeks_ahead INT DEFAULT 8)` — idempotent, returns (partition_name, status), keeps `alerts_YYYY_wNN` ISO naming; runs a repair call `('2026-01-05', 8)` at the end (no-op if Phase 0 ran).

### 1.3 database-logger hardening — `src/database_logger.py`
- **Partition automation lives here** (sole writer, dies without partitions, already owns a PG pool): startup gate calls `ensure_alert_partitions` (fail-fast `sys.exit(1)` on error); every 24h in the existing 60s stats loop: `ensure_alert_partitions` + `drop_old_alert_partitions()` + `refresh_alert_summary()`.
- MQTT `clean_session=False` (stable client_id already) so QoS1 alerts queue across restarts.
- Batch failure isolation: on batch error → rollback → `ensure_alert_partitions` → retry batch once → on second failure fall back to row-by-row; individually-failing rows logged at ERROR **with full payload** and counted (loud, not silent).
- Heartbeat: touch `/data/logs/ops/alert-database-logger.heartbeat` each healthy cycle. New shared helper `src/utils/heartbeat.py` (`beat(path)`, `check(path, max_age=150)`).

### 1.4 Healthchecks (compose-level; delete the broken Dockerfile HEALTHCHECK)
- alert-router / file-logger / database-logger / analytics-engine: heartbeat-file freshness via `heartbeat.check()` (path from `HEARTBEAT_FILE` env; router `beat()`s after each 60s status publish in `run()`).
- http-gateway: keep `curl -f localhost:8080/health` (`/health` gains `db`/`mqtt` detail but stays 200 on DB blip — the gateway must not restart-loop when PG hiccups).
- mqtt-broker/postgres: keep existing checks.

### 1.5 Compose: Postgres becomes core
Remove `profiles: [analytics]` from `postgres` and `database-logger` (analytics-engine stays profiled). Add `POSTGRES_*` env to `alert-router` (for Phase 2 email log; fail-open, no hard dependency). Dockerfile: `COPY sql/ ./sql/`, `COPY tools/*.py ./tools/`.

### 1.6 pytest scaffold
`requirements-dev.txt` (pytest, pytest-mock; runtime requirements untouched), `tests/` at root: `test_partition_math.py`, `test_heartbeat.py`; DB-touching tests gated on `AM_TEST_DSN` (scratch postgres via docker). Shell tests in `tools/tests/` remain the integration layer and must keep passing.

### 1.7 Rollout
`git pull` → `docker-compose build --no-cache alert-router` → one-off migrate container → `docker-compose --profile primary up -d` (+ analytics-engine). Verify: all containers healthy (first time ever), `schema_migrations` populated, inserts flowing.

## Phase 2 — Email v2: tracking + hardening + templates (v1.7.0)

### 2.1 `sql/migrations/0003_email_log.sql`
Single unpartitioned table (volume is trivial):
`email_log(id, attempted_at epoch, pathway, aggregated bool, alert_count, level, clients text[], recipients text[], subject, status CHECK in ('accepted','failed'), http_status, error(trunc 2000), graph_request_id, duration_ms, retries)` + index on `attempted_at DESC`.
`status='accepted'` deliberately means Graph-202-queued, not delivered (Graph has no delivery webhook).

### 2.2 `pathways/email_log.py` (new) — `EmailLogWriter`
Fail-open psycopg2 writer: lazy single connection, `connect_timeout=3`, autocommit; on failure log ERROR, drop connection, **never raise**. *Deliberate deviation from fail-fast, justified:* email is the last-resort notification channel — a tracking-DB outage must not become a notification outage. Failures stay loud in ops logs.

### 2.3 `pathways/email_pathway.py` hardening
- `timeout=(5, 30)` on both Graph POSTs.
- Extract `_post_sendmail() -> GraphResult{ok, http_status, error_text, request_id, duration_ms, retries}` shared by immediate + aggregated paths. Retry only 429/5xx, max 2 retries, honor `Retry-After` (cap 30s) else 2s/8s backoff; **never** retry after 202; never retry other 4xx.
- `record(...)` every attempt (including MSAL token failures, `http_status=NULL`) via `EmailLogWriter`.
- Surface async/storm failures: `base_pathway` gains `async_errors` counter; email pathway increments on timer-thread flush failures; `alert_router.publish_service_status` merges it into `pathway_errors` — storm-send failures finally appear on the status topic.
- Aggregated context gains `client` (single client or None) + `clients` (sorted unique list).
- Delete orphaned `src/pathways/database_pathway.py`; drop its CLAUDE.md mention.

### 2.4 Template redesign — `src/templates/emails/`
New: `alert_default.html.j2` (single), `alert_default_aggregated.html.j2` (storm), `components/alert_macros.html.j2` (shared macros: `level_badge`, `chip`, `details_table`, `metadata_table`, `client_banner`).
- 600px table-based, hand-inlined CSS (do **not** run premailer over these — it would strip the dark-mode block); `color-scheme` meta + `@media (prefers-color-scheme: dark)` class-keyed overrides; light base palette per THEME_SPECIFICATION.md level colors, chosen to survive Gmail auto-inversion; text wordmark, no remote images.
- Structure: level-colored top border → header + **prominent client pill** (`CLIENT: REVYSMC`) → level/category/source chips → title + `pre-wrap` message → details card (Mountain time **and** raw epoch, source, category, client, ingestion) → metadata key/value table (omitted when empty) → footer with version.
- Aggregated: "ALERT STORM — N critical alerts" header, client pill or "Multiple clients: a, b", summary card (window/duration/sources/time range), zebra per-alert table.
- `email_pathway.py`: default `template_file='emails/alert_default.html.j2'` and aggregated default likewise (bare `email_critical` config gets the new look with zero config change); always init Jinja; `datetime_format` filter pinned to `America/Edmonton` (no container-TZ dependence); `version` in context; **delete** `_create_html_body`/`_create_default_aggregated_body` (render failure → ERROR + return False, per no-failsafes rule; keep `_create_text_body` for `html_format:false`). HTML-only body (Graph takes one contentType).
- Rework `revysmc_*.html.j2` onto the shared macros, keeping RSC branding.
- New preview harness `tools/tests/render_email_preview.py` — renders any template with canned single/storm alert dicts to stdout for browser inspection without sending mail.

### 2.5 Tests + rollout
`tests/test_email_retry.py` (decision matrix: 202→no retry, 429 honors Retry-After, 500→backoff, 400→no retry), `tests/test_email_log_writer.py` (fail-open). Rollout: build, migrate, `up -d alert-router`. Verify: send test critical → row in `email_log`; stop postgres, send again → email still arrives + ERROR logged; template previews checked in Gmail (light+dark), Outlook, iOS Mail; storm test with 3 criticals from 2 clients → "Multiple clients" mail.

## Phase 3 — 90-day backfill (one-off)

`tools/backfill_file_logs.py` (new): parses `file/{client}/{date}.log` weekly files + flat `file/{date}.log`.
- Regex parser (importable, unit-tested): `^(\d{10}) \[(level)\] \[(category)\] (source) \[(client)\]: (title) - (message)$`; first `" - "` split ambiguity accepted; **folder name wins** over parsed client; flat-file rows → `client=NULL`; invalid level/category → counted, logged with file:line, skipped; sanitization artifacts (`" ↵ "`, truncation) kept as-is.
- Insert: batched `execute_values`, `ingestion_source='manual'` (passes CHECK; marks backfilled rows forever), `metadata={"backfilled_from": "<file>"}`.
- Guards: `--since-days 90` default; skip anything ≤ `max(timestamp)` of non-manual rows; abort if backfilled rows already exist unless `--force`; `--dry-run` prints per-file counts + samples.
- Run as one-off container with `data/logs` mounted **ro**; then `refresh_alert_summary()`. Test: `tests/test_backfill_parser.py` golden lines (clientless `[]`, `↵`, truncated, brackets in title, spaces in source, garbage→None).

## Phase 4 — Gateway v2 (v2.0.0, breaking: passwords reset, users.json retired, HTTPS-only)

### 4.1 Identity schema — `sql/migrations/0002_identity.sql`
Epoch `BIGINT` for all new time columns (project convention; `alerts.timestamp` stays TIMESTAMPTZ for partitioning).
- `users(id, username unique lowercase-checked, password_hash argon2id, status active|disabled, default_client, must_change_password, failed_attempts, locked_until, expires_at, last_login_at, created_at, updated_at)`
- `roles(id, name unique, description, is_system)`; `user_roles(user_id, role_id)`
- `role_scopes(role_id, client, permission)` — permission CHECK in `('*','logs:read','alerts:write','thresholds:write','users:manage')`; client `'*'` = all. Extending the CHECK later is a deliberate migration (fail-fast culture).
- `auth_tokens(name, token_sha256, user_id, status, expires_at, last_used_at)` — hashed Bearer tokens tied to users (replaces plaintext tokens.json).
- `auth_audit(at, event, username, source_ip, detail jsonb)` + indexes — login_success/failed, lockout, user/role/scope/token mutations.
- Seed: `master_admin` system role with `('*','*')` scope.
- Dropped dead fields (`rate_limit` per-user hourly): never enforced; lockout replaces the intent.

### 4.2 `src/userstore/` (new, framework-free: psycopg2 + argon2-cffi) + CLI
Single owner of all account/role/scope/token/audit operations; imported by gateway, CLI, and migration script; no Flask imports.
- `passwords.py`: `hash_password/verify_password/needs_rehash` (argon2id defaults).
- `store.py`: `UserStore.from_env()`; account CRUD; `verify_login()` (enforces status/expiry/**lockout: 5 fails → 900s**, audits, resets on success); `get_principal(username)` (one join users→roles→scopes); `verify_token()`; role/scope CRUD; `create_token()` returns plaintext once (`amt_` + 43 chars); `audit()`/`recent_audit()`.
- `PrincipalData` (frozen dataclass): `roles`, `scopes: {client: frozenset(perms)}`, `default_client`; `is_master_admin()`, `has_permission(perm, client)`, `visible_clients(perm)`.
- **CLI `tools/user-manager.py`** (canonical; retires `tools/http-auth-manager.sh`): `init-admin` (refuses if a master_admin exists; prints generated password once), `create-user/set-password/list/show/disable/enable/delete`, `create-role/grant-role/revoke-role/add-scope/remove-scope`, `create-token/revoke-token`, `audit`. Run via `docker exec -it alert-http-gateway python3 tools/user-manager.py ...` or a one-off `docker run` if the gateway is dead — this is the fallback when the web UI is down.
- **Migration `tools/migrate-users-json.py`** (one-off): maps current users.json — `jimi`→master_admin (+must_change_password); nodes→`<client>-node` role `{(c,alerts:write),(c,logs:read)}` + `default_client`; viewers→`<client>-viewer` role `{(c,logs:read)}`; `expires` ISO→epoch; prints one-time credential table (owner updates sender machines + hands out passwords; drop the `testing` account).

### 4.3 Gateway package — `src/gateway/` (replaces `src/http_gateway.py`, deleted)
```
src/gateway/
  __init__.py   create_app()          wsgi.py   app = create_app()   (gunicorn)
  config.py     GatewayConfig: SECRET_KEY from GATEWAY_SECRET_KEY env (required ≥32 chars else exit 1),
                POSTGRES_*, MQTT, poll interval, session lifetime
  db.py         ThreadedConnectionPool + UserStore in app.extensions
  mqtt.py       MqttPublisher (port of forward_to_mqtt)
  principal.py  Principal + resolve_principal + decorators (require_auth / require_permission / require_master_admin)
  auth/         routes.py (/login GET+POST, /logout POST, /api/whoami), cache.py (CredentialCache),
                ip_throttle.py (20 fails/10min/IP → 429)
  api/          routes.py (/health, POST /alert, POST /test, /docs, /docs/<f>)
  logs/         routes.py (/logs page, /api/logs/list, /api/logs/tail, /api/logs/chunk), files.py (authz)
  dashboard/    routes.py (/dashboard page, /api/dashboard/summary|critical)
  admin/        routes.py (server-rendered /admin/users* forms)
  templates/    layout.html, login.html, dashboard.html, logs.html, admin/{users_list,user_form,password_result}.html
```
`src/static/` stays where it is (volume-mounted ro → JS/CSS updates land via `git pull` + hard refresh, no rebuild; templates are baked into the image → rebuild required). URLs preserved exactly: `/health`, `/alert`, `/test`, `/docs`, `/static/...`, `/logs`, `/api/logs/list|tail`. New: `/login`, `/logout`, `/dashboard`, `/admin/...`, `/api/whoami`, `/api/dashboard/*`, `/api/logs/chunk`.

### 4.4 Auth subsystem
- **Hand-rolled sessions on Flask's signed cookie** (not Flask-Login): payload `{uid, u, iat}`; `HTTPONLY`, `SECURE`, `SameSite=Lax`, 12h sliding lifetime. `ProxyFix(x_for=1, x_proto=1)` — required behind nginx-proxy for real client IPs (audit/throttle) and Secure cookies. **Consequence: remove the direct `8080:8080` host publish** (spoofable XFF otherwise); all traffic via `https://alert.d-t.pw`.
- **CSRF**: Flask-WTF `CSRFProtect`, `api` blueprint exempt (it never accepts session cookies, so nothing to forge); login form + logout + admin forms carry tokens (layout embeds token in a meta tag).
- Per-blueprint auth policy: `api` = Basic/Bearer only; `logs` = session **or** Basic/Bearer (keeps curl/shell-test workflows); `dashboard` = session only; `admin` = session + master_admin only (CLI is the API-less fallback); anonymous HTML requests → 302 `/login?next=` (same-origin-validated `next`).
- **`POST /alert` authorization change (breaking, → CLIENTNODE.md):** effective client = payload `client` else `principal.default_client` else `''`; requires `has_permission('alerts:write', effective_client)`; clientless requires master_admin. Closes today's any-node-can-spoof-any-client hole.
- **CredentialCache** (`FRESH_TTL=60`, `STALE_GRACE=300`): fresh hit → constant-time compare of per-boot-salted fast hash, no DB/argon2 per node POST; miss → `verify_login` (argon2) → repopulate; DB exception within grace → serve stale + WARNING; beyond → 503 on `/alert`. Invalidated on password change/disable.
- Lockout (store-level, applies to Basic too) + per-IP login throttle (in-memory; correct because single gunicorn worker).
- Log-file authz (`logs/files.py`): `ops/**`→master_admin; `file/{client}/**`→`has_permission('logs:read', client)`; flat/other→master_admin; traversal checks ported verbatim.

### 4.5 Web pages (templates + static)
- `layout.html`: nav (logo, Dashboard, Logs, Admin if master_admin; right: username + role badge, logout POST-form), flash messages, footer `Alert Manager v{{ version }}` (fixes the `{__version__}` bug; version via context processor). `logs.html` ports the inline HTML; `SERVER_POLL_INTERVAL_MS` injected via template var.
- Static: new `base.css` (theme `:root` vars moved from log-viewer.css + nav/cards/forms/tables/modal/badges), `dashboard.css`, `common.js` (fetchWithTimeout, timezone helpers, `timeAgo`, `escapeHtml`), `dashboard.js`; `log-viewer.css` keeps viewer-only rules + search/highlight/progress styles.
- `login.html`: centered card, username/password/CSRF, generic "Invalid username or password" flash; `GET /` → redirect `/dashboard` or `/login`.

### 4.6 Dashboard
**API** (session-only; scope predicate injected server-side from `principal.visible_clients('logs:read')` — never from query params; `client IS NULL/''` rows visible to master_admin only; direct indexed queries, no matview):
- `GET /api/dashboard/summary?window=24h` → `{counts:{critical..debug}, total, since, scope, generated_at}`; SQL `SELECT level, COUNT(*) FROM alerts WHERE timestamp >= to_timestamp(:since) [AND client = ANY(:clients)] GROUP BY level`; missing levels zero-filled.
- `GET /api/dashboard/critical?window=24h&offset=0&limit=50` (max 200) → rows `{id, timestamp epoch, category, title, message FULL, source, client, metadata, ingestion_source}` + `total`; `ORDER BY timestamp DESC`; truncation is client-side.
- Window enum: `1h/6h/12h/24h/7d/14d/30d/all`→seconds, `all`=7,776,000 (90d cap); invalid → 400. DB pool down → 503 + warning banner on the page.

**UI** (`dashboard.html` + `dashboard.js`): header + timeframe `<select>` (persisted `localStorage['dashboard_window']`, default 24h) + "Updated Xs ago"; card grid `repeat(auto-fit, minmax(150px,1fr))`, 5 cards critical→debug, `border-top: 4px solid var(--level-X)`, big count (critical pulses when >0, scroll-links to table); critical table: Time (`timeAgo` + absolute in selected TZ), Client pill (em-dash if null), monospace Source, Alert = bold title + message truncated 120 chars; row click → native `<dialog>` modal (level badge, definition grid incl. raw epoch + ingestion + id, `pre-wrap` full message, metadata table + collapsible JSON `<pre>`; full-screen <768px); "Load more" pagination; empty state "No critical alerts in the selected timeframe"; auto-refresh 60s, skipped when tab hidden; all DB text inserted via `textContent`/escape (untrusted).

### 4.7 Log viewer rework
**Server** (`logs/routes.py`): binary reads with newline-aligned byte offsets (fixes text-mode `f.tell()` fragility + mid-line splits). `?stat=1` → `{file_size}`; forward tail read (`position`, 1MB cap, trim to last `\n`, `+eof`); new `GET /api/logs/chunk?file=F&end=E` backward read (1MB, start aligned past first `\n`, `eof: start==0`). **Delete the `lines*100` (10k) truncation**; 50MB guard becomes a client-side stop ("Older entries beyond 50MB not loaded"). Auth/path/403 semantics unchanged.
**Client** (rewrite `log-viewer.js`, single vanilla file): `LogStore` (chronological array `{raw, ts, level}`, level parsed once at ingest; `matchIndexes(filters, query)` over the **full store**), `Renderer`, `Search`, `Poller`. Initial sequence: stat → start live polling from `position=file_size` immediately → backward-chunk backfill loop with progress % — newest lines render instantly, history streams in behind. **Chunked rendering via IntersectionObserver bottom sentinel** (render newest 500 matches, append 500 older on scroll) — chosen over true virtual scrolling because newest-at-top prepends + wrapped variable-height lines fight fixed-row virtualization. `MAX_DOM_LINES`, prune loop, 10k warning banner all deleted. Search box in filter bar: debounced 250ms, case-insensitive substring over `raw` for every stored line, ANDed with level filters, `<mark>` highlights (applied post-escape), counter "312 matches / 48,203 lines", auto re-runs as chunks arrive. Filter/search changes re-render from the store (no DOM-walking). Unchanged: 10s poll, pause/resume, 403 auto-pause, weekly-file dropdown (`/api/logs/list` untouched), ops toggle, timezone selector (moves to common.js; TZ change re-renders from store).

### 4.8 Admin UI (master_admin only, server-rendered, CSRF on every POST)
Routes: `/admin/users` (table: username, roles, scopes, status pill, last login), `/admin/users/new`, `/admin/users/<u>/edit`, `POST .../disable|enable`, `POST .../reset-password`. Form: username `^[a-z0-9_-]{3,32}$` (create only); role checkboxes from `list_roles()` (master_admin gets red warning); repeatable scope rows — client `<input list>` datalist (distinct clients from alerts ∪ existing scopes) + permission checkboxes (v1: logs:read pre-checked); tiny inline JS for "add scope" row. Passwords always auto-generated (`secrets.token_urlsafe(12)`), shown **once** on `password_result.html` (`Cache-Control: no-store`, copy button). Guard rails: can't disable/de-admin your own session. No delete in v1 (disable covers it). Handlers call `UserStore` directly — same code path as CLI.

### 4.9 WSGI + compose
**gunicorn, 1 worker × 8 gthread threads, timeout 60** (`gateway.wsgi:app`, `--chdir /app/src`): I/O-bound workload; single process keeps MQTT client, credential cache, and IP throttle coherent; threads cover polling concurrency.
`requirements.txt` += `argon2-cffi>=23.1`, `Flask-WTF>=1.2`, `gunicorn>=21.2`.
Compose http-gateway: gunicorn command; **remove `ports: 8080:8080`**; add `POSTGRES_*`, `GATEWAY_SECRET_KEY=${GATEWAY_SECRET_KEY}`; `depends_on: postgres: service_healthy`. `.env`/`.env.example` gain `GATEWAY_SECRET_KEY` (`openssl rand -hex 32`).

### 4.10 Phase 4 rollout (order matters)
```bash
cd /opt/docker/alert-manager && git pull            # v2.0.0
openssl rand -hex 32                                # → .env GATEWAY_SECRET_KEY
docker-compose build --no-cache alert-router
docker run --rm --network WebApps --env-file .env -e POSTGRES_HOST=postgres \
  alert-router:latest python3 src/db_migrate.py
docker run --rm --network WebApps --env-file .env -e POSTGRES_HOST=postgres \
  -v /opt/docker/alert-manager/data/config:/data/config:ro \
  alert-router:latest python3 tools/migrate-users-json.py     # SAVE printed credentials
docker-compose --profile primary up -d
```
Then update sender machines (price-checker, bv-sync, sync-client, yamacam) with new passwords; hand new passwords to john/revy; delete users.json/tokens.json after confirmed cutover. Hard-refresh browsers (Ctrl+Shift+R).
Risks: missing SECRET_KEY → gateway exits by design; anything hitting `http://host:8080` directly breaks (audit sender configs first — they should use `https://alert.d-t.pw`).

---

## Testing & verification

**Unit (pytest, established in Phase 1):** partition math, heartbeat, email retry matrix, email-log fail-open, backfill parser goldens, argon2 round-trip, `has_permission` matrix (master/multi-client agent/node), log-path authz table, credential cache (fresh/expiry/stale-grace/invalidation), gateway routes with FakeUserStore + mocked MQTT (401/403/inheritance/login flow/CSRF/API-ignores-cookies), store CRUD + lockout (gated on `AM_TEST_DSN`).

**Integration/manual per feature:**
- *Login/nav*: logged-out `/dashboard` → `/login?next=`; wrong password → flash, no cookie; `next=https://evil.com` rejected; Basic `POST /alert` unaffected; logout kills session; 5 bad passwords → lockout message; audit rows via CLI.
- *Dashboard*: `window=bogus` → 400; send critical → card increments + row appears + modal shows full message/metadata; timeframe survives reload; `docker stop alert-postgres` → 503 banner, page still renders (and email still sends, tracking ERROR logged).
- *Scope leaks (critical)*: as clientA-scoped user, summary counts == `psql` count `WHERE client='clientA'`; every `/api/dashboard/critical` row has clientA; crafted `?client=clientB` has no effect (param doesn't exist); `file/clientB/...` tail → 403 + auto-pause.
- *Viewer*: largest weekly file loads fully (line count == `wc -l`, no 10k truncation) with progress %; search finds strings from the oldest part before scrolling; search+level filters compose; live lines keep prepending under active search; TZ switch re-renders.
- *Admin*: create scoped user → password shown once only; new user sees only their client; disable → bounced to login; self-disable blocked; POST without CSRF → 400.
- *Email*: preview harness for all 4 templates in light+dark; live single + storm (3 criticals, 2 clients → "Multiple clients"); verify in Gmail/Outlook/iOS Mail; `email_log` rows for accepted + failed; broken template name → ERROR, nothing sent.
- *Ops*: all containers report healthy; kill database-logger mid-stream → QoS1 catch-up on restart (clean_session=False); backfill `--dry-run` counts ≈ `wc -l` of parsed files.

## Docs & version management
Per phase: `./tools/bump-version.sh minor` (1.6.0, 1.7.0) then `major` (2.0.0); CHANGELOG.md sections; git tag + push per CLAUDE.md. Update: `CLIENTNODE.md` (credential rotation, `client` validated against write scopes, HTTPS-only), `docs/HTTP_GATEWAY.md` (new endpoints/pages), `docs/AUTH.md` (rewrite: sessions/RBAC/argon2/CLI), `CLAUDE.md` (auth model, gateway architecture, healthchecks, postgres-as-core, email tracking), `tools/README.md`.

## Deferred to a later plan (per owner)
Service status page (container health, pathway errors, partition status, last-alert per client) · routing-rules & pathway config web editor · email pathway settings UI · email send-history web viewer (recording ships now in Phase 2) · API rate limiting (dropped per-user `rate_limit` fields; lockout+throttle cover the security intent) · `alerts:write`/`thresholds:write` UI features (schema supports them from day one).

## Critical files
Modify: `src/database_logger.py`, `pathways/email_pathway.py`, `pathways/base_pathway.py`, `src/alert_router.py` (heartbeat + pathway_errors merge), `docker-compose.yml`, `docker/Dockerfile`, `requirements.txt`, `src/static/js/log-viewer.js` (rewrite), `src/static/css/log-viewer.css`, `src/templates/emails/revysmc_*.j2`.
Create: `src/db_migrate.py`, `sql/migrations/0001–0003`, `src/utils/heartbeat.py`, `pathways/email_log.py`, `src/templates/emails/alert_default*.j2` + `components/alert_macros.html.j2`, `tools/backfill_file_logs.py`, `tools/render_email_preview.py`, `src/userstore/*`, `src/gateway/*` (+ templates), `tools/user-manager.py`, `tools/migrate-users-json.py`, `src/static/{css/base.css,css/dashboard.css,js/common.js,js/dashboard.js}`, `requirements-dev.txt`, `tests/*`.
Delete: `src/http_gateway.py` (Phase 4), `src/pathways/database_pathway.py`, `tools/http-auth-manager.sh`, hardcoded email HTML builders.
