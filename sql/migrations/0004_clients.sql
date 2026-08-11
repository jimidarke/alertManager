-- Alert Manager - Migration 0004: Clients Registry
--
-- Promotes "client" from pure free text (any string accepted in an alert
-- payload's `client` field / a role_scopes.client row, checked only
-- against the caller's own scope) into a real, admin-managed registry --
-- a master_admin-only web UI (gateway/admin/routes.py) can create/list/
-- disable/enable entries here.
--
-- Enforcement stays entirely app-layer, per this feature's own brief:
-- `alerts.client` (sql/init/01-schema.sql, VARCHAR(255) nullable) and
-- `role_scopes.client` (0002_identity.sql, TEXT NOT NULL) are UNTOUCHED by
-- this migration -- no FK, no CHECK referencing this table on either
-- column. This mirrors the existing level/category precedent (a
-- hand-kept-in-sync allow-list checked in code, with a DB CHECK as a
-- mirror only, not the primary mechanism -- see chk_level/chk_category in
-- sql/init/01-schema.sql) with one deliberate difference: clients are
-- dynamic, admin-managed data, not a fixed vocabulary, so the code-side
-- check (UserStore.client_exists(), gateway/api/routes.py's
-- _require_alert_permission) is a live query against this table, not a
-- hardcoded Python constant.
--
-- The `'*'` wildcard client sentinel (role_scopes.client / a scope row's
-- client field) is NOT a row in this table and never will be -- it stays
-- a Python-layer special case (userstore/store.py's has_permission(),
-- gateway/admin/routes.py's scope-row validation), exactly as it is today.

CREATE TABLE IF NOT EXISTS clients (
    id BIGSERIAL PRIMARY KEY,

    -- Canonical client identifier, e.g. 'revysmc'. Same identifier width
    -- convention as users.username / roles.name (VARCHAR(255)). Always
    -- stored trimmed+lowercased -- UserStore._normalize_client() (already
    -- used for role_scopes.client) is reused verbatim for every write to
    -- this column too; chk_clients_name_lowercase below is the same
    -- defense-in-depth mirror chk_users_username_lowercase already is for
    -- `users`.
    name VARCHAR(255) NOT NULL UNIQUE,

    description TEXT,

    -- Soft-disable convention -- mirrors users.status exactly (roles has
    -- no such column at all; users.status/disable_user()/enable_user() is
    -- the closer precedent for THIS specific piece, per the feature brief).
    status VARCHAR(20) NOT NULL DEFAULT 'active',

    -- Epoch BIGINT, per this project's stated convention for all new time
    -- columns (docs/OVERHAUL_PLAN.md section 4.1, already followed by
    -- 0003_email_log.sql's attempted_at) -- not TIMESTAMPTZ, since this
    -- table has no partition-range boundary need the way alerts.timestamp
    -- does.
    created_at BIGINT NOT NULL,
    updated_at BIGINT NOT NULL
);

-- Enum-style CHECK constraint, expressed the same idempotent
-- DROP-then-ADD way as chk_level/chk_category/chk_email_log_status, so
-- re-applying this file is a safe no-op.
ALTER TABLE clients
    DROP CONSTRAINT IF EXISTS chk_clients_status,
    ADD CONSTRAINT chk_clients_status CHECK (status IN ('active', 'disabled'));

ALTER TABLE clients
    DROP CONSTRAINT IF EXISTS chk_clients_name_lowercase,
    ADD CONSTRAINT chk_clients_name_lowercase CHECK (name = lower(name));

-- Deliberately no index on `status` alone: UserStore.client_exists()/
-- get_client() look up by the already-UNIQUE `name` column (a single-row,
-- O(1) lookup regardless of table size); UserStore.list_clients() scans
-- the whole table, which is fine at this table's expected scale (a
-- hand-curated admin registry, not ingested data -- same proportionate-
-- indexing precedent `roles` already sets by carrying no index of its
-- own either). Revisit only if this table's row count ever approaches
-- `alerts`-scale, which it structurally cannot.

-- This table/sequence is created after sql/init/01-schema.sql's blanket
-- "GRANT ALL ... ON ALL TABLES/SEQUENCES IN SCHEMA public" already ran, so
-- the alertmanager role needs an explicit grant here -- same practice as
-- every other post-init migration in this directory.
GRANT ALL PRIVILEGES ON clients TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE clients_id_seq TO alertmanager;
