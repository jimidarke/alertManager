# Alert Manager — Android Mobile App: Architecture & Prototype Scope

**Status:** Scoping document (no code exists yet)
**Written:** 2026-08-05
**Audience:** An implementing developer/model building a working prototype. This document resolves the major architectural decisions up front; follow it as written unless a decision is explicitly marked "open".

---

## 1. What we're building

A native Android app for the Alert Manager (`alert.d-t.pw`) that lets a user:

1. **Log in** with local email/username + password (the default for now), resolving to an account in the existing Postgres identity store (RBAC roles/scopes). **Google SSO is implemented but gated behind a feature flag, off by default** until the bugs are worked out. See §4.1; decided 2026-08-05.
2. **See alerts and logs scoped to their permissions** — the same `logs:read` / `master_admin` visibility rules the web dashboard and log viewer enforce.
3. **Filter** by alert level, category, client, and date range; search text.
4. **Receive push notifications** for critical (and configurably, lower-level) alerts.
5. **Configure notification thresholds and quiet hours** per user, persisted server-side.

Out of scope for the prototype: alert submission from the phone, admin/user management, LED sign control, analytics reports, iOS.

---

## 2. Current server reality (verified against source, 2026-08-05)

The app cannot be built against the server as-is. Facts that drive the design:

| Fact | Where | Consequence |
|---|---|---|
| Bearer tokens (`amt_` + 43 url-safe chars, SHA-256 stored in `auth_tokens`) already authenticate the `api` and `logs` blueprints | `src/userstore/store.py:100`, `src/gateway/principal.py:236-239` | Mobile API auth = Bearer token (obtained via the §4.1 token endpoint — password grant now, Google SSO exchange once the flag is on). No cookies/CSRF on mobile. |
| **No *self-service* HTTP endpoint issues a token.** As of v3.1.0 a `master_admin` can issue one from the web UI (`POST /admin/users/<username>/tokens/new`) as well as the CLI (`tools/user-manager.py create-token`) — but both are operator-driven, session-or-argv, and cannot be called by an app holding only a user's password | `src/gateway/admin/routes.py`, `tools/user-manager.py:276-291` | Still must add a login→token endpoint (§4.1). The admin route is not a substitute: no password grant, session+CSRF required, `master_admin` only. |
| Dashboard alert-query endpoints (`/api/dashboard/summary`, `/api/dashboard/groups`) are **session-only** and shaped for the grouped web view (latest 30 per client/source), not a general paginated query | `src/gateway/dashboard/routes.py` | Must add a general Bearer-capable alert query API (§4.2). |
| The `alerts` Postgres table (weekly-partitioned, indexed on client+timestamp, level+timestamp, GIN metadata, fulltext on title+message) already holds every alert via `database-logger` | `sql/init/01-schema.sql:6-46`, `src/database_logger.py:215-236` | The alert feed reads from Postgres, **not** by parsing log text files. Indexes already support every filter we need. |
| `/api/logs/list|tail|chunk` already work with Bearer auth and enforce per-client RBAC | `src/gateway/logs/routes.py`, `logs/files.py:169-191` | Raw log viewer in the app reuses these verbatim. Zero server work. |
| `GET /api/whoami` works with Bearer | `src/gateway/auth/routes.py:179-223` | Used for session validation + role display. |
| No push infrastructure of any kind exists | — | Must add a push dispatcher service + device/prefs tables (§5). |
| No CORS, no API rate limiting (only login IP throttle); TLS terminated by nginx-proxy; gunicorn 1 worker × 8 gthreads; in-process caches are single-worker-dependent | `docker-compose.yml:241-245` | Native app needs no CORS. Keep new endpoints cheap; don't add per-request DB writes on hot paths. |
| External MQTT (`:42690`) is **ESP32 LED devices only** by policy (CLAUDE.md v2.0.2) | CLAUDE.md | The app must NOT connect to MQTT. All app traffic is HTTPS. Push comes via a push service, not a phone-side MQTT subscription (also battery-hostile). |
| Timestamps are Unix epoch seconds everywhere | CLAUDE.md | App stores epoch, converts to device-local time for display only. |

---

## 3. Architecture overview

```
┌─────────────── Android app (Kotlin) ───────────────┐
│ Compose UI ← ViewModel (StateFlow) ← Repository     │
│                     │                    │          │
│              Retrofit/OkHttp      FCM SDK (receive) │
└─────────┬──────────────────────────────▲───────────┘
          │ HTTPS (Bearer)               │ push
          ▼                              │
  nginx-proxy → http-gateway      Firebase Cloud Messaging
  (existing + new endpoints §4)          ▲
          │                              │ FCM HTTP v1 API
          ▼                              │
      PostgreSQL ◄──── push-notifier (NEW service, §5)
      (alerts, identity,        ▲ subscribes alerts/input
       + devices/prefs §4.4)    │
                          mqtt-broker (internal :1883)
```

Three workstreams, in dependency order:

- **A. Gateway API additions** (Python/Flask, existing codebase) — §4
- **B. Push-notifier service** (new `src/push_notifier.py`, same pattern as `database_logger.py`) — §5
- **C. Android app** (new repo or `mobile/android/` directory) — §6

A working read-only prototype needs only A + C. Push (B) is Phase 3.

### Decision: push transport = FCM (Firebase Cloud Messaging)

Options considered:

1. **FCM data messages** ← **chosen.** The platform standard; survives Doze; high-priority messages are the only sanctioned way to reliably wake a device for time-critical alerts; free; server side is a single OAuth2-signed HTTPS POST per device (FCM HTTP v1 API). Cost: requires a (free) Firebase project and `google-services.json` in the app; Google transits the (already non-secret) alert title/level.
2. *Self-hosted ntfy / UnifiedPush* — fits the project's self-hosting ethos and is a reasonable **later** option (the dispatcher in §5 isolates the transport behind one function, so adding an `ntfy` backend is ~50 lines), but it requires either running a battery-draining persistent connection in our app or asking users to install a separate distributor app. Not prototype material.
3. *Polling with WorkManager* — minimum interval 15 min, useless for critical alerts. Rejected as the primary path, but we keep a 6-hour WorkManager "safety-net sync" that reconciles missed notifications (FCM is at-most-once in practice).
4. *Phone-side MQTT subscription* — violates the v2.0.2 device-only MQTT policy and Doze kills it. Rejected.

**Privacy note:** send FCM **data messages only** (never "notification" messages) with minimal payload (alert id, level, title, client). The app fetches the full alert over HTTPS on tap. High priority only for levels the user's prefs mark as urgent; normal priority otherwise (Google throttles apps that overuse high priority).

### Decision: quiet hours & thresholds are enforced **server-side**, channels client-side

The server decides *whether* to send and at *what priority* (don't wake the radio just to have the phone discard the message — battery and FCM-quota best practice). The client maps levels to **Android notification channels** so the OS gives the user per-channel sound/vibration/DND control:

- `channel_critical` — IMPORTANCE_HIGH, sound + vibration. Optionally `setBypassDnd(true)` if the user grants DND-override access (Settings intent `ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS`); ship this as an opt-in toggle.
- `channel_warning` — IMPORTANCE_DEFAULT.
- `channel_info` — IMPORTANCE_LOW (silent, status-bar only).

Quiet-hours semantics (server-side, per user, stored in `notification_prefs`):
- During quiet hours, alerts **below** `quiet_override_level` are **suppressed entirely** (not queued — the app's feed is the record; a "missed during quiet hours" digest is a post-prototype nicety).
- Alerts at/above `quiet_override_level` (default `critical`) are sent as normal.
- Quiet hours are a `start_minute`/`end_minute` pair (minutes since local midnight) plus an IANA `timezone` string; the **server** evaluates them in the user's stored timezone (`zoneinfo`). Overnight ranges (e.g. 22:00→07:00) must work: `start > end` means the window wraps midnight.

---

## 4. Workstream A — Gateway API additions

All new endpoints live in the existing Flask app-factory structure (`src/gateway/`). New blueprint `mobile` (`src/gateway/mobile/`) with policy `API_ONLY` (Basic/Bearer, no cookies, CSRF-exempt like `api`), except the token-issue endpoint which takes credentials in the body. Every response follows the existing conventions: JSON, epoch timestamps, `{"status":"error","message":...}` on failure.

### 4.1 Auth redesign — Google SSO for humans, local everything else

**Decision (2026-08-05, pre-live so no compat shims needed):** human sign-in gains **"Sign in with Google" (OIDC against Google directly — no self-hosted IdP, no Firebase Auth)**, built now but **gated behind a feature flag** (`GOOGLE_SSO_ENABLED` env var in `src/gateway/config.py`, default `false`). While the flag is off, **local email/username + password is the login method** everywhere (web form and mobile `password` grant); the Google code paths are dark. Once flipped on, Google becomes the primary human sign-in and passwords demote to break-glass. Everything below the authentication step is unchanged either way: the Postgres identity store remains the source of truth for *accounts and RBAC* (roles, `(client, permission)` scopes, `master_admin`), and `amt_` bearer tokens remain the machine/API credential (SDK, curl, scripts).

**Flag behavior:** when `GOOGLE_SSO_ENABLED=false` — `/login` renders only the password form; `/auth/google/*` routes return 404; the `google_id_token` grant returns `403 {"error":"sso_disabled"}`. Client discovery: unauthenticated `GET /api/auth/methods` → `{"password": true, "google_sso": <flag>}`; the app shows/hides the Google button from this (never hardcode). The local form accepts username **or** email in the identifier field (match `users.username`, else `users.email`).

**Identity linking & provisioning:**
- Migration adds to `users`: `google_sub TEXT UNIQUE NULL` (Google's stable `sub` claim — the durable key), `email TEXT UNIQUE NULL`; `password_hash` becomes nullable (SSO-only accounts have none). Drop `must_change_password` from the mobile flow entirely — SSO-only users never see it.
- **No auto-signup.** An unknown Google identity gets `403 {"error":"no_account","detail":"ask an administrator to provision your account"}`. Admin pre-creates the user with their email (`tools/user-manager.py create-user --email ...`); first successful Google login where `email_verified=true` and email matches links `google_sub` permanently (thereafter match on `sub` only — emails can change).

**Google Cloud setup (manual, free, no Firebase required — but shares the project you'll create for FCM):** one OAuth 2.0 *Web application* client (used by the gateway redirect flow **and** as the `aud` for mobile ID tokens) plus one *Android* client (SHA-1-bound, needed by Credential Manager; issues no secrets).

**Server-side verification (shared helper, used by both flows):** validate the ID token's signature against Google's JWKS (cache keys; `authlib` or `google-auth`'s `id_token.verify_oauth2_token`), then `iss ∈ {accounts.google.com, https://accounts.google.com}`, `aud` = our web client ID, `exp`, `email_verified`. Then map `sub` → local user, load `PrincipalData` as today.

**Web flow (auth blueprint):** `GET /auth/google/start` → 302 to Google with `state` + PKCE; `GET /auth/google/callback` → code exchange, verify ID token, create the existing signed session cookie. `/login` page keeps the password form below a "Sign in with Google" button. Password path retains `IPThrottle` + account lockout unchanged.

**Mobile flow:** the app uses **Android Credential Manager** (Sign in with Google) to obtain a Google ID token on-device, then exchanges it:

`POST /api/auth/token` — body is one of two grants:
- `{"grant_type": "google_id_token", "id_token": str, "device_name": str}` — verify as above → mint token.
- `{"grant_type": "password", "username": str, "password": str, "device_name": str}` — the default grant while the SSO flag is off (break-glass thereafter); `username` accepts username or email; same `UserStore` verify path as `POST /login` including lockout + `IPThrottle` (429).

Both return `201 {"token": "amt_...", "token_id": int, "expires_at": epoch, "user": <whoami-shape>}` with `expires_at = now + 90 days` (`device_name` → `auth_tokens.name`, e.g. `"android-Pixel 8"`); write `auth_audit` (`token_issued`, with grant type).

`POST /api/auth/token/revoke` (Bearer) — revokes the presented token (logout). Sets `auth_tokens.status='revoked'`.

### 4.2 Alert query — `GET /api/alerts` (+ promote dashboard endpoints)

The core feed API, reading the `alerts` table. Auth: `SESSION_OR_API`. RBAC identical to `dashboard/routes.py:_scope_clause`: non-master-admin gets `client = ANY(visible_clients('logs:read')) AND client IS NOT NULL AND client != ''`; master_admin unfiltered.

Query params (all optional):

| Param | Type / values | Default |
|---|---|---|
| `since`, `until` | epoch seconds (filter on `timestamp`, partition-pruning friendly) | last 24 h |
| `level` | CSV of `critical,warning,notice,info,debug` | all |
| `category` | CSV of the 11 fixed categories | all |
| `client` | CSV; intersected with the caller's visible clients (never widens) | all visible |
| `q` | text — uses the existing fulltext index on `title || ' ' || message` | — |
| `offset`, `limit` | int; limit 1–200, default 50 | 0 / 50 |

Response uses the same per-alert shape as `/api/dashboard/groups`'s `alerts` entries (`id`, `timestamp` epoch int, `level`, `category`, `title`, `message`, `source`, `client`, `metadata`, `ingestion_source`) plus `{"total": int, "offset": int, "limit": int, "generated_at": epoch}`. Cap `since` at 90 days like the dashboard's `all` window. Add `GET /api/alerts/<id>` for the push-tap deep link (404 if outside the caller's scope).

Also: change the `dashboard` blueprint policy from `SESSION_ONLY` to `SESSION_OR_API` so the app can reuse `/api/dashboard/summary` for the home-screen level counts. (These endpoints are read-only GETs; CSRF is irrelevant to them and Bearer callers carry no cookies.)

### 4.3 Device registration & notification prefs

- `POST /api/devices` — Bearer. Body `{"fcm_token": str, "platform": "android", "model": str, "app_version": str}`. Upserts on `fcm_token` (a token can move between users on shared devices: last registration wins). Returns `{"device_id": int}`.
- `DELETE /api/devices/<id>` — Bearer, own devices only. Called on logout.
- `GET /api/notifications/prefs` / `PUT /api/notifications/prefs` — Bearer. One prefs row per **user** (not per device — simpler, and matches "my thresholds" mental model). PUT body = full document:

```json
{
  "enabled": true,
  "min_level": "warning",
  "categories": null,
  "clients": null,
  "quiet_hours": {
    "enabled": true,
    "start_minute": 1320,
    "end_minute": 420,
    "timezone": "America/Vancouver",
    "override_level": "critical"
  }
}
```

`min_level` = lowest level that generates a push (ordering: debug < info < notice < warning < critical). `categories`/`clients`: `null` = all (within RBAC), else allowlist arrays. Server validates values against the fixed level/category enums and `timezone` against `zoneinfo.available_timezones()`. Defaults on first login: `enabled=true, min_level="critical"`, quiet hours disabled.

### 4.4 Migration `sql/migrations/0004_mobile.sql`

```sql
CREATE TABLE mobile_devices (
  id BIGSERIAL PRIMARY KEY,
  user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  fcm_token TEXT NOT NULL UNIQUE,
  platform VARCHAR(20) NOT NULL DEFAULT 'android',
  model TEXT, app_version TEXT,
  created_at BIGINT NOT NULL, last_seen_at BIGINT NOT NULL,
  status VARCHAR(20) NOT NULL DEFAULT 'active'   -- active | stale (FCM said UNREGISTERED)
);
CREATE INDEX idx_mobile_devices_user ON mobile_devices(user_id) WHERE status = 'active';

CREATE TABLE notification_prefs (
  user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  min_level VARCHAR(20) NOT NULL DEFAULT 'critical',
  categories TEXT[] NULL,          -- NULL = all
  clients TEXT[] NULL,             -- NULL = all visible via RBAC
  quiet_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  quiet_start_minute INT NOT NULL DEFAULT 1320,
  quiet_end_minute INT NOT NULL DEFAULT 420,
  quiet_timezone TEXT NOT NULL DEFAULT 'UTC',
  quiet_override_level VARCHAR(20) NOT NULL DEFAULT 'critical',
  updated_at BIGINT NOT NULL
);
```

---

## 5. Workstream B — Push-notifier service (`src/push_notifier.py`)

A standalone consumer modeled directly on `src/database_logger.py`: subscribes to MQTT `alerts/input` (internal broker), one new compose service under the `primary` profile, same base image, `command: python3 src/push_notifier.py --config /data/config/docker.json`.

Per received alert (already normalized by the time it's on `alerts/input`):

1. **Load candidate users** — every user with an active device, prefs `enabled`, and RBAC visibility of the alert's `client` (`logs:read` scope covering it, or master_admin; clientless alerts → master_admins only). Cache the user/prefs/device set in memory, refresh every 60 s — do **not** hit Postgres per alert.
2. **Threshold filter** — alert level ≥ user's `min_level`; category/client allowlists if set.
3. **Quiet hours** — evaluate now() in the user's `quiet_timezone`; suppress if inside the window and level < `quiet_override_level`. Wrap-midnight rule: inside ⇔ (`start ≤ end` ? `start ≤ m < end` : `m ≥ start or m < end`).
4. **Send** via FCM HTTP v1 (`https://fcm.googleapis.com/v1/projects/{project}/messages:send`), authenticated with a service-account JSON (path from env `FCM_SERVICE_ACCOUNT_FILE`; use `google-auth` to mint OAuth2 tokens — do not pull in the full `firebase-admin` SDK). Data-only message:

```json
{
  "message": {
    "token": "<device fcm_token>",
    "android": {"priority": "HIGH"},        // HIGH only if level >= warning; else NORMAL
    "data": {
      "alert_id": "123", "level": "critical", "category": "system",
      "title": "Disk failure", "client": "prod-web", "timestamp": "1754400000"
    }
  }
}
```

5. **Housekeeping** — on FCM 404/`UNREGISTERED`, mark the device `stale` (stop sending; the app re-registers on next launch). Log sends/failures to ops log; skip (don't crash) on FCM outage — per Development Notes this service is a *pathway-like* consumer, but push delivery is inherently best-effort, so fail-open like `EmailLogWriter`, not fail-fast.
6. **Storm guard** — hard cap: max 1 push per user per 30 s per level; when exceeded, coalesce into one "N alerts (critical) in the last 30s" push. Without this, an alert storm burns FCM quota and users disable notifications.

The FCM call sits behind a `PushTransport` interface (one method: `send(device, data, priority)`) so an ntfy/UnifiedPush transport can be added later without touching filtering logic.

---

## 6. Workstream C — Android app

### 6.1 Stack (current best practice, verified 2026)

| Concern | Choice |
|---|---|
| Language / UI | Kotlin, **Jetpack Compose** (Material 3), single-Activity |
| Architecture | MVVM: Compose ← ViewModel (`StateFlow<UiState>`, sealed classes) ← Repository. Skip the UseCase layer — overkill at this size. |
| DI | **Hilt** (`hiltViewModel()` per nav destination) |
| Networking | **Retrofit + OkHttp + kotlinx.serialization**; OkHttp `Authenticator`/interceptor adds `Authorization: Bearer` and funnels 401 → logout flow |
| Paging | **Paging 3** against `offset`/`limit` of `GET /api/alerts` |
| Persistence | **Jetpack DataStore** (prefs, filter state). Token in **EncryptedSharedPreferences** (Keystore-backed). **No Room/offline cache in the prototype** — the feed is network-only. |
| Push | Firebase Messaging SDK (`FirebaseMessagingService`), data messages only |
| Background | WorkManager: 6 h safety-net sync + `onNewToken` re-registration |
| Min/target SDK | minSdk 26 (notification channels), targetSdk = current (36) |
| Navigation | Navigation-Compose, routes: `login`, `feed`, `alert/{id}`, `logs`, `logviewer/{path}`, `settings` |

Module layout (single Gradle module is fine for the prototype; package-by-feature):

```
mobile/android/app/src/main/java/pw/dt/alertmanager/
├── di/            # Hilt modules (network, auth)
├── data/          # Retrofit API interface, DTOs (mirror server JSON exactly), AuthRepository, AlertRepository, LogRepository, PrefsRepository
├── push/          # AlertFirebaseMessagingService, NotificationChannels, Notifier
└── ui/            # login/, feed/, detail/, logs/, settings/ — each: Screen.kt + ViewModel.kt
```

### 6.2 Screens

1. **Login** — server URL field (default `https://alert.d-t.pw`, editable — keeps test.json/local dev usable). On load, call `GET /api/auth/methods`. Default UI: **email/username + password form** → `password` grant. If the server reports `google_sso: true`, also show a **"Sign in with Google"** button (Android Credential Manager, `GetGoogleIdOption` with the *web* client ID as audience) → `google_id_token` grant. Either way: store token + `whoami`, fetch prefs (FCM device registration only once push ships — App Phase 2). Errors: `no_account` (403, "ask admin to provision"), `sso_disabled` (403), 401 bad creds, lockout message, 429 throttled.
2. **Alert feed** (home) — level-count summary chips across the top (`/api/dashboard/summary`), then paged alert list (level color-dot, title, client badge, relative time). Filter bar: level multi-select chips, category dropdown, client dropdown (populated from `whoami`/scope clients seen in results), date-range picker (maps to `since`/`until`), debounced search box (`q`). Pull-to-refresh. Level colors: reuse the web viewer palette (`docs/THEME_SPECIFICATION.md`).
3. **Alert detail** — full message (multiline preserved — DB rows aren't sanitized), metadata key/value table, source, ingestion_source, absolute local + epoch time. Reached from list tap or notification tap (deep link `alertmanager://alert/{id}` → fetch `GET /api/alerts/{id}`).
4. **Log browser** — `GET /api/logs/list` → group by folder (client). Tap file → viewer: initial load via `/api/logs/chunk` from EOF (one ~1 MB page, "load earlier" loads previous chunk), live tail toggle polling `/api/logs/tail?position=` every 10 s while screen is foregrounded (mirror the web viewer's contract; positions are byte offsets, keep them opaque). Client-side level color-coding by substring match (`[critical]` etc.), search-in-page.
5. **Settings** — notification master toggle, min-level picker, quiet hours (enable, start/end time pickers, timezone defaulting to device zone, "critical overrides quiet hours" toggle) → `PUT /api/notifications/prefs`; per-channel OS settings shortcut (`ACTION_CHANNEL_NOTIFICATION_SETTINGS`); DND-override opt-in (guides the user through the policy-access grant, then sets `setBypassDnd(true)` on the critical channel); account info from `whoami`; logout (revoke token + `DELETE /api/devices/{id}` + clear storage).

### 6.3 Push handling on the client

- On app start: create the three channels (idempotent). Android 13+: request `POST_NOTIFICATIONS` runtime permission right after first login (with rationale UI).
- `AlertFirebaseMessagingService.onMessageReceived`: build the notification from the data payload — channel by `level` (critical→`channel_critical`, warning/notice→`channel_warning`, info/debug→`channel_info`), tap `PendingIntent` deep-links to alert detail. Group by client using `setGroup()` + a summary notification so storms collapse.
- `onNewToken`: persist and re-`POST /api/devices` (via WorkManager, since it can fire while logged-out — queue until authenticated).
- Coalesced storm pushes (`"count" > 1` in data payload, §5.6) render as "N critical alerts" and deep-link to the filtered feed instead of a detail screen.

### 6.4 Error handling rules

- 401 anywhere (token expired/revoked) → clear token, navigate to Login with "session expired".
- 403 on a specific resource → inline "no access" state, do not log out.
- 503 from `/api/alerts` (DB down — mirrors dashboard behavior) → full-screen retry state showing the server's `message`.
- Network failure → cached-nothing prototype shows retry UI; keep last successful page in memory so rotation doesn't blank the screen (ViewModel survives).
- Never retry POSTs automatically; GETs may retry once on IOException.

---

## 7. Delivery phases (each independently shippable)

| Phase | Contents | Done when |
|---|---|---|
| **1. Server auth + read API** | §4.1: `/api/auth/token` grants + `/api/auth/methods` + Google OIDC code paths **behind `GOOGLE_SSO_ENABLED=false`**, `users.google_sub`/`email` migration; §4.2 `/api/alerts` (+ dashboard policy change); migration 0004 (tables can ship early); shell tests in `tools/tests/test_mobile_api.sh` | Password grant issues a working token and honors lockout/throttle; `curl -H "Authorization: Bearer amt_..." https://.../api/alerts?level=critical` returns scoped JSON; with flag off, `/auth/google/*` 404s and `google_id_token` grant returns `sso_disabled` |
| **2. App Phase 1 — read-only, password auth** | Login (password form; Google button auto-appears later via `/api/auth/methods`), feed + filters, detail, log browser, settings (local-only), against Phase 1 | A non-admin user sees exactly their clients' alerts/logs; master_admin sees ops logs too |
| **3. App Phase 2 — push** | Firebase project, §4.3 device/prefs endpoints, §5 push-notifier service, app push handling + settings wired to server prefs | Critical alert POSTed via `send-http-alert.sh` rings a locked phone in <10 s; quiet hours verifiably suppress a warning but pass a critical |

**SSO enablement is not a phase** — it's an ops flag-flip, done whenever local auth is proven stable: create the Google OAuth clients, set `GOOGLE_SSO_ENABLED=true` + client IDs in `.env`, restart the gateway. The app and web UI adapt automatically via `/api/auth/methods`. Test the full Google flow end-to-end (web + mobile, `no_account` path included) before announcing it.

Update `docs/CLIENTNODE.md` (new endpoints) and `docs/AUTH.md` (mobile token flow) with Phase 1; add the push-notifier service to `docs/DOCKER.md` with Phase 3. Docker builds/restarts are run manually server-side by the operator (project rule) — implementers provide exact commands.

## 8. Risks & open items

- **Google Cloud setup** is a manual, human step but **blocks nothing initially** (SSO flag defaults off): OAuth web + Android client IDs are needed only at SSO flag-flip; FCM service-account key + `google-services.json` at App Phase 2 (push). All zero-cost, one shared project.
- **Google as auth dependency** (once the flag is on): a Google outage blocks SSO login (existing sessions/tokens keep working — verification is local). Mitigated by the password grant remaining as break-glass; keep at least one local-password `master_admin`.
- **Untested dark code**: the Google OIDC paths ship disabled and therefore unexercised — before flag-flip, run a dedicated end-to-end test pass (see §7); don't assume they work because they compiled.
- **Single gunicorn worker**: new endpoints add DB reads on a 1-worker/8-thread gateway. Fine at this scale; keep `/api/alerts` to one query (window functions for `total`, or drop exact totals and use `has_more`).
- **Token expiry UX**: 90-day expiry means a quarterly re-login. Acceptable; refresh-token flows are explicitly out of scope.
- **Open item**: whether the app should live in this repo (`mobile/android/`) or its own repo. Doc assumes in-repo; nothing else depends on the choice.
- **Open item (deferred)**: quiet-hours "morning digest" of suppressed alerts; ntfy/UnifiedPush transport; Room offline cache; `thresholds:write` scope integration (schema reserved, unused today — per-user prefs above deliberately don't use it).

## 9. External references consulted

- Android app architecture (Compose + MVVM + Hilt + StateFlow): [Jetpack Compose 2026 production guide](https://softaims.com/blog/jetpack-compose-android-production-2026), [Hilt with Jetpack (developer.android.com)](https://developer.android.com/training/dependency-injection/hilt-jetpack), [MVVM/Hilt/Compose structuring](https://medium.com/@rushabhprajapati20/crafting-a-scalable-android-architecture-with-mvvm-jetpack-compose-hilt-and-usecase-0a0ba32ca438)
- FCM priorities & notification best practices: [Notifying your users with FCM (Android Developers Blog)](https://android-developers.googleblog.com/2018/09/notifying-your-users-with-fcm.html), [Android push notifications 2026 setup & best practices](https://www.pushwoosh.com/blog/android-push-notifications/), [Android push notifications guide (Bugfender)](https://bugfender.com/blog/android-push-notifications/)
- Critical alerts / DND bypass: [Implementing Critical Alerts on Android](https://medium.com/@surendar1006/implementing-critical-alerts-on-android-aa49b4d75705)
- Self-hosted push landscape (evaluated, deferred): [UnifiedPush](https://unifiedpush.org/news/20221218_unifiedpush/), [FCM alternatives (emteria)](https://emteria.com/blog/fcm-alternatives), [Google Push alternatives (Tuta)](https://tuta.com/blog/google-push-alternative)
- Notification-system design patterns: [How to Design a Notification System (2026)](https://www.systemdesignhandbook.com/guides/design-a-notification-system/)
- Google SSO: [Sign in with Google via Credential Manager (developer.android.com)](https://developer.android.com/identity/sign-in/credential-manager-siwg), [Verifying Google ID tokens](https://developers.google.com/identity/sign-in/web/backend-auth)
