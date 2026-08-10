-- Alert Manager - Partition Management Functions
-- Version: 1.0.0

-- Function to create future partitions (run weekly via cron)
CREATE OR REPLACE FUNCTION create_alert_partitions()
RETURNS TABLE(partition_name TEXT, start_date DATE, end_date DATE, status TEXT) AS $$
DECLARE
    v_start_date DATE;
    v_end_date DATE;
    v_partition_name TEXT;
    v_week_num INT;
    v_year_num INT;
BEGIN
    -- Create next 4 weeks of partitions
    FOR i IN 0..3 LOOP
        -- Calculate start of week (Monday)
        v_start_date := DATE_TRUNC('week', CURRENT_DATE + (i * 7));
        v_end_date := v_start_date + INTERVAL '7 days';

        -- Extract ISO year and week number for partition name
        v_year_num := EXTRACT(ISOYEAR FROM v_start_date);
        v_week_num := EXTRACT(WEEK FROM v_start_date);

        v_partition_name := 'alerts_' || v_year_num || '_w' || LPAD(v_week_num::TEXT, 2, '0');

        -- Check if partition already exists
        IF EXISTS (
            SELECT 1 FROM pg_tables
            WHERE schemaname = 'public'
            AND tablename = v_partition_name
        ) THEN
            -- Partition exists, return status
            RETURN QUERY SELECT v_partition_name, v_start_date, v_end_date, 'already_exists'::TEXT;
        ELSE
            -- Create partition
            BEGIN
                EXECUTE format('
                    CREATE TABLE %I PARTITION OF alerts
                    FOR VALUES FROM (%L) TO (%L)',
                    v_partition_name, v_start_date, v_end_date
                );

                RETURN QUERY SELECT v_partition_name, v_start_date, v_end_date, 'created'::TEXT;
            EXCEPTION WHEN OTHERS THEN
                RETURN QUERY SELECT v_partition_name, v_start_date, v_end_date, 'error: ' || SQLERRM;
            END;
        END IF;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Function to drop old partitions (keep 52 weeks = 1 year)
CREATE OR REPLACE FUNCTION drop_old_alert_partitions()
RETURNS TABLE(partition_name TEXT, status TEXT) AS $$
DECLARE
    v_partition_name TEXT;
    v_cutoff_date DATE;
BEGIN
    -- Calculate cutoff date (52 weeks ago)
    v_cutoff_date := CURRENT_DATE - INTERVAL '52 weeks';

    -- Find all partitions older than cutoff date
    FOR v_partition_name IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = 'public'
        AND tablename LIKE 'alerts_2%'
        AND tablename < 'alerts_' || EXTRACT(ISOYEAR FROM v_cutoff_date) || '_w' || LPAD(EXTRACT(WEEK FROM v_cutoff_date)::TEXT, 2, '0')
        ORDER BY tablename
    LOOP
        BEGIN
            EXECUTE format('DROP TABLE IF EXISTS %I', v_partition_name);
            RETURN QUERY SELECT v_partition_name, 'dropped'::TEXT;
        EXCEPTION WHEN OTHERS THEN
            RETURN QUERY SELECT v_partition_name, 'error: ' || SQLERRM;
        END;
    END LOOP;

    -- If no partitions dropped
    IF NOT FOUND THEN
        RETURN QUERY SELECT 'none'::TEXT, 'no old partitions to drop'::TEXT;
    END IF;
END;
$$ LANGUAGE plpgsql;

-- Function to get partition statistics
CREATE OR REPLACE FUNCTION get_partition_stats()
RETURNS TABLE(
    partition_name TEXT,
    start_range TEXT,
    end_range TEXT,
    row_count BIGINT,
    total_size TEXT,
    table_size TEXT,
    indexes_size TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.relname::TEXT AS partition_name,
        pg_get_expr(c.relpartbound, c.oid, true) AS start_range,
        NULL::TEXT AS end_range,
        c.reltuples::BIGINT AS row_count,
        pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
        pg_size_pretty(pg_relation_size(c.oid)) AS table_size,
        pg_size_pretty(pg_total_relation_size(c.oid) - pg_relation_size(c.oid)) AS indexes_size
    FROM pg_class c
    JOIN pg_inherits i ON i.inhrelid = c.oid
    JOIN pg_class p ON p.oid = i.inhparent
    WHERE p.relname = 'alerts'
    ORDER BY c.relname;
END;
$$ LANGUAGE plpgsql;

-- Function to refresh materialized view (run hourly via cron)
CREATE OR REPLACE FUNCTION refresh_alert_summary()
RETURNS TEXT AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY alert_summary_hourly;
    RETURN 'Materialized view refreshed at ' || NOW();
EXCEPTION WHEN OTHERS THEN
    RETURN 'Error refreshing view: ' || SQLERRM;
END;
$$ LANGUAGE plpgsql;

-- Function to get database statistics
CREATE OR REPLACE FUNCTION get_database_stats()
RETURNS TABLE(
    metric TEXT,
    value TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 'Total Alerts'::TEXT, COUNT(*)::TEXT FROM alerts
    UNION ALL
    SELECT 'Database Size'::TEXT, pg_size_pretty(pg_database_size(current_database()))
    UNION ALL
    SELECT 'Alerts Table Size'::TEXT, pg_size_pretty(pg_total_relation_size('alerts'))
    UNION ALL
    SELECT 'Active Connections'::TEXT, COUNT(*)::TEXT FROM pg_stat_activity WHERE datname = current_database()
    UNION ALL
    SELECT 'Oldest Alert'::TEXT, MIN(timestamp)::TEXT FROM alerts
    UNION ALL
    SELECT 'Newest Alert'::TEXT, MAX(timestamp)::TEXT FROM alerts
    UNION ALL
    SELECT 'Alerts Last Hour'::TEXT, COUNT(*)::TEXT FROM alerts WHERE timestamp > NOW() - INTERVAL '1 hour'
    UNION ALL
    SELECT 'Alerts Last Day'::TEXT, COUNT(*)::TEXT FROM alerts WHERE timestamp > NOW() - INTERVAL '1 day'
    UNION ALL
    SELECT 'Partition Count'::TEXT, COUNT(*)::TEXT FROM pg_tables WHERE tablename LIKE 'alerts_%';
END;
$$ LANGUAGE plpgsql;

-- Grant execute permissions
GRANT EXECUTE ON FUNCTION create_alert_partitions() TO alertmanager;
GRANT EXECUTE ON FUNCTION drop_old_alert_partitions() TO alertmanager;
GRANT EXECUTE ON FUNCTION get_partition_stats() TO alertmanager;
GRANT EXECUTE ON FUNCTION refresh_alert_summary() TO alertmanager;
GRANT EXECUTE ON FUNCTION get_database_stats() TO alertmanager;

-- Success message
DO $$
BEGIN
    RAISE NOTICE 'Partition management functions created successfully';
    RAISE NOTICE 'Usage:';
    RAISE NOTICE '  SELECT * FROM create_alert_partitions();';
    RAISE NOTICE '  SELECT * FROM drop_old_alert_partitions();';
    RAISE NOTICE '  SELECT * FROM get_partition_stats();';
    RAISE NOTICE '  SELECT * FROM get_database_stats();';
END $$;
