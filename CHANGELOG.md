# Changelog

All notable changes to Alert Manager will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [3.1.1] - 2026-08-09

Documentation-only release. Several guides still described the pre-v2.0.0 world -- a
reachable `:8080` host port, MQTT as an alert-ingestion path, a `routing.exclude` field
that was never implemented, and a `docker-compose build http-gateway` that silently does
nothing. No code, schema, or behaviour changes.

### Fixed
- **`:8080` is gone everywhere** -- `README.md`, `docs/ANALYTICS_QUICKSTART.md`, and
  `src/static/README.md` now point at `https://alert.d-t.pw`. The `http-gateway` container
  has published no host port since v2.0.0; the old URL does not resolve from outside the
  Docker host.
- **`docker-compose build http-gateway` documented as the no-op it is** -- `alert-router`
  is the only service with a build context; every other service reuses
  `image: alert-router:latest`. `CLAUDE.md` and `src/static/README.md` now say to build
  `alert-router`.
- **`routing.exclude` removed from the payload spec** (`CLAUDE.md`) -- it was never
  implemented; `apply_routing_rules()` reads only `override` and `pathways`, and
  `override: true` with an empty/missing `pathways` routes nowhere.
- **`README.md` architecture section rewritten** -- HTTPS `POST /alert` is the sole
  ingestion path, MQTT is documented as the ESP32 LED-sign device bus it actually is,
  compose profiles corrected to `primary`/`secondary`/`analytics`/`deprecated`, the SNMP
  setup block replaced with a deprecation note, and the sample `curl` fixed (it was
  missing the required `timestamp` and used a non-existent `test` category against the
  removed `/alerts/input` path).
- **`docs/CLIENTNODE.md` rewritten** around the three things senders actually get wrong:
  `HTTP 200` means "fields present", not "alert accepted" (invalid values are dropped
  asynchronously downstream); `timestamp` must be a Unix epoch integer; and the `client`
  field is authorization-checked, so an unscoped client is a `403`, not a bad password.
- **`docs/MOBILE_APP_PLAN.md`** -- clarified that the v3.1.0 admin token route is not a
  self-service password grant, so §4.1 (login→token endpoint) is still required.
- **`CLAUDE.md` test count** refreshed to ~1000.

## [3.1.0] - 2026-08-09

Bearer token management moves into the `master_admin` web UI. `UserStore.create_token()`/`list_tokens()`/`revoke_token()` already existed but were reachable only through `tools/user-manager.py` -- provisioning a service account meant creating it in the browser, then shelling into the container to issue its token. No schema change; no behaviour change for tokens already in circulation.

### Added
- **`POST /admin/users/<username>/tokens/new`** -- issue a Bearer token for an existing account. The plaintext token is shown exactly once on the same `Cache-Control: no-store` reveal page that already handles generated passwords.
- **`POST /admin/users/<username>/tokens/<token_id>/revoke`** -- revoke a token. Ownership is verified via `list_tokens(user_id=...)` **before** `revoke_token()` is called, so an admin cannot revoke another account's token by editing the id in this account's URL (`404` if it doesn't belong there) -- the same "never trust a path parameter alone" discipline as the existing disable/edit guard rails.
- **Token issuance at account-creation time** -- the New User form (`/admin/users/new`) takes an "Issue a Bearer token" checkbox plus a name field, so a machine account gets its password and its first token in one step.
- **Token list on the edit page** -- `/admin/users/<username>/edit` now lists every token the account holds (name, status, created, last used) with a Revoke button per active token, plus a small form to issue additional tokens later.
- **"Service account" checkbox** on the New User form -- a purely client-side convenience (`src/static/js/admin-user-form.js`) that unchecks "Require password change" and checks "Issue a Bearer token". Nothing server-side reads `service_account`; unchecking it is a deliberate no-op rather than a bound sync, so fields stay as the admin set them.

### Notes
- Tokens issued through the web UI always have `expires_at` unset (never expire). There is no expiry-picker control, matching `user-manager.py create-token`'s own default when `--expires-at` is omitted.
- A blank token name falls back to `"default"` rather than rejecting the form -- `auth_tokens.name` is `NOT NULL` but is not otherwise a validated field.
- There is no token *delete* route, only revoke (`status='revoked'`), so history stays in `auth_audit`.

## [3.0.0] - 2026-08-07

**Breaking release: the identity store is destroyed and rebuilt. Every password and Bearer token in existence stops working.** Alert data is untouched -- `alerts` and `email_log` are not involved.

Rollout (each stage is a separate command on purpose):
```bash
python3 tools/reset-identity.py                              # type: RESET IDENTITY
python3 src/db_migrate.py
python3 tools/user-manager.py init-admin --username <you>
# then rotate GATEWAY_SECRET_KEY in .env and restart http-gateway
```
Every account, role, scope and token is then recreated by hand. Existing integrations stay dead until re-credentialed.

### Added
- **`GET`/`POST /change-password`** -- self-service password change for browser sessions (`src/gateway/auth/routes.py`, `templates/change_password.html`). Requires the current password (re-verified through `verify_login()`, so wrong guesses count toward lockout), a new password of at least 12 characters, a matching confirmation, and a value different from the current one.
- **`must_change_password` is now enforced.** A `before_request` gate in `create_app()` redirects any flagged browser session to `/change-password` until it is resolved. The flag was previously set in six places and enforced in none -- there was no change-password route at all, so an operator could hand out a password the recipient had no way to rotate. Basic/Bearer callers are never gated.
- **`tools/reset-identity.py`** -- destroys the six identity tables and un-records migration `0002`. Interactive TTY plus the typed phrase `RESET IDENTITY`; no `--force` flag.
- **`user-manager.py list-tokens [--username]`** -- `UserStore.list_tokens()` previously had zero callers, and neither `list` nor `show` printed tokens, so `revoke-token <id>` was unusable without scraping `auth_audit`. `docs/CLIENTNODE.md` pointed operators at commands that could not work.
- **`user-manager.py unlock <username>`** / `UserStore.unlock_user()` -- clears lockout without touching `status`, so unlocking a disabled account keeps it disabled. Previously a lockout cleared only by waiting, a successful login, or `set-password`/`enable` as a side effect.
- Admin user-creation form now has a **"Require password change at next login"** checkbox (default checked) instead of hardcoding `must_change_password=True`, so machine accounts are not flagged with something that cannot apply to them.

### Changed
- `sql/migrations/0002_identity.sql` was **edited in place** rather than amended by a `0004` -- a deliberate one-time exception to migration immutability, recorded in that file's header. It is only sound because both deployments are dropped and re-applied in this release.
- `_resolve_session()` now memoizes its result on `g` for the request. The gate and the `require_auth` decorator share one `get_principal()` join instead of issuing two; this halves DB round trips on the dashboard/logs hot path. Request-scoped, so the "a disabled account's session dies on its next request" guarantee is unchanged.
- `tools/send-http-alert.sh`, `tools/validate-https-flow.sh` and `tools/tests/test_normalization.sh` read `ALERT_USER`/`ALERT_PASSWORD` from the environment. The first two hardcoded a real gateway password in git, and `validate-https-flow.sh` echoed it to stdout.

### Removed
- **`users.expires_at`.** Read by `verify_login()`/`verify_token()`, but the only caller that ever set a non-NULL value was `tools/migrate-users-json.py`. No CLI flag, no admin-UI field, no update path -- every account carried `NULL` forever. (`auth_tokens.expires_at` is live and stays.)
- **`thresholds:write` and `users:manage`** from the `chk_role_scopes_permission` CHECK and `ALLOWED_PERMISSIONS`. Both were grantable through the admin UI and enforced by zero code; `/admin/users*` gates on the `master_admin` role.
- **`tools/migrate-users-json.py`** and `tests/test_migrate_users_json.py` (~925 lines). The one-off pre-v2.0.0 importer; its only input was `data/config/http-gateway/users.json`.
- **`data/config/http-gateway/users.json`** and `tokens.json` -- eight accounts with single-round salted-SHA-256 hashes, still bind-mounted read-only into the container.
- Dead `"auth"` block in `data/config/docker.json` (tracked in git, read by nothing) and the `users_file`/`rate_limiting` keys in `examples/config-database.json`.
- `docs/AUTH.md` §2.10 (pre-v2.0.0 migration), replaced by §2.10 change-password and §2.11 identity reset.

### Fixed
- `CLAUDE.md` claimed there is no Python unit-test suite. There are ~970 tests under `tests/`, with pytest in `requirements-dev.txt`.

## [2.0.3] - 2026-07-22

### Added
- **`sdk/python/`**: a Python SDK for `POST /alert`/`POST /test`/`GET /health`, independently versioned (starts at `0.1.0`, own `pyproject.toml`/`CHANGELOG.md` -- not tied to this project's own version). Evolves `docs/CLIENTNODE.md`'s documented `requests.post(...)` integration pattern rather than replacing it: same base URL, same Basic/Bearer auth model, same JSON payload shape.
  - Client-side validation (`alertmanager_sdk.validate_alert()`) mirrors `src/alert_router.py`'s `validate_alert()` field-for-field, closing a real gap: `POST /alert` only validates field *presence*, not values -- an invalid `level`/`category`, non-int `timestamp`, or non-string `source` gets HTTP 200 back immediately and is only actually checked later, asynchronously, on the MQTT-consumer side; if it fails there, it's silently dropped with no error ever reaching the original caller. Running the same validation client-side, before the network call, is the SDK's core value proposition.
  - Typed exception hierarchy (`AlertManagerError` and subclasses) mapping every documented gateway response shape to a specific exception -- deliberately no lockout/rate-limit-specific exception, since the gateway itself cannot distinguish a wrong password from a locked-out/disabled account in its response.
  - Retry/backoff for `POST /alert`/`POST /test` mirrors `pathways/email_pathway.py`'s `_post_sendmail` decision matrix exactly (retry only 429/5xx, capped at 2 retries, honor `Retry-After`, never retry connection-level failures on a non-idempotent POST).
  - Opt-in automatic exception reporting (`install_exception_hooks()`/`exception_hook_scope()`/`report_exceptions()`) hooking both `sys.excepthook` and `threading.excepthook` -- deliberately no `logging.Handler` integration, to keep exactly one automatic-reporting channel.
- `docs/CLIENTNODE.md` links to the new SDK; `.gitignore` gains `.pytest_cache/`.

## [2.0.2] - 2026-07-21

Architectural decision, not a code change: HTTPS (`POST /alert`) is now the sole documented/supported alert-ingestion path.

### Changed
- **SNMP Gateway deprecated**: never adopted in production (confirmed during the v2.0.0 rollout -- its container had never been started before that deploy, unlike every other long-running service). `snmp-gateway`'s compose profile moves from `secondary` to `deprecated`, alongside `syslog-gateway`, so it no longer starts with a normal `--profile secondary` run. Code, SQL-equivalent config, and the SNMP reference docs (`SNMP_INTEGRATION.md`, `SNMPV3_SPECIFICATION.md`, `SNMP_CLIENT_MAPPING.md`) are retained, not deleted -- same treatment `syslog-gateway` already got in v1.2.0.
- **MQTT reframed as device-communication, not ingestion**: MQTT was never actually usable for submitting alerts (the internal `alerts/input` topic isn't ACL'd for external publish; only ESP32 LED sign devices can reach the external listener at all, and only for their own zone topics), but the docs previously listed it alongside HTTPS as if it were a peer ingestion option. `CLAUDE.md` and `docs/CLIENTNODE.md` now state explicitly that MQTT is a device-communication bus (LED sign display push + telemetry) separate from how alerts get into the system.
- `docs/CLIENTNODE.md`'s Connection Matrix, MQTT Topics, Management Tools, and Documentation Index sections updated to drop SNMPv3 entirely and stop presenting MQTT as an alert-submission method.

## [2.0.1] - 2026-07-21

Two fixes discovered during the actual v2.0.0 production rollout:

### Fixed
- **`VERSION` file missing from the Docker image**: `docker/Dockerfile` never copied `VERSION` into `/app`, so `src/version.py` (which reads it via `Path(__file__).parent.parent / "VERSION"`) silently fell back to `"unknown"` -- visible on the gateway's `/health` endpoint and the `layout.html` footer app-wide. Added `COPY VERSION .` alongside the other application-file copies.
- **`tools/backfill_file_logs.py`'s max-non-manual-timestamp guard**: the guard live-queries `MAX(timestamp) WHERE ingestion_source <> 'manual'`, unscoped. If the live pipeline (`database-logger`) has already been fixed and restarted *before* the backfill runs -- exactly what happened during this rollout, since Phase 1 (partition fix) necessarily precedes Phase 3 (backfill) -- that live query returns a recent "now"-ish timestamp, newer than the entire historical outage gap, so every backfill row would fail `timestamp <= max` and be silently skipped (`skipped_old`) even though none of them were ever actually inserted. `--force` does not help (it only bypasses the separate "manual rows already exist" abort check). Added `--max-timestamp EPOCH`, which overrides the live query with an operator-supplied pre-outage boundary instead.

## [2.0.0] - 2026-07-21

**BREAKING RELEASE.** This is Phase 4 of `docs/OVERHAUL_PLAN.md`: the HTTP gateway is rebuilt from a single-file `src/http_gateway.py` (JSON-config-driven Basic Auth / Bearer tokens against `data/config/http-gateway/users.json` + `tokens.json`) into a Postgres-backed identity/RBAC system (`src/userstore/`) fronting a full Flask package (`src/gateway/`) with session-cookie login, a dashboard, a rewritten log viewer, and a server-rendered admin UI. **Every existing HTTP gateway password is reset, `users.json`/`tokens.json` are retired, and the service is HTTPS-only** (the direct `8080:8080` host port publish is removed) — see the Rollout section of the Task 29 report / `docs/OVERHAUL_PLAN.md` section 4.10 for the exact, order-dependent upgrade sequence.

### Added
- **90-day file-log backfill tool** (`tools/backfill_file_logs.py`, Task 13/Phase 3): one-off script that parses the alert-content logs written by `pathways/file_pathway.py` (`file/{date}.log` flat + `file/{client}/{date}.log` per-client weekly) and inserts them into the partitioned `alerts` table, so history survives the Jan 11 2026 partition-automation outage (see `docs/OVERHAUL_PLAN.md`) instead of being queryable only from raw log files
  - `parse_log_line()`: pure, standalone parser for the exact line format `data/config/docker.json`'s `file_log` pathway writes (`{epoch10} [{level}] [{category}] {source} [{client}]: {title} - {message}`), matching its real single-line sanitization behavior (`" ↵ "` newline replacement, 500-char message truncation with `...` suffix); invalid level/category values are rejected and counted/logged by the caller with file:line context, not the parser itself
  - File walker treats any `file/{client}/` subdirectory as authoritative for that file's client ("folder name wins" over a disagreeing `[client]` bracket in the line itself), including `file/unauthenticated/`
  - Batched `execute_values` insert, always `ingestion_source='manual'` and `metadata={"backfilled_from": "<file>"}`
  - Guards: `--since-days 90` default; skips rows with `timestamp <=` the max timestamp of existing non-manual (`http`/`mqtt`/`snmp`/`feed`) rows, so it only fills the gap left by the outage rather than duplicating already-live data; aborts unless `--force` if any `ingestion_source='manual'` rows already exist; `--dry-run` reports per-file counts and sample rows without ever opening a database connection
  - `tests/test_backfill_parser.py`: golden-line parser tests generated by running the real `FilePathway.send_alert()` (not hand-typed approximations) -- clientless/flat, per-client, newline-sanitization artifact, message truncation (cross-checked against the real sanitizer), brackets inside title, collapsed-whitespace source, garbage/truncated lines, invalid level/category
  - `tests/test_backfill_db.py`: mocked-DB tests for file discovery, the guard predicate, `count_manual_rows`/`get_max_non_manual_timestamp` SQL shape, the `execute_values` call shape (columns/template/`ingestion_source`/`metadata`), "folder name wins", reject counting with file:line context, and `run()`'s abort-unless-force/`--dry-run`-never-connects/`--force` orchestration
  - **Not yet run against production data or a live Postgres** -- see the Task 13 report for the pre-run verification checklist (no live Postgres is available in the dev environment and docker is never invoked from automation, per project rule)
- **Identity & RBAC schema** (`sql/migrations/0002_identity.sql`, Task 14): `users` (argon2id `password_hash`, `status`, `default_client`, `must_change_password`, `failed_attempts`/`locked_until` lockout state, `expires_at`, `last_login_at`), `roles`, `user_roles`, `role_scopes` (`role_id`, `client`, `permission` -- `client='*'`/`permission='*'` wildcards), `auth_tokens` (hashed Bearer tokens, `ON DELETE RESTRICT` from `users`), `auth_audit` (append-only login/lockout/CRUD trail). Seeds one `master_admin` system role holding a `('*', '*')` scope. Deliberately drops `users.json`'s per-user `rate_limit` field -- never enforced anywhere; account lockout + IP throttle (below) replace the security intent.
- **`src/userstore/`** (Task 15): framework-free (`psycopg2` + `argon2-cffi` only) single owner of all account/role/scope/token/audit operations, imported by the gateway, the CLI, and the migration script.
  - `passwords.py`: `hash_password`/`verify_password`/`needs_rehash` over argon2-cffi's `PasswordHasher` defaults (argon2id, `time_cost=3`, `memory_cost=65536` KiB, `parallelism=4`)
  - `store.py`'s `UserStore`: `create_user`/`get_user`/`list_users`/`set_password`/`disable_user`/`enable_user`/`delete_user`; `verify_login()` (status/expiry/lockout checks before any password comparison, 5 consecutive failures locks the account 900s, exactly one `auth_audit` row per call); `get_principal()`/`verify_token()` (single join producing a `PrincipalData` -- `roles`, `scopes: {client: frozenset(permissions)}`, `default_client`); `create_token()` (returns the `amt_`-prefixed plaintext exactly once, stores only its SHA-256); `create_role`/`grant_role`/`revoke_role`/`add_scope`/`remove_scope`; `audit()`/`recent_audit()`
  - `PrincipalData.is_master_admin()` / `.has_permission(permission, client)` / `.visible_clients(permission)` -- the authorization primitives every gateway blueprint and the admin UI build on
- **`tools/user-manager.py`** (Task 16): the canonical identity CLI, replacing the deleted `tools/http-auth-manager.sh` -- `init-admin` (refuses if a `master_admin` already exists), `create-user`/`set-password`/`list`/`show`/`disable`/`enable`/`delete`, `create-role`/`grant-role`/`revoke-role`/`add-scope`/`remove-scope`, `create-token`/`revoke-token`, `audit`. Generated passwords/tokens are printed to stdout exactly once. Run via `docker exec -it alert-http-gateway python3 tools/user-manager.py <command>` or a one-off `docker run` if the gateway itself is down.
- **`tools/migrate-users-json.py`** (Task 17): one-off migration from the legacy `data/config/http-gateway/users.json` into the new schema -- `type: admin` grants the seeded `master_admin` role; `type: node` gets a `<client>-node` role (`alerts:write` + `logs:read` scopes) plus `default_client`; `type: client` gets a `<client>-viewer` role (`logs:read` only); the `testing` account is always dropped. Every migrated account receives a fresh random password (the old SHA-256 hash cannot be converted to argon2id) with `must_change_password=True`, printed once in a single one-time credential table. Aborts before creating anything if any target username already exists.
- **`src/gateway/` package** (Tasks 18-27): a Flask app-factory (`create_app()` in `src/gateway/__init__.py`) that replaces the deleted `src/http_gateway.py`, organized as five blueprints:
  - `auth` (`gateway/auth/`): `GET/POST /login`, `POST /logout`, `GET /api/whoami` -- hand-rolled signed-cookie session (`{uid, u, iat}`, `HttpOnly`/`Secure`/`SameSite=Lax`, 12h sliding lifetime), Flask-WTF CSRF protection, a per-IP login throttle (`IPThrottle`: 20 failures/10 minutes -> 429), and `CredentialCache` (fresh/stale/miss/unavailable in-memory cache fronting Basic/Bearer verification so high-frequency node `POST /alert` traffic doesn't pay a DB + argon2 round trip on every request)
  - `api` (`gateway/api/`): `GET /health`, `POST /alert`, `POST /test`, `GET /docs`, `GET /docs/<filename>` -- Basic/Bearer only, CSRF-exempt (never accepts a session cookie); see the breaking `POST /alert` change below
  - `logs` (`gateway/logs/`): `GET /logs` (page), `GET /api/logs/list`, `GET /api/logs/tail`, new `GET /api/logs/chunk` -- session OR Basic/Bearer, so existing `tools/tests/*.sh` curl workflows keep working; binary, newline-aligned reads (fixes the old text-mode `f.tell()` fragility and mid-line-split risk under concurrent writers); the old in-memory 10,000-line truncation is gone entirely
  - `dashboard` (`gateway/dashboard/`): `GET /dashboard` (page), `GET /api/dashboard/summary`, `GET /api/dashboard/critical` -- session-only; alert counts and a critical-alert table over a selectable window (`1h`/`6h`/`12h`/`24h`/`7d`/`14d`/`30d`/`all`), scoped server-side to each principal's `visible_clients('logs:read')` (rows with a NULL/empty `client` are visible to `master_admin` only)
  - `admin` (`gateway/admin/`): `GET /admin/users`, `GET/POST /admin/users/new`, `GET/POST /admin/users/<username>/edit`, `POST .../disable`, `POST .../enable`, `POST .../reset-password` -- `master_admin`-only, session-only, full CSRF protection; every handler calls the same `UserStore` methods the CLI does; self-lockout guard rails prevent disabling or de-admin-ing your own session; passwords are always server-generated and shown exactly once
  - Shared template/static foundation (`templates/layout.html`, `login.html`, `dashboard.html`, `logs.html`, `admin/*.html`; `src/static/{css,js}`) -- fixes the old inline-HTML log viewer's dead `{__version__}` f-string bug via a real Jinja `version` context processor
- **gunicorn WSGI cutover** (Task 28): `gateway.wsgi:app`, 1 worker x 8 gthread threads, `--timeout 60` -- a single process keeps the in-memory `CredentialCache`/`IPThrottle`/MQTT client state coherent while threads cover request concurrency. `docker-compose.yml`'s `http-gateway` service now depends on `postgres: service_healthy` and carries `POSTGRES_*`/`GATEWAY_SECRET_KEY` environment variables.

### Changed
- **BREAKING -- `POST /alert` authorization**: the `client` field (payload value, else the account's own `default_client`, else clientless `''`) is now checked against the authenticated account's `alerts:write` scopes, not merely accepted verbatim. A client the caller has no scope for now returns `403`; submitting no client at all (and having no configured default) now requires `master_admin`. This closes a hole where any authenticated account could previously submit alerts under any other client's name. See `CLIENTNODE.md`.
- **BREAKING -- HTTPS-only**: the `http-gateway` compose service no longer publishes port 8080 to the host; all traffic must go through the `nginx-proxy` reverse proxy at `https://alert.d-t.pw`. Direct `http://<host>:8080` access, including from sender machines that bypassed the proxy, no longer works.
- **BREAKING -- full credential reset**: every account is re-created from scratch by `tools/migrate-users-json.py` with a brand-new random password (`users.json`'s SHA-256 hashes cannot be converted to argon2id); every sender machine and human user needs its credentials updated after cutover.
- User/access-control model replaces the old fixed `admin`/`node`/`client` account **types** (with their hardcoded rate limits and 1/5/2-year expiries) with roles -> `role_scopes` (`client`, `permission`) grants, checked via `has_permission()`/`visible_clients()`. `default_client`-based inheritance for an omitted payload `client` is unchanged in spirit, but is now an account attribute rather than an implication of account "type".

### Removed
- `src/http_gateway.py` -- replaced in full by `src/gateway/` (Task 28)
- `tools/http-auth-manager.sh` -- replaced by `tools/user-manager.py`
- `data/config/http-gateway/users.json` and `tokens.json` -- retired once the operator confirms the cutover (see rollout notes); replaced by the Postgres identity schema
- Per-user `rate_limit` fields (never enforced in code; account lockout + IP throttle cover the security intent -- see Deferred, below)

### Deferred
Explicitly deferred to a later plan (recorded here so it isn't lost): service status page (container health, pathway errors, partition status, last-alert per client) · routing-rules & pathway config web editor · email pathway settings UI · email send-history web viewer (recording ships now in Phase 2) · API rate limiting (dropped per-user `rate_limit` fields; lockout+throttle cover the security intent) · `alerts:write`/`thresholds:write` UI features (schema supports them from day one).

## [1.7.0] - 2026-07-20

### Added
- **Email Send Tracking** (`email_log` table + `pathways/email_log.py`'s `EmailLogWriter`): every Graph API send attempt made by `EmailPathway` -- immediate and storm-aggregated, success and failure -- is now recorded
  - `sql/migrations/0003_email_log.sql`: unpartitioned `email_log` table (`attempted_at` epoch BIGINT, `pathway`, `aggregated`, `alert_count`, `level`, `clients[]`, `recipients[]`, `subject`, `status` accepted/failed, `http_status`, `error`, `graph_request_id`, `duration_ms`, `retries`); `status='accepted'` means Graph returned HTTP 202 (queued), not confirmed delivery -- Graph has no delivery webhook
  - `EmailLogWriter` is deliberately fail-open: lazy connection, autocommit, every method catches its own exceptions and logs ERROR rather than raising, and drops the connection on any failure so the next call reconnects fresh instead of retrying in a loop. A tracking-database outage must never become a notification outage.
- **New Default Email Templates**: dark-mode-aware Jinja2 templates backed by a new shared macro library, `src/templates/emails/components/alert_macros.html.j2` (level badges, chips, client pills, details/metadata tables, the dark-mode `<style>` block)
  - `alert_default.html.j2` / `alert_default_aggregated.html.j2` are now the out-of-the-box templates for any `email` pathway that doesn't set an explicit `template_file`/`aggregated_template` (previously these fell back to inline Python HTML builders)
  - Client-pill display for storm/aggregated sends spanning multiple clients; redesigned storm-alert layout
  - **Real mail-client verification (Gmail, Outlook, iOS Mail; light + dark) is still pending -- see manual verification checklist**

### Changed
- **Email Pathway Hardening** (`pathways/email_pathway.py`):
  - Graph `sendMail` calls now use explicit connect/read timeouts (5s connect, 30s read)
  - Retry decision matrix: HTTP 202 never retries (accepted); 429/5xx retry up to `MAX_RETRIES=2` times (fixed backoff schedule `[2, 8]` seconds), honoring a `Retry-After` response header when present (capped at 30s); any other 4xx never retries
  - `EmailLogWriter.record()` is now called for every send attempt on both the immediate and aggregated paths, including MSAL token-acquisition failures (recorded with `http_status=None`)
  - `BasePathway.async_errors` is now incremented when the timer-thread-driven aggregated flush fails (the synchronous `shutdown()` flush path is unaffected, since its failures are already logged directly)
  - `_create_html_body`/`_create_default_aggregated_body` (inline Python HTML builders) deleted in favor of the Jinja2 template pipeline; a template render failure is now a hard send failure (no fallback body), per the project's no-failsafes rule

### Removed
- **Orphaned `src/pathways/database_pathway.py`**: dead code left over from an earlier design where pathways wrote directly to Postgres; alert persistence has gone through the separate `database-logger` service (`src/database_logger.py`, which predates this pathway by a long way) long before `database_pathway.py` was ever written, and no active pathway config (`data/config/*.json`) referenced this pathway type

### Testing
- `tests/test_email_log_writer.py` (Task 9): fail-open contract -- lazy connection, successful insert shape, connect/insert/close failure all logged and swallowed, connection dropped and lazily re-established on the next call, no retry loop within a single `record()` call
- `tests/test_email_pathway.py` (Tasks 10-11): `_post_sendmail()`'s full retry decision matrix (202 no-retry, 429 honoring/capping/malformed-`Retry-After`, 500/503 backoff, 400/4xx no-retry, retry cap at 2, non-retried `requests` exceptions), `EmailLogWriter.record()` invoked on every immediate/aggregated attempt including MSAL failures, `async_errors` timer-vs-shutdown-flush semantics, `_client_context()`, and the Task 11 template-default/render-failure behavior. This coverage supersedes the plan's standalone `tests/test_email_retry.py` -- the retry matrix was implemented alongside the pathway hardening in Task 10 and already lives here with no gaps against the brief's matrix; a separate file would have duplicated the same assertions under a different name.

## [1.6.0] - 2026-07-20

### Added
- **SQL Migration Runner**: `src/db_migrate.py`, a standalone versioned migration runner (stdlib + psycopg2, no Alembic/ORM)
  - `schema_migrations(version, name, applied_at)` bookkeeping table, `pg_advisory_lock` serialization to prevent concurrent runs
  - Applies sorted `sql/migrations/NNNN_*.sql` files, each in its own transaction; fail-fast exit 1 on first error
  - `--dry-run` and `--dsn` flags; DSN built from the existing `POSTGRES_*` env var convention
  - Intended to be run as a one-off container before restarting affected services (see Rollout below)
- **Partition Automation**: `sql/migrations/0001_partition_management.sql` adds `ensure_alert_partitions(p_from, p_weeks_ahead)`, an idempotent function that creates any missing weekly `alerts_YYYY_wNN` partitions
- **Heartbeat Utility**: `src/utils/heartbeat.py` (`beat()` / `check()`) - shared liveness-file helper backing the new compose healthchecks
- **Pytest Scaffold**: `requirements-dev.txt` (pytest, pytest-mock) and a new `tests/` directory with coverage for the migration runner, partition math, database-logger, alert-router, analytics-engine, and the http-gateway health endpoint

### Changed
- **database-logger Hardening** (`src/database_logger.py`):
  - Startup partition gate calls `ensure_alert_partitions` at boot and exits fail-fast (`sys.exit(1)`) if it fails
  - 24-hour periodic partition maintenance (ensure / drop / refresh) folded into the existing 60-second stats loop
  - Batch-insert failure isolation: rollback, best-effort partition repair, one retry, then row-by-row fallback with full-payload error logging for any row that still fails
  - MQTT `clean_session=False` so queued QoS 1 alerts survive a service restart instead of being dropped
  - Heartbeat file touched every healthy cycle
- **PostgreSQL promoted out of the `analytics` compose profile**: `postgres` and `database-logger` are now core (default) services started by a bare `docker-compose up -d`, no longer gated behind `--profile analytics`; `alert-router` gained the same `POSTGRES_*` env vars in preparation for later phases

### Fixed
- **Docker Healthchecks**: removed the broken Dockerfile `HEALTHCHECK` (it checked `/data/logs/status.json`, which nothing ever wrote, producing false alarms) and replaced it with compose-level heartbeat-file healthchecks for `alert-router`, `file-logger`, `database-logger`, and `analytics-engine`; `http-gateway`'s `/health` now reports `db`/`mqtt` detail (informational only - a Postgres blip never flips the endpoint's status)

## [1.5.0] - 2026-02-14

### Added
- **New Alert Categories**: Added three new categories to the alert taxonomy
  - `business` - Business operations, audits, imports, config changes, CRUD events
  - `hardware` - Physical infrastructure, device status, battery levels, disk health
  - `environmental` - Environmental monitoring, temperature, humidity, air quality, CO2
- **Email Storm Prevention**: Aggregates multiple critical alerts within a rolling time window into a single summary email
  - Configurable window duration (default 10 seconds)
  - Level-based filtering for which alerts to aggregate
  - Jinja2 templates for aggregated email format
  - Graceful flush on service shutdown
  - Independent aggregation state per email pathway instance
- **Aggregated Email Templates**: Two new Jinja2 templates for storm alert summaries
  - `aggregated_alerts.html.j2` - Generic storm alert template
  - `revysmc_aggregated.html.j2` - REVYSMC-branded storm alert template

### Changed
- Updated PostgreSQL CHECK constraint for new categories
- Updated all documentation to reflect new category options
- PathwayManager now supports graceful shutdown via `shutdown_all()` method

## [1.4.0] - 2025-01-15

### Added
- **File Pathway Message Sanitization**: Single-line log format enforcement
  - Replaces newlines, carriage returns, and tabs in alert messages
  - Configurable max message length with truncation
  - Configurable newline replacement string
  - Applies to title, message, and source fields

## [1.3.0] - 2025-12-22

### Added
- **REVYSMC Critical Alert Email Template**: Custom professional email template for Revelstoke Snowmobile Club operations
  - Branded styling with red accent colors for critical alerts
  - Clean, minimal design aligned with Alert Manager theme specification
  - Action required callout section for operational awareness
  - Client-specific branding in header and footer
- **Email Pathway Jinja2 Support**: EmailPathway now supports custom Jinja2 templates
  - Added `template_file` configuration option for pathways
  - Jinja2 environment with HTML autoescape and custom filters
  - `datetime_format` filter for Unix timestamp formatting
  - Falls back to default templates if rendering fails

### Changed
- **MQTT External Access Scope**: Clarified that external MQTT (port 42690) is exclusively for ESP32 LED sign devices
  - All other clients (scripts, services, integrations) should use HTTP gateway
  - Updated CLIENTNODE.md, MQTT_AUTH.md, and CLAUDE.md with scope notices
- **MQTT ACL Configuration**: Rewritten to zone-specific model per mqttservernotes.txt
  - Zone-specific LED sign access (each device reads only its zone)
  - LED status topics: rssi, ip, uptime, memory
  - Internal Docker network retains unrestricted anonymous access
- **Documentation Audit**: Comprehensive timestamp format fixes across all docs
  - All alert payload examples now use Unix epoch integers (not ISO 8601)
  - Fixed HTTP_GATEWAY.md, DEV_GUIDE.md, AUTH.md, README.md, DOCKER.md
  - Added missing HTTP endpoints documentation (/logs, /api/logs/*, /docs)
  - Added user access control section to HTTP_GATEWAY.md

### Fixed
- **Barcode Not Found Analyzer**: Fixed aggregation to respect analysis period instead of log file boundaries
  - Added timestamp-based filtering to honor `analysis_period_days` configuration
  - Daily reports now correctly count only events from the target day, even when reading weekly log files
  - Removed exception fallbacks in timezone handling to always use pytz for consistency
  - Improved logging to show exact UTC date range being analyzed

### Deprecated
- **Syslog Gateway**: Marked as deprecated in CLAUDE.md (use HTTP or SNMPv3 instead)

## [1.2.0] - 2025-12-01

### Added
- **Theme Specification**: Comprehensive UI/UX design system documentation
  - Dark mode color palette with CSS custom properties
  - Alert level color standards (critical, warning, notice, info, debug)
  - Typography scale and font stack definitions
  - Component styling guidelines (buttons, cards, forms)
  - Logo specifications with required resolutions and variants
  - Alternative logo palette for special projects/marketing
- **Brand Assets**: Complete logo and favicon package
  - Logo variants: master, 1200x400, 800x400, 512x512
  - Favicon package: ICO (16/32/48), Apple touch icon (180), Android Chrome (192/512)
  - Paint.NET source file for editing
  - Assets directory structure with documentation
- **HTTP Gateway Branding**: Logo and favicon integration
  - Favicon links in HTML head
  - Header logo display with responsive layout
  - Footer branding with accent color
  - Dark mode meta tag support

### Changed
- **Log Viewer CSS**: Refactored to use CSS custom properties
  - All colors now use `var(--variable-name)` for maintainability
  - Version bumped to v1.1
  - Added responsive design for mobile devices
- **Email Templates**: Updated to dark theme for consistency
  - Base template with theme color reference comments
  - Logo support via base64 embedding (avoids email client blocking)
  - Footer branding with accent colors
  - All components updated to use theme colors
- **Base Analyzer**: Added theme support methods
  - `THEME_COLORS` dictionary with all specification colors
  - `get_logo_base64()` method for email logo embedding
  - `get_theme_colors()` method for template context
- **Barcode Not Found Analyzer**: Updated to use theme colors
  - Metrics use theme level colors (critical, warning)
  - Insight box uses type presets
  - Logo embedded in email reports

### Documentation
- Added `docs/THEME_SPECIFICATION.md` - Complete design system reference
- Updated `assets/README.md` - Logo and favicon usage guide
- Updated `src/static/README.md` - Static assets documentation

## [1.1.0] - 2025-11-20

### Added
- **Analytics Engine**: Complete analytics framework with scheduled log analysis and professional HTML email reports
  - Pluggable analyzer system for extensible analytics types
  - Jinja2 template engine for email report generation with Premailer CSS inlining
  - Query definition system for configuration-driven pattern matching
  - Barcode Not Found analyzer with detailed statistics and weekly reporting
  - Multi-client support with independent configurations per client
  - Template component library (summary cards, data tables, insight boxes)
- **Microsoft Graph API Integration**: MSAL-based authentication for O365 email sending
  - Secure token acquisition and refresh
  - Environment variable-based configuration
  - Support for both Graph API and legacy SMTP methods
- **PostgreSQL Database Integration**: Complete database logging pathway
  - TimescaleDB-optimized schema with automatic partitioning
  - Alert history storage with full metadata preservation
  - Database-backed analytics with complex query support
  - SQL query definitions for reusable analytics patterns
- **SNMPv3 Gateway**: Multi-level security support for SNMP trap ingestion
  - noAuthNoPriv (default, maximum compatibility)
  - authNoPriv (SHA authentication)
  - authPriv (SHA + AES encryption)
  - Discovery mode for trap analysis
  - OID mapping with template-based message generation
  - Username-based client assignment
- **Centralized Version Management**: Single VERSION file for all services
  - `src/version.py` module for programmatic access
  - Dynamic version display in HTTP gateway UI
  - Version reporting in health check endpoint

### Changed
- **Email Pathway**: Refactored to support both O365 Graph API (default) and SMTP
  - Simplified configuration with environment variables
  - Improved error handling and retry logic
  - Professional HTML email templates with inline CSS
- **HTTP Gateway**: Enhanced logging and authentication
  - User type-based access control (admin/node/client)
  - Rate limiting per user type
  - Token-based and basic authentication support
  - Improved log viewer with recursive directory support
- **MQTT Authentication**: Simplified to username/password only
  - Removed client certificate complexity for ESP32 compatibility
  - TLS 1.2 with ESP32-optimized cipher suites
  - Lower memory footprint for IoT devices
- **File Logging**: Added weekly rotation support
  - Per-client log files with configurable rotation periods
  - `{client}` and `{date}` placeholders in paths
  - Automatic directory creation

### Improved
- **Documentation Cleanup**: Removed temporary and planning documents
  - Consolidated analytics documentation into ANALYTICS.md and ANALYTICS_QUICKSTART.md
  - Removed outdated deployment summaries and validation guides
  - Removed future feature design docs (audio, light indicators)
  - Cleaned up docs folder structure
- **Configuration Structure**: Enhanced analytics and email pathway configs
  - Clear separation of concerns between pathway configuration and analytics
  - Reusable query definitions for multi-client deployments
  - Schedule configuration with cron-like syntax

### Fixed
- Configuration path resolution for Docker volumes
- Environment variable loading order for credentials
- MQTT callback API version compatibility across all services
- Email sending reliability with proper error handling

### Security
- Environment variable-based credential management (O365, SMTP)
- Read-only log access for analytics engine (least privilege)
- SNMPv3 security level support with proper authentication
- HTTP gateway authentication audit logging

## [1.0.0] - 2024-10-01

### Initial Release
- Core alert routing system with MQTT backbone
- HTTP and MQTT gateways for alert ingestion
- File, console, and LED sign pathways
- BetaBrite LED sign integration with 23+ animation modes
- Rule-based routing with level/category/source filtering
- Docker deployment with docker-compose
- Basic authentication and TLS support
- Service health monitoring
- Log viewer web interface

[1.1.0]: https://github.com/jimidarke/alertManager/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/jimidarke/alertManager/releases/tag/v1.0.0
