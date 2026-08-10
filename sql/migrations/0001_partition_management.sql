-- Alert Manager - Migration 0001: Partition Management
--
-- Prod bug: the `alerts` table (sql/init/01-schema.sql) is weekly-partitioned
-- with partitions named `alerts_YYYY_wNN` (ISO year / ISO week), but only 8
-- weeks of partitions were ever created (sql/init/02-partitions.sql) and
-- nothing has automated creating more since. Every insert past the last
-- partition's upper bound fails with "no partition of relation alerts
-- found".
--
-- This migration adds `ensure_alert_partitions(p_from, p_weeks_ahead)`, a
-- reusable, idempotent function that creates any missing weekly partitions
-- from `p_from` through (current ISO week + `p_weeks_ahead` weeks). It uses
-- the exact same `alerts_YYYY_wNN` naming and Monday-anchored week-boundary
-- math as sql/init/02-partitions.sql / sql/init/03-functions.sql, so
-- drop_old_alert_partitions()'s lexicographic name comparison against
-- 'alerts_' || ISOYEAR || '_w' || week continues to work correctly.
--
-- A later task wires this into src/database_logger.py: called once at
-- startup (fail-fast if it errors -- the service cannot run without
-- partitions) and every 24h thereafter. The repair call at the end of this
-- file applies the same fix immediately, so applying this migration repairs
-- prod on its own (idempotent no-op if the Phase 0 manual SQL repair --
-- docs/OVERHAUL_PLAN.md -- already ran against this database).
--
-- NOTE: This function's Monday-anchored ISO week/year date math
-- (DATE_TRUNC('week', ...), EXTRACT(ISOYEAR ...), EXTRACT(WEEK ...)) is
-- independently re-implemented in Python at tests/partition_math.py, used
-- by tests/test_partition_math.py to verify week/year-boundary correctness
-- without a live DB. If this function's date arithmetic ever changes,
-- update that mirror in lockstep or its tests will silently drift from
-- what this function actually does.
CREATE OR REPLACE FUNCTION ensure_alert_partitions(p_from DATE, p_weeks_ahead INT DEFAULT 8)
RETURNS TABLE(partition_name text, status text) AS $$
DECLARE
    v_week DATE;
    v_end DATE;
    v_partition_name TEXT;
BEGIN
    IF p_from IS NULL THEN
        RAISE EXCEPTION 'ensure_alert_partitions: p_from must not be NULL';
    END IF;

    IF p_weeks_ahead IS NULL THEN
        RAISE EXCEPTION 'ensure_alert_partitions: p_weeks_ahead must not be NULL';
    END IF;

    -- Monday of the ISO week containing p_from -- same DATE_TRUNC('week', ...)
    -- boundary used throughout sql/init/02-partitions.sql and
    -- sql/init/03-functions.sql.
    v_week := DATE_TRUNC('week', p_from)::DATE;

    -- Always walk through at least (current ISO week + p_weeks_ahead),
    -- regardless of how far in the past p_from is. This lets one call both
    -- repair an old gap (p_from far in the past) and keep future weeks
    -- pre-created (anchored off CURRENT_DATE, not off p_from) -- matching
    -- the Phase 0 repair block's v_end calculation.
    v_end := DATE_TRUNC('week', CURRENT_DATE)::DATE + (p_weeks_ahead * 7);

    WHILE v_week <= v_end LOOP
        -- ISO year/week naming -- identical expression to
        -- sql/init/02-partitions.sql and sql/init/03-functions.sql, so
        -- names stay consistent across a Dec 31 / Jan 1 ISO year boundary
        -- (a week's ISO year is that of its Thursday, which can differ
        -- from the calendar year of its Monday; EXTRACT(ISOYEAR ...) /
        -- EXTRACT(WEEK ...) already account for this).
        v_partition_name := 'alerts_' || EXTRACT(ISOYEAR FROM v_week) || '_w' || LPAD(EXTRACT(WEEK FROM v_week)::TEXT, 2, '0');

        IF EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = 'public' AND tablename = v_partition_name
        ) THEN
            -- Idempotent: pre-existing partitions are reported, never
            -- recreated or altered.
            RETURN QUERY SELECT v_partition_name, 'already_exists'::TEXT;
        ELSE
            -- Real errors (permission denied, lock conflicts, etc.) are not
            -- caught here -- they propagate to the caller so the migration
            -- runner's transaction rolls back and the failure is loud, not
            -- swallowed.
            EXECUTE format(
                'CREATE TABLE %I PARTITION OF alerts FOR VALUES FROM (%L) TO (%L)',
                v_partition_name, v_week, v_week + 7
            );
            RETURN QUERY SELECT v_partition_name, 'created'::TEXT;
        END IF;

        v_week := v_week + 7;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

GRANT EXECUTE ON FUNCTION ensure_alert_partitions(DATE, INT) TO alertmanager;

-- Repair call: creates any partitions missing from the last-known-good week
-- (2026-01-05, per docs/OVERHAUL_PLAN.md) through 8 weeks ahead of today.
-- No-op for any week whose partition already exists (e.g. if the Phase 0
-- manual SQL repair already ran on this database).
SELECT * FROM ensure_alert_partitions(DATE '2026-01-05', 8);
