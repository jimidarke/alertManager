-- Alert Manager - Migration 0003: Email Log
--
-- Phase 2 (email hardening) of the overhaul plan (docs/OVERHAUL_PLAN.md,
-- section 2.1) adds tracking for every email send attempt made by
-- EmailPathway -- both successes and failures -- so email delivery
-- problems are visible instead of silently swallowed.
--
-- `email_log` is a single unpartitioned table -- send volume here is
-- trivial (one row per email *attempt*, not one row per underlying alert),
-- unlike the weekly-partitioned `alerts` table (sql/init/01-schema.sql).
-- `attempted_at` is BIGINT epoch seconds, not TIMESTAMPTZ, per this
-- project's stated convention for all new time columns going forward
-- ("Epoch BIGINT for all new time columns", docs/OVERHAUL_PLAN.md section
-- 4.1) -- `alerts.timestamp` only stays TIMESTAMPTZ because it drives that
-- table's partition-range boundaries, which doesn't apply here.
--
-- status = 'accepted' means the Microsoft Graph API returned HTTP 202
-- (send request queued) -- it does NOT mean the email was delivered. Graph
-- has no delivery webhook, so "accepted" is the strongest success signal
-- this system can ever record.
--
-- A later task (Task 9: pathways/email_log.py's EmailLogWriter, called
-- from pathways/email_pathway.py) writes the Python code that inserts into
-- this table. This migration only creates the schema.
--
-- NOTE: 0002 is deliberately skipped here -- it is reserved for the Phase 4
-- identity schema migration (docs/OVERHAUL_PLAN.md section 4.1), which
-- lands later on this same branch. Do not renumber or reuse 0002.
CREATE TABLE IF NOT EXISTS email_log (
    id BIGSERIAL PRIMARY KEY,

    -- When this send attempt was made. Epoch seconds (project convention
    -- for new time columns) -- not TIMESTAMPTZ, since this table is not
    -- partitioned and has no need for the range-boundary semantics that
    -- require it on `alerts`.
    attempted_at BIGINT NOT NULL,

    -- Which email pathway instance made the attempt (config pathway name,
    -- e.g. "email_alerts", "email_revysmc_critical" -- see the `name`
    -- BasePathway is constructed with). Same identifier width as
    -- alerts.source.
    pathway VARCHAR(255) NOT NULL,

    -- Whether this row represents a storm-prevention aggregated send
    -- (multiple alerts merged into one email) rather than a single alert.
    aggregated BOOLEAN NOT NULL DEFAULT FALSE,

    -- Number of underlying alerts this send represents: 1 for a normal
    -- single-alert send, >1 for an aggregated storm email.
    alert_count INTEGER NOT NULL DEFAULT 1,

    -- Alert level driving this send (critical/warning/notice/info/debug,
    -- same values as alerts.level). Nullable: a failure path that never
    -- reaches alert-specific rendering (e.g. an MSAL token failure) is not
    -- guaranteed to have one.
    level VARCHAR(20),

    -- Distinct client(s) whose alert(s) this send covers. Same identifier
    -- width as alerts.client; an array because an aggregated send can span
    -- multiple clients.
    clients TEXT[],

    -- Destination addresses this send was attempted to (the pathway's
    -- to_addresses at send time).
    recipients TEXT[],

    -- Rendered email subject line. TEXT, not VARCHAR(n) -- same convention
    -- as alerts.title/alerts.message for unbounded human-readable content
    -- (as opposed to the fixed-width identifier columns above).
    subject TEXT,

    -- Outcome of the send attempt. 'accepted' = Microsoft Graph API
    -- accepted the send request (HTTP 202, queued) -- NOT confirmation of
    -- delivery, since Graph has no delivery webhook. 'failed' = the
    -- request errored or was rejected.
    status VARCHAR(20) NOT NULL,

    -- Raw HTTP status code from the Graph API sendMail call. NULL when the
    -- attempt never reached that call (e.g. MSAL token acquisition failed
    -- first).
    http_status INTEGER,

    -- Error detail for failed attempts. Length-bounded via VARCHAR(n), the
    -- same way every other length-limited text field in this schema is
    -- expressed (see alerts.level/category/source/client in
    -- sql/init/01-schema.sql) -- this codebase has no
    -- CHECK(length(...) <= n) precedent to follow instead.
    error VARCHAR(2000),

    -- Microsoft Graph's request-tracking id (request-id response header),
    -- for cross-referencing with Graph-side logs/support tickets. Not
    -- always present -- e.g. failures before Graph responds.
    graph_request_id VARCHAR(255),

    -- Wall-clock duration of the send attempt, in milliseconds.
    duration_ms INTEGER,

    -- Number of retries performed for this attempt (0 = resolved on the
    -- first try, whether that resolution was success or a non-retryable
    -- failure).
    retries INTEGER NOT NULL DEFAULT 0
);

-- Enum-style CHECK constraint, expressed the same idempotent
-- DROP-then-ADD way as chk_level / chk_category / chk_ingestion_source in
-- sql/init/01-schema.sql, so re-applying this file is a safe no-op.
ALTER TABLE email_log
    DROP CONSTRAINT IF EXISTS chk_email_log_status,
    ADD CONSTRAINT chk_email_log_status CHECK (status IN ('accepted', 'failed'));

-- Primary access pattern: most recent send attempts first -- mirrors
-- idx_alerts_timestamp's ON (timestamp DESC) convention.
CREATE INDEX IF NOT EXISTS idx_email_log_attempted_at ON email_log (attempted_at DESC);

-- This table/sequence is created after sql/init/01-schema.sql's blanket
-- "GRANT ALL ... ON ALL TABLES/SEQUENCES IN SCHEMA public" already ran, so
-- the alertmanager role needs an explicit grant here -- same practice as
-- 0001_partition_management.sql's GRANT EXECUTE ... TO alertmanager for
-- the function it added.
GRANT ALL PRIVILEGES ON email_log TO alertmanager;
GRANT ALL PRIVILEGES ON SEQUENCE email_log_id_seq TO alertmanager;
