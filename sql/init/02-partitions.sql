-- Alert Manager - Initial Partition Creation
-- Version: 1.0.0
-- Creates 8 weeks of partitions (current week + 7 future weeks)

DO $$
DECLARE
    start_date DATE;
    end_date DATE;
    partition_name TEXT;
    week_num INT;
    year_num INT;
BEGIN
    -- Create partitions for current week + next 7 weeks
    FOR i IN 0..7 LOOP
        -- Calculate start of week (Monday)
        start_date := DATE_TRUNC('week', CURRENT_DATE + (i * 7));
        end_date := start_date + INTERVAL '7 days';

        -- Extract ISO year and week number for partition name
        year_num := EXTRACT(ISOYEAR FROM start_date);
        week_num := EXTRACT(WEEK FROM start_date);

        partition_name := 'alerts_' || year_num || '_w' || LPAD(week_num::TEXT, 2, '0');

        -- Create partition if it doesn't exist
        BEGIN
            EXECUTE format('
                CREATE TABLE IF NOT EXISTS %I PARTITION OF alerts
                FOR VALUES FROM (%L) TO (%L)',
                partition_name, start_date, end_date
            );

            RAISE NOTICE 'Created partition: % (% to %)', partition_name, start_date, end_date;
        EXCEPTION WHEN duplicate_table THEN
            RAISE NOTICE 'Partition % already exists', partition_name;
        END;
    END LOOP;

    RAISE NOTICE 'Initial partitions created successfully';
END $$;
