# Auth Reset & Tier-2 Cleanup — Design

**Date:** 2026-08-07
**Status:** Approved (design), pending implementation plan
**Version target:** 3.0.0 — breaking. Two permission values are removed from the
schema CHECK, a column is dropped, and every credential in existence is
invalidated. v2.0.0 took a major bump for a change of the same kind (the RBAC
rewrite), so this matches precedent. The HTTP alert payload contract does not change.

## Goal

Wipe the identity store on both deployments (local, `alert-vps`) and rebuild it
from one bootstrap `master_admin`. Use the fact that no rows survive to delete
the accumulated auth tech debt that would otherwise require migrations to unwind.

## Decisions

| Question | Decision |
|---|---|
| Blast radius | Full reset, prod + local. Nothing migrates forward. |
| Post-reset state | Exactly one bootstrap `master_admin`. All other accounts created by hand afterwards. |
| Cleanup tier | Tier 2 — reset hygiene plus the defects the reset exposes. No structural refactor. |
| `must_change_password` | Build the change-password route rather than drop the column. |

## Non-goals (deferred to a later pass)

These are real and documented, but out of scope here:

- Collapsing the two parallel Basic/Bearer resolution stacks (`gateway/principal.py`
  vs `gateway/api/routes.py` — two `require_auth`, two byte-identical 401 builders).
- `auth_audit` retention/pruning. The table grows unbounded and takes one row per
  Basic-auth API request. The reset resets it to zero rows, which buys time.
- Extending `IPThrottle` beyond `POST /login`; Basic-auth brute force against
  `/api/*` remains throttle-free (account lockout still applies).
- Splitting the 990-line `src/userstore/store.py`.
- Centralizing the 6 copies of `_normalize_client` / 3 of `_normalize_username`.
- Rewriting git history to purge previously committed secrets. Mitigation is
  rotation, not rewrite.

---

## 1. Scope of the wipe

Six tables, dropped and recreated:
`auth_audit`, `auth_tokens`, `role_scopes`, `user_roles`, `roles`, `users`.

Explicitly **untouched**: `alerts` (and its partitions), `email_log`,
`schema_migrations` rows for versions 1 and 3. `sql/init/` contains only the
`alerts` table and is not involved.

## 2. Migration handling

`sql/migrations/0002_identity.sql` is **edited in place** rather than amended by a
new `0004_identity_cleanup.sql`.

**Rationale.** `db_migrate.py` skips any migration with a row in
`schema_migrations`, so the reset deletes that row and re-applies the edited file.
A `0004` that dropped columns `0002` created would be permanent archaeology
encoding concepts that never worked. There are exactly two databases and both are
reset in the same operation.

**Risk accepted.** A third database that ran the old `0002` and never gets reset
would silently diverge. A header comment in `0002_identity.sql` records this as a
deliberate one-time exception to migration immutability, dated and tied to this spec.

## 3. Schema changes in `0002_identity.sql`

### 3.1 Drop `users.expires_at`

Enforced at `store.py:602-604` (`verify_login`) and `store.py:753`
(`verify_token`, owning-user expiry), but the only caller that ever passed a real
value is `tools/migrate-users-json.py`, which this spec deletes. There is no CLI
flag, no admin-UI field, and no update path — every post-reset account would carry
`NULL` forever.

Cascade:
- Remove the column from the `CREATE TABLE`.
- Remove both enforcement branches in `store.py`.
- Remove the `expires_at` kwarg from `UserStore.create_user`.
- Remove it from the `get_user` / `list_users` SELECT column lists.
- Remove `"expires_at"` from `cmd_list`'s column list at `tools/user-manager.py:211`.
- Remove it from `src/gateway/templates/admin/users_list.html`.

`auth_tokens.expires_at` is **kept** — that one is live (enforced `store.py:749`,
settable via `create-token --expires-at`).

### 3.2 Narrow the permission CHECK

```sql
CHECK (permission IN ('*', 'logs:read', 'alerts:write'))
```

Removes `thresholds:write` and `users:manage`. Both are offered as admin-UI
checkboxes today and enforced by zero code — an operator can grant a permission
nothing reads. Admin routes gate on `require_master_admin` → `is_master_admin()`,
never on `users:manage`.

Cascade: `ALLOWED_PERMISSIONS` (`store.py:111`) narrows to match. The admin form
derives its checkboxes from that constant (`admin/routes.py:395`) and re-validates
against it (`:495`, `:596`), so the UI and validation follow automatically.

### 3.3 Kept as-is

`must_change_password` (Section 4 gives it teeth), the `master_admin` seed rows,
all existing indexes and FK behaviours — including the deliberate
`auth_tokens.user_id ... ON DELETE RESTRICT`.

## 4. Change-password route

**Routes** (both on `auth_bp`, session auth only, CSRF-protected via the existing
`CSRFProtect` instance):

- `GET /change-password` — renders `src/gateway/templates/change_password.html`.
- `POST /change-password` — requires `current_password`, `new_password`,
  `confirm_password`.

**Validation**, in order: current password verifies against the stored hash;
new matches confirm; new is at least 12 characters; new differs from current.
Any failure re-renders the form with an error and writes no state.

**On success:** `UserStore.set_password(username, new, must_change_password=False)`,
an `auth_audit` row with event `password_changed`, then redirect to `/`.

**Enforcement:** an app-level `before_request` hook in `create_app()`. If the
request resolves to a *session* principal with `must_change_password` true, and the
endpoint is not `auth.change_password`, `auth.logout`, `auth.login`, or static,
redirect to `/change-password`.

**Avoiding a doubled DB round trip.** `_resolve_session` (`principal.py:294`)
re-fetches `get_principal()` on every call and is deliberately uncached; `require_auth`
then stores the result on `g.principal`. A `before_request` hook that resolved the
session independently would issue a *second* identical query per request. So
`_resolve_session` memoizes its result on `g` (a private `g._session_principal`
sentinel, distinct from the public `g.principal` the decorator sets) for the
lifetime of the request. The hook and the decorator then share one fetch, which
also halves the cost of the existing dashboard/logs hot path. The memo is
request-scoped, so the "disabled account dies on its next request" guarantee in
`_resolve_session`'s docstring is preserved exactly.

**Session-only by design.** Basic/Bearer principals are never blocked. A service
account created through the admin UI gets `must_change_password=True` today
(`admin/routes.py:506`) and must keep working against `POST /alert`. To stop
setting a meaningless flag on machine accounts, the admin user-creation form gains
a **"Require password change at next login"** checkbox (default checked), and
`admin/routes.py` honours it rather than hardcoding `True`.

## 5. CLI gaps the reset exposes

### 5.1 `user-manager.py list-tokens`

`UserStore.list_tokens()` (`store.py:760`) has **zero callers**. Consequence:
`docs/CLIENTNODE.md:108` instructs operators to find a token id via `list`/`show`
to revoke it, but neither subcommand prints tokens — ids are recoverable only by
scraping `auth_audit`. Token revocation is effectively broken.

Add `list-tokens [username]` (all tokens when username omitted) printing
`id, name, username, status, expires_at, last_used_at, created_at`. Never prints
token material — only the sha256 is stored. Fix `docs/CLIENTNODE.md:108`.

### 5.2 `user-manager.py unlock`

Five failed logins lock an account for 900 seconds with no operator override; it
clears only via successful login, `set-password`, or `enable`. Add
`UserStore.unlock_user(username)` — sets `failed_attempts = 0`, `locked_until = NULL`,
writes an `auth_audit` row with event `unlock` — and an `unlock <username>` subcommand.

## 6. Purge list

Deleted outright:

| Path | Note |
|---|---|
| `data/config/http-gateway/users.json` | 8 accounts, single-round salted-SHA-256. Bind-mounted `:ro` into the container. Gitignored now, but present in history at `32f3ab4` / `882fe43`. |
| `data/config/http-gateway/tokens.json` | `{"tokens": {}}`, zero readers. |
| `tools/migrate-users-json.py` | 447 lines. Its only input is the file above. |
| `tests/test_migrate_users_json.py` | 478 lines. |
| `data/config/docker.json` `"auth"` block (L245-249) | Tracked in git, read by nothing. |
| `examples/config-database.json` L123 `users_file`, L125 `rate_limiting` | Dead keys. |
| `docs/AUTH.md` §2.10 | Pre-v2.0.0 migration instructions. |

Hardcoded credentials replaced with env lookups:

| Path | Problem |
|---|---|
| `tools/send-http-alert.sh:15-16` | `device-monitor` / `IQiM9QYv3NnuTKM` literal in git. The account exists in neither store. |
| `tools/validate-https-flow.sh:12-13` | Same pair — and **prints the password to stdout** at L311-312, L327-335. |
| `tools/tests/test_normalization.sh:6,20,33` | `-u admin:password` literal. |

Each becomes `"${ALERT_USER:?set ALERT_USER}"` / `"${ALERT_PASSWORD:?set ALERT_PASSWORD}"`,
and the echo statements stop printing the secret.

`tools/certs/client.key` — a committed private key for the deprecated MQTT
client-certificate path — is deleted. Client-cert auth is already deprecated
(`tools/README.md:13`) and the TLS listener sets `require_certificate false`, so
nothing depends on it. Deleting from the working tree does not remove it from
history; it is unused, so no revocation is required.

## 7. Documentation

- `docs/AUTH.md` — drop §2.10; update §2.4 permission table; remove `expires_at`
  from the account model; document `/change-password`, `list-tokens`, `unlock`.
- `docs/CLIENTNODE.md` — fix the L108 token-revocation instruction.
- `docs/HTTP_GATEWAY.md` — add the two `/change-password` routes.
- `CLAUDE.md` — narrow the permission table; drop the `thresholds:write` /
  `users:manage` "reserved" rows; correct the stale claim that there is no Python
  unit-test suite (there are 37 files under `tests/`, with `requirements-dev.txt`).
- `CHANGELOG.md` — `[3.0.0]` breaking entry.
- `VERSION` — `2.0.3` → `3.0.0` via `./tools/bump-version.sh major`.

## 8. Testing

Baseline before any change: **336 passed, 5 skipped** across
`test_userstore.py`, `test_gateway_principal.py`, `test_gateway_auth_routes.py`,
`test_user_manager_cli.py`, `test_identity_migration_sql.py`,
`test_gateway_admin_routes.py`.

- **Delete:** `tests/test_migrate_users_json.py`.
- **Update:** `test_identity_migration_sql.py` (dropped column, narrowed CHECK),
  `test_userstore.py` (no `expires_at`; new `unlock_user`), `test_user_manager_cli.py`
  (`list-tokens`, `unlock`, `list` columns), `test_gateway_admin_routes.py` and
  `test_gateway_admin_templates.py` (permission checkboxes, new must-change checkbox),
  `test_gateway_principal.py`.
- **New:** change-password route tests in `test_gateway_auth_routes.py` — happy path,
  wrong current password, mismatch, too short, unchanged password, CSRF rejection,
  and the `before_request` redirect (including that Basic/Bearer requests are *not*
  redirected).

Full `pytest tests/` must be green before the prod reset runs.

## 9. Rollout

Ordered, with Docker steps run manually server-side per `CLAUDE.md`.

1. Land all code, schema, test and doc changes; full suite green.
2. **Local:** `tools/reset-identity.py` → `src/db_migrate.py` → `user-manager.py init-admin`.
   Verify login, dashboard, `/change-password`, and a `POST /alert`.
3. **Prod:** rebuild the image, then the same three commands via `docker exec`.
4. Rotate `GATEWAY_SECRET_KEY` in `.env` and restart `http-gateway`. This
   invalidates every outstanding session cookie signed against the old key.
5. Recreate roles, human accounts and service accounts as needed via the admin UI
   or `user-manager.py`. Every existing integration is dead until re-credentialed —
   accepted.

### `tools/reset-identity.py`

Destructive; refuses to run without an interactive typed confirmation — the
operator must enter the literal string `RESET IDENTITY` at a prompt. There is no
`--force` / `--yes` flag, because the one place this runs unattended is exactly the
place it must not. Does exactly two things: `DROP TABLE ... CASCADE` on the six tables, then
`DELETE FROM schema_migrations WHERE version = 2`. It deliberately does **not**
re-migrate or seed, so each stage of the rollout is separately visible and
auditable. Reuses `db_migrate.py`'s `POSTGRES_*` env resolution and `--dsn` override.
