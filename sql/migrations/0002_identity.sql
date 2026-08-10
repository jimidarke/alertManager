-- Alert Manager - Migration 0002: Identity Schema
--
-- Phase 4 (Gateway v2, v2.0.0 -- breaking release: passwords reset,
-- users.json retired, HTTPS-only) of the overhaul plan
-- (docs/OVERHAUL_PLAN.md, section 4.1) replaces the current plaintext-JSON
-- account store (data/config/http-gateway/users.json, tokens.json) with a
-- Postgres-backed identity schema: users, roles, per-(client,permission)
-- scopes, hashed Bearer tokens, and an auth audit trail.
--
-- All new time columns are BIGINT epoch seconds, per this project's stated
-- convention for everything added after alerts.timestamp (which stays
-- TIMESTAMPTZ only because it drives that table's partition-range
-- boundaries -- see sql/migrations/0003_email_log.sql's header for the same
-- reasoning applied to attempted_at).
--
-- NOTE: this file is numbered 0002 even though sql/migrations/0003_email_log.sql
-- was written and shipped first on this branch -- 0003's header already
-- reserves 0002 for this migration. src/db_migrate.py applies migrations in
-- filename-sorted order (0001 -> 0002 -> 0003), but neither this file nor
-- 0003 references anything the other creates, so the two are
-- order-independent at apply time.
--
-- Later tasks build the code that uses this schema (Task 15:
-- src/userstore/, Task 16: tools/user-manager.py, Task 17:
-- tools/migrate-users-json.py, Task 18+: src/gateway/). This migration only
-- creates the schema plus the master_admin system role/scope seed row that
-- init-admin (Task 16) and the users.json migration script (Task 17) both
-- depend on already existing.
--
-- Deliberately NOT included: a per-user `rate_limit` field. users.json had
-- one (data/config/http-gateway/users.json) but it was never enforced
-- anywhere in the codebase; account lockout (users.failed_attempts /
-- users.locked_until, below) replaces the security intent without carrying
-- forward a dead field.
--
-- ---------------------------------------------------------------------------
-- EDITED IN PLACE 2026-08-07 (v3.0.0) -- a deliberate ONE-TIME exception to
-- migration immutability, per docs/superpowers/specs/2026-08-07-auth-reset-design.md.
--
-- The v3.0.0 auth reset drops and re-applies this migration on both
-- deployments (local + alert-vps) with zero surviving rows, so the two
-- changes below were made here rather than stacked into a 0004 that would
-- have dropped columns this file had just created:
--
--   1. users.expires_at REMOVED. It was read by verify_login()/verify_token()
--      but the only caller that ever set a non-NULL value was
--      tools/migrate-users-json.py, now deleted. No CLI flag, no admin-UI
--      field, no update path ever existed -- every account carried NULL.
--      (auth_tokens.expires_at is untouched; that one is live.)
--   2. chk_role_scopes_permission NARROWED to ('*', 'logs:read',
--      'alerts:write'). 'thresholds:write' and 'users:manage' were
--      grantable via the admin UI and enforced by zero code -- admin routes
--      gate on require_master_admin -> is_master_admin(), never on
--      'users:manage'.
--
-- If a database exists that applied the PRE-2026-08-07 version of this file
-- and was not part of that reset, it will silently diverge from this schema.
-- Reconcile it by re-running the reset (tools/reset-identity.py), not by
-- hand-patching.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,

    -- Login name. VARCHAR(255) matches the identifier-column width used
    -- throughout sql/init/01-schema.sql (alerts.source / alerts.client).
    -- Always lowercase -- enforced below by chk_users_username_lowercase
    -- rather than relying on application code alone, the same
    -- defense-in-depth CLAUDE.md documents for alerts.level/category.
    username VARCHAR(255) NOT NULL UNIQUE,

    -- argon2id hash (Task 15: src/userstore/passwords.py). TEXT, not
    -- VARCHAR(n) -- argon2id's encoded hash format is long and its length
    -- varies with the configured cost parameters, so no fixed cap is
    -- meaningful here (unlike the bounded identifier columns above).
    password_hash TEXT NOT NULL,

    -- 'disabled' deactivates an account without deleting its history (role
    -- grants, audit rows) -- mirrors users.json's existing status field.
    status VARCHAR(20) NOT NULL,

    -- Client a `node` user's alerts are attributed to when the payload
    -- omits `client` (CLAUDE.md "Client Name Inheritance"). NULL for
    -- master_admin and any user with no default. Same width as
    -- alerts.client.
    default_client VARCHAR(255),

    -- Forces a password change on next login (e.g. init-admin's generated
    -- password, or an admin-issued reset).
    must_change_password BOOLEAN NOT NULL DEFAULT FALSE,

    -- Consecutive failed login attempts since the last success. Reset to 0
    -- by UserStore.verify_login() on a successful login (Task 15).
    failed_attempts INTEGER NOT NULL DEFAULT 0,

    -- Epoch seconds until which login is locked out (5 failed attempts ->
    -- 900s, per docs/OVERHAUL_PLAN.md section 4.2). NULL = not locked.
    locked_until BIGINT,

    -- Epoch seconds of the most recent successful login. NULL = never
    -- logged in.
    last_login_at BIGINT,

    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS chk_users_status,
    ADD CONSTRAINT chk_users_status CHECK (status IN ('active', 'disabled'));

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS chk_users_username_lowercase,
    ADD CONSTRAINT chk_users_username_lowercase CHECK (username = lower(username));

GRANT ALL PRIVILEGES ON users TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE users_id_seq TO alertmanager;

-- ---------------------------------------------------------------------------
-- roles
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS roles (
    id BIGSERIAL PRIMARY KEY,

    -- e.g. 'master_admin' (seeded below), '<client>-node', '<client>-viewer'
    -- (Task 17's users.json migration). VARCHAR(255) -- same identifier
    -- width convention as users.username.
    name VARCHAR(255) NOT NULL UNIQUE,

    description TEXT,

    -- Distinguishes seeded system roles (master_admin) from user-created
    -- ones, e.g. so admin UI/CLI tooling (Task 16) can refuse to delete or
    -- rename system roles.
    is_system BOOLEAN NOT NULL DEFAULT FALSE
);

GRANT ALL PRIVILEGES ON roles TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE roles_id_seq TO alertmanager;

-- ---------------------------------------------------------------------------
-- user_roles (join table: which roles a user holds)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_roles (
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id BIGINT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,

    -- Composite PK both enforces "no duplicate role assignment for a user"
    -- and (with user_id leftmost) gives the get_principal(username) join
    -- path (Task 15) a ready-made index for its user_id lookup.
    PRIMARY KEY (user_id, role_id)
);

-- Reverse lookup (all users holding a given role) -- not on the hot
-- per-request auth path, but used by role deletion/management flows
-- (Task 16). user_id is already covered by the composite PK above; role_id,
-- as the non-leading PK column, is not.
CREATE INDEX IF NOT EXISTS idx_user_roles_role_id ON user_roles (role_id);

GRANT ALL PRIVILEGES ON user_roles TO alertmanager;

-- ---------------------------------------------------------------------------
-- role_scopes (which (client, permission) grants a role carries)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS role_scopes (
    role_id BIGINT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,

    -- Client this scope applies to, or the sentinel '*' meaning all
    -- clients. TEXT, not VARCHAR(n) -- holds either a client identifier or
    -- the single-character sentinel, not a bounded identifier field like
    -- alerts.client.
    client TEXT NOT NULL,

    -- Enum-style CHECK values match docs/OVERHAUL_PLAN.md section 4.1
    -- exactly (enforced below via chk_role_scopes_permission). Extending
    -- this list later is a deliberate migration, not a config change --
    -- fail-fast culture (CLAUDE.md "Do not implement failsafes") --
    -- deliberately not left open-ended the way auth_audit.event is.
    permission VARCHAR(50) NOT NULL,

    -- Composite PK: both the "no duplicate (role,client,permission) grant"
    -- uniqueness constraint the brief calls for, and (with role_id
    -- leftmost) a ready-made index for the get_principal(username) join
    -- path's per-role scope lookup (Task 15) -- same rationale as
    -- user_roles' composite PK above.
    PRIMARY KEY (role_id, client, permission)
);

ALTER TABLE role_scopes
    DROP CONSTRAINT IF EXISTS chk_role_scopes_permission,
    ADD CONSTRAINT chk_role_scopes_permission CHECK (
        permission IN ('*', 'logs:read', 'alerts:write')
    );

GRANT ALL PRIVILEGES ON role_scopes TO alertmanager;

-- ---------------------------------------------------------------------------
-- auth_tokens (hashed Bearer tokens, replaces plaintext tokens.json)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_tokens (
    id BIGSERIAL PRIMARY KEY,

    -- Human-readable label for the token (e.g. "price-checker prod"), shown
    -- by `user-manager.py list`/`show` (Task 16). Not unique -- a user may
    -- hold several tokens labeled the same over time.
    name VARCHAR(255) NOT NULL,

    -- SHA-256 hex digest of the plaintext bearer token (Task 15:
    -- create_token() returns the `amt_`-prefixed plaintext exactly once and
    -- persists only this hash). CHAR(64), not VARCHAR -- a hex-encoded
    -- SHA-256 digest is always exactly 64 characters, so a fixed-length
    -- type documents that invariant directly.
    token_sha256 CHAR(64) NOT NULL UNIQUE,

    -- RESTRICT, not CASCADE: tools/user-manager.py's `delete` is a
    -- distinct, first-class verb from `disable` (docs/OVERHAUL_PLAN.md
    -- section 4.2) -- e.g. compromised-account cleanup, precisely the
    -- scenario where an admin most wants "what credentials did this
    -- account have" preserved, not silently wiped by the same operation
    -- that removes the account. CASCADE would also directly contradict
    -- this table's own status='revoked' soft-delete design just below
    -- (kept as a row specifically so token-mutation history in auth_audit
    -- stays meaningful) -- a CASCADE-on-user-delete would erase that
    -- history anyway the moment the owning user is deleted. RESTRICT
    -- forces an explicit revoke/cleanup of a user's tokens before the user
    -- row itself can be deleted, which fits naturally into the `delete`
    -- CLI flow a later task builds.
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE RESTRICT,

    -- 'revoked' is a soft-delete (user-manager.py revoke-token, Task 16) --
    -- kept as a row rather than deleted so auth_audit's token-mutation
    -- history stays meaningful. No 'expired' value: expiry is derived from
    -- expires_at at verify time, not stored as a separate status
    -- transition.
    status VARCHAR(20) NOT NULL,

    -- Epoch seconds after which this token no longer authenticates. NULL =
    -- never expires.
    expires_at BIGINT,

    -- Epoch seconds this token was last presented successfully. NULL =
    -- never used.
    last_used_at BIGINT,

    -- Judgment call: not in docs/OVERHAUL_PLAN.md section 4.1's column list
    -- verbatim, but added here -- an issued-credential inventory with no
    -- creation timestamp is a real gap for an admin reviewing `list`
    -- output (last_used_at alone can't distinguish a brand-new unused
    -- token from a two-year-old one). See task report for reasoning.
    created_at BIGINT NOT NULL
);

ALTER TABLE auth_tokens
    DROP CONSTRAINT IF EXISTS chk_auth_tokens_status,
    ADD CONSTRAINT chk_auth_tokens_status CHECK (status IN ('active', 'revoked'));

-- Listing/managing one user's tokens (Task 16 `list`/`show`) filters by
-- user_id; not covered by the token_sha256 UNIQUE index or the id PK.
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_id ON auth_tokens (user_id);

GRANT ALL PRIVILEGES ON auth_tokens TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE auth_tokens_id_seq TO alertmanager;

-- ---------------------------------------------------------------------------
-- auth_audit (append-only auth/identity event log)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS auth_audit (
    id BIGSERIAL PRIMARY KEY,

    -- Epoch seconds the audited event occurred.
    at BIGINT NOT NULL,

    -- e.g. login_success, login_failed, lockout, user_created,
    -- user_disabled, user_enabled, password_changed, role_created,
    -- role_granted, role_revoked, scope_added, scope_removed,
    -- token_created, token_revoked. TEXT and deliberately NOT
    -- CHECK-constrained -- unlike role_scopes.permission (a small,
    -- security-sensitive enum), this event vocabulary will keep growing as
    -- later Phase 4 tasks add call sites, and a CHECK here would demand a
    -- migration for every new event name on the way to shipping the
    -- gateway.
    event TEXT NOT NULL,

    -- The audited actor's username, as text -- NOT a user_id FK. A
    -- login_failed event against a nonexistent username (typo, credential
    -- stuffing) has no users row to reference, and this table must still
    -- record it; a text column also keeps the audit trail meaningful after
    -- the audited user is later deleted. Nullable for the rare event with
    -- no associated username at all.
    username TEXT,

    -- Client IP the request originated from (post-ProxyFix, per
    -- docs/OVERHAUL_PLAN.md section 4.4). Text, not inet -- gateway code
    -- reads this from a header/WSGI environ as a string, and this table's
    -- job is to record what happened, not to validate a value that may be
    -- absent or malformed (e.g. behind a misconfigured proxy).
    source_ip TEXT,

    -- Event-specific structured detail (e.g. {"role": "master_admin"} for
    -- role_granted, {"reason": "5 failed attempts"} for lockout). JSONB,
    -- same choice as alerts.metadata/display_config/routing.
    detail JSONB
);

-- Primary access pattern: most recent audit events first -- same
-- ON (<column> DESC) convention as idx_alerts_timestamp /
-- idx_email_log_attempted_at.
CREATE INDEX IF NOT EXISTS idx_auth_audit_at ON auth_audit (at DESC);

-- The other two query shapes docs/OVERHAUL_PLAN.md section 4.1 calls out:
-- filtering the audit trail by event type (e.g. all lockouts) and by actor
-- (e.g. one user's full history via the Task 16 `audit` subcommand).
CREATE INDEX IF NOT EXISTS idx_auth_audit_event ON auth_audit (event);
CREATE INDEX IF NOT EXISTS idx_auth_audit_username ON auth_audit (username);

GRANT ALL PRIVILEGES ON auth_audit TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE auth_audit_id_seq TO alertmanager;

-- ---------------------------------------------------------------------------
-- Seed: master_admin system role with the ('*', '*') all-clients,
-- all-permissions scope. Task 16's `init-admin` and Task 17's
-- migrate-users-json.py (jimi -> master_admin) both assign this role to an
-- account rather than creating it themselves. INSERT ... ON CONFLICT DO
-- NOTHING makes re-running this migration file a safe no-op.
-- ---------------------------------------------------------------------------
INSERT INTO roles (name, description, is_system)
VALUES ('master_admin', 'Full administrative access to all clients and all permissions', TRUE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_scopes (role_id, client, permission)
SELECT r.id, '*', '*'
FROM roles r
WHERE r.name = 'master_admin'
ON CONFLICT (role_id, client, permission) DO NOTHING;
