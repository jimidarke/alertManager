# Alert Manager Authentication & Access Control

**Rewritten for v2.0.0.** The Alert Manager has two independent access endpoints, each with its own authentication mechanism:

1. **MQTT Endpoint** (`alert.d-t.pw:42690`) -- username/password over TLS, **ESP32 LED sign devices only**. See `MQTT_AUTH.md` for the full protocol/device-side detail; this document only summarizes it.
2. **HTTP Gateway** (`https://alert.d-t.pw`) -- everything else: scripts, services, integrations, and the browser dashboard/log-viewer/admin UI. This is the bulk of this document, and it is a **full rewrite of the pre-v2.0.0 model**: there is no more Basic-auth-against-a-JSON-file-only story, no more plaintext `tokens.json`, and no more fixed `admin`/`node`/`client` account types. Authentication is session cookies (browser) or Basic/Bearer (scripts/devices) against a PostgreSQL-backed identity store, with role-based authorization.

---

## 1. MQTT Endpoint (summary -- see `MQTT_AUTH.md`)

- **Scope**: port 42690 is exclusively for ESP32 BetaBrite LED sign devices. Every other client (scripts, services, integrations) uses the HTTP gateway instead.
- **Authentication**: username/password only. Client certificates are **deprecated** -- do not provision new ones for device auth.
- **TLS**: TLSv1.2, ESP32-compatible cipher suites, 2048-bit RSA server certificate (still required for the TLS handshake itself, even though client certs are no longer used for authentication).
- **User management**: `tools/mqtt-auth-manager.sh` (username/password accounts, zone ACLs).
- **Certificate management** (server/CA certificate for TLS, not client auth): `tools/cert-manager.sh` -- see that script's own `--help` output. Internal container-network MQTT (port 1883) remains unauthenticated and is not reachable from outside the Docker host.

---

## 2. HTTP Gateway Authentication & Authorization

### 2.1 Architecture

`src/gateway/` (a Flask app-factory package, `create_app()`) is organized into five blueprints, each with its own auth policy (`src/gateway/principal.py`):

| Blueprint | Routes | Accepted auth methods |
|-----------|--------|------------------------|
| `api` | `/health`, `/alert`, `/test`, `/docs`, `/docs/<f>` | Basic or Bearer **only** -- never a session cookie |
| `logs` | `/logs`, `/api/logs/list`, `/api/logs/tail`, `/api/logs/chunk` | Session, Basic, **or** Bearer |
| `dashboard` | `/dashboard`, `/api/dashboard/*` | Session only |
| `admin` | `/admin/users*` | Session only, **and** the account must hold the `master_admin` role |
| `auth` | `/login`, `/logout` | N/A (these routes establish/destroy the session itself) |
| `auth` | `/api/whoami` | Session, Basic, or Bearer |

`api` never accepting a session cookie is what makes its CSRF exemption safe (see 2.3): there is no cookie for a forged cross-site request to ride along on. `logs` accepts both so existing `curl -u ...` shell-test workflows (`tools/tests/*.sh`) keep working unchanged alongside the new browser UI.

**Anonymous-request behavior**: an anonymous request to a session-allowing HTML route (e.g. `GET /dashboard` with no cookie, no `Authorization` header) gets a `302` redirect to `/login?next=<path>` (same-origin-validated `next` -- an absolute URL or protocol-relative `//host/...` is rejected and replaced with the default landing page). Any request under `/api/*`, or any request that presented an `Authorization` header that failed to resolve, always gets a `401` challenge instead, regardless of whether "session" is an allowed method for that route -- a script that deliberately sent (wrong) credentials should see `401`, never an HTML page it can't do anything with.

### 2.2 Sessions

Hand-rolled on top of Flask's own signed-cookie session (not Flask-Login): the cookie payload is exactly `{uid, u, iat}` (user id, username, issued-at epoch), created by `POST /login` and verified on every request by re-fetching the account's *live* `status` from the database (not cached) -- a disabled account's session stops working on its very next request, with zero staleness window.

Cookie properties: `HttpOnly` (no JS access), `Secure` (HTTPS-only -- safe to require unconditionally because `ProxyFix(x_for=1, x_proto=1)` makes `request.is_secure` reflect the real client-to-proxy scheme when running behind `nginx-proxy`), `SameSite=Lax`.

**12-hour sliding lifetime**: `PERMANENT_SESSION_LIFETIME` is set to 12 hours and `session.permanent = True` is set at login; Flask's own `SESSION_REFRESH_EACH_REQUEST` (on by default) re-issues a fresh `Set-Cookie` with a renewed expiry on every response, so an active session never expires mid-use, while an idle one expires 12 hours after its last request.

### 2.3 CSRF

Flask-WTF's `CSRFProtect` is initialized globally (`csrf.init_app(app)`); the `api` blueprint explicitly exempts itself (`csrf.exempt(api_bp)`) since it never accepts a session cookie -- there is nothing to forge. Every other blueprint's POST routes (`/login`, `/logout`, every `/admin/users*` mutation) require a valid `csrf_token` form field; `layout.html` embeds the token so every server-rendered form can include it. A POST without a valid token is rejected with `400`.

### 2.4 Role-Based Access Control (RBAC)

**Schema** (`sql/migrations/0002_identity.sql`): `users` → `user_roles` → `roles` → `role_scopes`. A `role_scopes` row is a `(role_id, client, permission)` triple:

- **`permission`** is one of: `logs:read`, `alerts:write`, or the wildcard `*` (all permissions). This is a DB `CHECK` constraint -- extending the list is a deliberate migration, not a config change. (`thresholds:write` and `users:manage` existed in the v2.x CHECK and were removed in v3.0.0: both were grantable through the admin UI and enforced by zero code. `/admin/users*` is gated on the `master_admin` role, not on a permission.)
- **`client`** is either a real client name (matching `alerts.client`/the alert payload's `client` field) or the wildcard `*` (all clients).

A single seeded system role, **`master_admin`**, holds exactly one scope: `('*', '*')` -- every permission, every client, including rows where `client` is NULL/empty (which no other role's wildcard scope can see -- see 2.4.2).

Every authenticated request resolves to a `PrincipalData` (`src/userstore/store.py`): `roles: frozenset[str]`, `scopes: {client: frozenset[permission]}`, plus `user_id`/`username`/`status`/`default_client`/`must_change_password`. Three methods drive every authorization decision in the gateway:

- **`is_master_admin()`** — `True` iff `"master_admin" in roles`. Independent of `scopes`.
- **`has_permission(permission, client)`** — `True` if any of: an exact `(client, permission)` scope, a `(client, '*')` scope, a `('*', permission)` scope, or a `('*', '*')` scope. `client` is compared verbatim (already-lowercased) -- no normalization inside this call.
- **`visible_clients(permission)`** — the set of clients holding `permission`, or the `ALL_CLIENTS` sentinel if a `('*', permission)` scope exists. An **empty** `frozenset` means "no clients visible" (deny), never "see everything" -- only the `ALL_CLIENTS` sentinel means that.

#### 2.4.1 `POST /alert`'s breaking authorization change

Effective client = payload `client` (stripped), else the account's `default_client`, else `''` (clientless). The request requires `has_permission('alerts:write', effective_client)`; the clientless case (`''`) additionally requires `is_master_admin()` -- `has_permission()` alone is not sufficient there, because a non-`master_admin` role could in principle be granted an explicit `client=''` scope without being `master_admin`. This closes the old gateway's "any authenticated account can submit an alert as any client" hole. See `CLIENTNODE.md` for the client-facing breaking-change note.

#### 2.4.2 Wildcard scopes and the null/empty-client exclusion

A `client='*'` scope means "every **named** client", not "every row including clientless/system rows". The dashboard's scope predicate (`src/gateway/dashboard/routes.py`) explicitly excludes `client IS NULL`/`client = ''` rows for every principal except one that is independently confirmed `is_master_admin()` -- `visible_clients()` returning the `ALL_CLIENTS` sentinel is **not**, on its own, treated as proof of `master_admin`, because nothing in the schema ties a `client='*'` scope to that one role. This is a deliberate, defense-in-depth design point (found during code review): `is_master_admin()` and `visible_clients()` are computed from disjoint fields (`roles` vs. `scopes`) of the same `PrincipalData`, and both are checked explicitly wherever it matters.

### 2.5 Password hashing (argon2id)

`src/userstore/passwords.py` uses `argon2-cffi`'s `PasswordHasher` with its own library defaults, which are argon2id: `time_cost=3`, `memory_cost=65536` KiB (64 MiB), `parallelism=4`, `hash_len=32`, `salt_len=16`. These parameters are deliberately not overridden -- argon2-cffi's own maintainers revise them upward over time as hardware improves, and this module tracks that rather than freezing an early choice. `needs_rehash(hash)` flags a hash encoded with older/weaker parameters for regeneration on next successful login.

### 2.6 The `tools/user-manager.py` CLI

The canonical way to administer accounts/roles/scopes/tokens -- replaces the deleted `tools/http-auth-manager.sh`. Run inside the gateway container (or a one-off container if the gateway itself is down):

```bash
docker exec -it alert-http-gateway python3 tools/user-manager.py <command> ...
# or, if the gateway container is down:
docker run --rm -it --env-file .env <image> python3 tools/user-manager.py <command> ...
```

**Subcommands** (each is a thin wrapper over one `UserStore` method -- see `--help` on any subcommand for its exact flags):

| Command | Purpose |
|---------|---------|
| `init-admin [username]` | Bootstrap the first `master_admin` account (refuses if one already exists); prints a generated password once |
| `create-user <username> [--password] [--default-client] [--must-change-password] [--status]` | Create a new account (random password printed once if `--password` omitted) |
| `set-password <username> [--password] [--must-change-password]` | Set/reset a password; also clears any lockout state |
| `list` | Table of all accounts |
| `show <username>` | One account's details |
| `disable <username>` / `enable <username>` | Deactivate / reactivate an account (`enable` also clears lockout state) |
| `unlock <username>` | Clear lockout state only (`failed_attempts=0`, `locked_until=NULL`) -- leaves `status` alone, so unlocking a disabled account keeps it disabled |
| `delete <username>` | Permanently delete an account (blocked while it still owns any token -- revoke those first) |
| `create-role <name> [--description] [--is-system]` | Create a role |
| `grant-role <username> <role>` / `revoke-role <username> <role>` | Assign/remove a role |
| `add-scope <role> <client> <permission>` / `remove-scope <role> <client> <permission>` | Add/remove a `(client, permission)` grant on a role (`client`/`permission` may be `*`) |
| `create-token <name> (--username \| --user-id) [--expires-at]` | Issue a Bearer token (plaintext printed once) |
| `list-tokens [--username]` | List tokens (all accounts, or one) with their ids -- how you find the id `revoke-token` needs |
| `revoke-token <token_id>` | Revoke a token by id |
| `audit [--username] [--event] [--limit]` | Recent `auth_audit` rows |

Every mutating subcommand accepts `--actor`/`--source-ip` for the audit trail (`--actor` defaults to `cli:$USER`/`cli:$USERNAME` if set, otherwise omitted -- `docker exec` sets neither by default, so pass it explicitly if you want attribution).

**Example** (create a scoped write-only account for a sender machine):
```bash
docker exec -it alert-http-gateway python3 tools/user-manager.py create-user price-checker --default-client price-checker
docker exec -it alert-http-gateway python3 tools/user-manager.py create-role price-checker-node --description 'Alert submission for price-checker'
docker exec -it alert-http-gateway python3 tools/user-manager.py add-scope price-checker-node price-checker alerts:write
docker exec -it alert-http-gateway python3 tools/user-manager.py grant-role price-checker price-checker-node
```

The `master_admin`-only web UI (`/admin/users`) performs the exact same `UserStore` calls from a form, for operators who prefer a browser -- including `create_token()`/`list_tokens()`/`revoke_token()`, not just account/role/scope creation -- see `docs/HTTP_GATEWAY.md`.

### 2.7 `CredentialCache` (Basic/Bearer only, `POST /alert`)

`src/gateway/auth/cache.py`'s `CredentialCache` fronts `UserStore.verify_login()`/`verify_token()` specifically for `POST /alert` (high-frequency node traffic) so a burst of requests doesn't pay a DB round trip + argon2 comparison on every single one. It is **not** wired into the shared `logs`/`api`-other-route resolvers -- only `POST /alert` uses it.

Decision matrix, keyed on cache age (`FRESH_TTL = 60s`, `STALE_GRACE = 300s`):

| Cache state | Behavior |
|-------------|----------|
| No entry, or entry belongs to a different credential | **MISS** -- always calls the real `verify_login`/`verify_token`; caches the result (positive or negative); a live-check failure here raises `CredentialCacheUnavailable` |
| Matches, age < 60s | **FRESH HIT** -- returns the cached result immediately, no DB/argon2 call at all |
| Matches, 60s ≤ age < 300s | **STALE** -- always re-attempts the live check; on success, refreshes the cache; on failure, serves the stale result with a WARNING log |
| Matches, age ≥ 300s | **TOO STALE** -- always re-attempts the live check; on failure, raises `CredentialCacheUnavailable` (too old to trust) |

`CredentialCacheUnavailable` is turned into a `503` response by `POST /alert` -- distinct from a `401` ("definitely not authenticated"): it means "cannot currently determine". Credentials are stored as an HMAC-SHA256 digest under a per-process-boot random salt (never the plaintext, never persisted) so a memory dump reveals nothing usable, even across a restart.

**Invalidation on password change/disable**: not hook-based -- a stale, now-wrong cache entry can only ever be served during a *simultaneous* Postgres outage, for at most 300 seconds (`STALE_GRACE`), because every cache hit past `FRESH_TTL` re-attempts the live check first. `POST /login` (session auth) never goes through this cache at all -- a password change/disable is reflected on that path's very next request, with no staleness window whatsoever.

### 2.8 Lockout and IP throttle

Two independent, stacking mechanisms apply on every `POST /login` attempt:

- **Account lockout** (`UserStore.verify_login`, applies to Basic auth too): 5 consecutive failed attempts against **one account** sets `locked_until` 900 seconds into the future. A locked account's login attempt (even with the correct password) is rejected with the same generic message as any other failure -- the account being locked is never revealed to the caller, only to the `auth_audit` trail (`event='lockout'`). A successful login, an admin/CLI `set-password`/`enable`, or `user-manager.py unlock` clears the lock immediately.
- **Per-IP login throttle** (`src/gateway/auth/ip_throttle.py`'s `IPThrottle`, in-memory, correct specifically because the gateway runs as a single gunicorn worker process): 20 failed login attempts from **one source IP** within a trailing 10-minute window returns `429`, regardless of which username(s) were tried -- protects the login endpoint itself from being hammered, independent of the account-level lockout above. Checked *before* any credential verification is attempted.

Neither mechanism has a `record_success()` reset for the other's counter -- a shared IP (NAT, office network) legitimately mixes successful and failed logins across different users, so a success on one account must not clear another account's failure count on the same IP.

### 2.9 Bearer tokens

`create_token()` returns a plaintext token of the form `amt_` + 43 URL-safe base64 characters (32 bytes / 256 bits of randomness) **exactly once** -- only its SHA-256 digest is persisted. `verify_token()` checks the token's own `status`/`expires_at` and the owning account's `status` (a disabled account cannot authenticate via a token it still holds), but does **not** check account lockout -- lockout exists to slow down password guessing, which does not apply to a token looked up by exact hash match. Use `list-tokens` to find a token's id, then `revoke-token <id>` to soft-delete it (`status='revoked'`, so its history remains in `auth_audit`); deleting the owning user account is blocked (`ON DELETE RESTRICT`) while any token still references it.

The web UI covers the same lifecycle without a terminal: the New User form (`/admin/users/new`) can issue one token at account-creation time (an "Issue a Bearer token" checkbox + name field, alongside a purely client-side "Service account" convenience toggle that just nudges that checkbox and unchecks "Require password change"), and an account's edit page (`/admin/users/<username>/edit`) lists every token it holds and can issue more or revoke existing ones. Tokens issued through the web UI always have `expires_at=None` (never expire) -- there is no expiry-picker control there, matching the CLI's own default when `--expires-at` is omitted.

### 2.10 Changing a password (`/change-password`)

`users.must_change_password` is enforced as of v3.0.0. Any browser session whose account carries the flag is redirected by a `before_request` gate (`src/gateway/__init__.py`) to `GET /change-password` and cannot reach any other page until it is cleared -- only `/change-password`, `/login`, `/logout`, `/api/whoami` and static assets are exempt.

`POST /change-password` requires the current password (re-verified through `verify_login()`, so a wrong guess counts toward lockout exactly like any other failed credential check), a new password of at least 12 characters, and a matching confirmation; the new password must differ from the current one. On success it calls `set_password(..., must_change_password=False)` and writes a `password_changed` audit row.

**Session auth only.** A Basic/Bearer caller is never gated and cannot use this route: a machine account cannot fill in an HTML form, and gating `POST /alert` would break every device. Rotate a service account's credential with `tools/user-manager.py set-password`, where an operator is accountable for the new value.

### 2.11 Resetting the identity store

`tools/reset-identity.py` destroys the entire identity store -- every account, role, scope, token and audit row -- so it can be rebuilt from scratch. It drops the six identity tables and deletes migration `0002`'s `schema_migrations` row; `alerts` and `email_log` are never referenced. It requires an interactive TTY and the typed confirmation `RESET IDENTITY` (there is no `--force` flag on purpose), and it deliberately does not re-migrate or seed, so each stage is a separate visible command:

```bash
python3 tools/reset-identity.py
python3 src/db_migrate.py
python3 tools/user-manager.py init-admin --username <you>
```

Afterwards, rotate `GATEWAY_SECRET_KEY` in `.env` and restart the gateway -- otherwise a session cookie signed with the old key outlives the account it points at (it would fail the `_resolve_session` lookup, but rotating is the honest move).

---

## 3. Usage Examples

### Basic Authentication

```bash
curl -X POST https://alert.d-t.pw/alert \
  -u device-monitor:secure-password-123 \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "info",
    "category": "system",
    "title": "Test Alert",
    "message": "HTTP auth test successful",
    "source": "device-monitor"
  }'

# Health check (no authentication required)
curl -X GET https://alert.d-t.pw/health
```

### Bearer Token Authentication

```bash
curl -X POST https://alert.d-t.pw/alert \
  -H "Authorization: Bearer amt_1a2b3c4d5e6f7g8h9i0j1k2l3m4n5o6p7q8r9s0t1u2v3w4" \
  -H "Content-Type: application/json" \
  -d '{
    "timestamp": '$(date +%s)',
    "level": "critical",
    "category": "security",
    "title": "Security Alert",
    "message": "Unauthorized access detected",
    "source": "monitoring-service"
  }'
```

### Authentication Testing

```bash
# Test endpoint with authentication
curl -X POST https://alert.d-t.pw/test -u device-monitor:password123
# Expected: {"status": "success", "message": "Test alert sent successfully", ...}

# No authentication (should fail with 401)
curl -X POST https://alert.d-t.pw/alert \
  -H "Content-Type: application/json" \
  -d '{"level": "info", "category": "system", "title": "Test", "message": "Test", "source": "test"}'
# Expected: 401, WWW-Authenticate: Basic realm="Alert Gateway"

# Wrong password 5 times in a row -> account locked for 900s
# Session login form shows the same generic message either way:
# "Invalid username or password"
```

### Session login (browser)

```bash
# Log in and keep the session cookie for subsequent requests:
curl -c cookies.txt -X POST https://alert.d-t.pw/login \
  --data-urlencode "username=jimi" \
  --data-urlencode "password=your-password" \
  --data-urlencode "csrf_token=$(curl -s -c cookies.txt https://alert.d-t.pw/login | grep -oP 'name="csrf_token" value="\K[^"]+')"

curl -b cookies.txt https://alert.d-t.pw/dashboard
```

### Python client (Basic auth)

```python
import requests
import time

class HTTPAlertClient:
    def __init__(self, base_url="https://alert.d-t.pw", username=None, password=None, token=None):
        self.base_url = base_url
        self.auth = (username, password) if username else None
        self.headers = {"Content-Type": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"

    def send_alert(self, level, category, title, message, source, metadata=None):
        alert = {
            "timestamp": int(time.time()),
            "level": level,
            "category": category,
            "title": title,
            "message": message,
            "source": source,
        }
        if metadata:
            alert["metadata"] = metadata
        response = requests.post(f"{self.base_url}/alert", auth=self.auth, headers=self.headers, json=alert)
        return response

client = HTTPAlertClient(username="device-monitor", password="secure-password-123")
response = client.send_alert("info", "system", "Test Alert", "HTTP client test", "python-script")
```

---

## 4. Security Best Practices

### HTTP Gateway
- Rotate passwords/tokens via `tools/user-manager.py set-password`/`create-token`+`revoke-token` -- there is no bulk/scheduled rotation tooling in v1
- Grant the narrowest role/scope that does the job (`logs:read` for viewers, `alerts:write` scoped to one client for senders) -- avoid `*`/`master_admin` except for real operators
- Treat `GATEWAY_SECRET_KEY` like any other production secret (`openssl rand -hex 32`, stored only in `.env`, never committed)
- Monitor `auth_audit` (`tools/user-manager.py audit`) for repeated `login_failed`/`lockout` events

### MQTT
- Client certificates are deprecated -- use username/password (`tools/mqtt-auth-manager.sh`) for all new devices
- TLS 1.2 only on the external listener (port 42690); internal broker (port 1883) stays container-network-only

---

## 5. Troubleshooting

| Symptom | Cause | Resolution |
|---------|-------|------------|
| `401 Unauthorized` on `/alert`/`/test`/`/docs` | Missing/invalid Basic or Bearer credentials | Verify the account exists and is `active`; `tools/user-manager.py show <username>` |
| `403 Forbidden` on `POST /alert` | Authenticated, but no `alerts:write` scope for the effective client (or clientless without `master_admin`) | `tools/user-manager.py add-scope <role> <client> alerts:write` then `grant-role` |
| `403 Forbidden` on `/api/logs/*` | No `logs:read` scope for that file's client folder, or a `master_admin`-only path (`ops/**`, flat files) | Grant a `logs:read` scope for that client, or use a `master_admin` account for ops logs |
| `429 Too Many Requests` on `/login` | 20 failed logins from this IP within 10 minutes | Wait for the trailing window to clear (`IPThrottle.WINDOW_SECONDS = 600`), or investigate the source |
| Generic "Invalid username or password" every time, even with the right password | Account is locked out (5 fails → 900s) | Wait 900s, or have a `master_admin` run `tools/user-manager.py set-password`/`enable` to clear the lock immediately |
| `503` on `POST /alert` | `CredentialCache` had no fresh/stale answer and the live DB check also failed | Check Postgres connectivity/health (`GET /health`'s `db` field) |
| Login/dashboard/admin pages 302-redirect to `/login` unexpectedly | Session cookie expired (12h sliding) or the account was disabled since the last request | Log in again; if disabled, contact a `master_admin` |
| `400` on any `/admin/users*` POST | Missing/invalid CSRF token | Ensure the form includes `layout.html`'s `csrf_token` field; don't hand-craft admin POSTs without it |
| `http://<host>:8080` connection refused/times out | v2.0.0 removed the host port publish -- HTTPS-only now | Use `https://alert.d-t.pw`; update any sender config hardcoding the old direct address |

This document covers the HTTP Gateway's authentication/authorization model in full. For MQTT device provisioning and TLS specifics, see `MQTT_AUTH.md`; for the alert payload schema and the `POST /alert` breaking-change note, see `CLIENTNODE.md`.
