-- Alert Manager Database Schema
-- Version: 1.0.0
-- PostgreSQL 16+

-- Create main alerts table (partitioned by timestamp)
CREATE TABLE IF NOT EXISTS alerts (
    id BIGSERIAL,
    timestamp TIMESTAMPTZ NOT NULL,
    level VARCHAR(20) NOT NULL,
    category VARCHAR(50) NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    source VARCHAR(255) NOT NULL,
    client VARCHAR(255),

    -- JSONB columns for nested structures
    metadata JSONB,
    display_config JSONB,
    routing JSONB,

    -- Audit fields
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ingestion_source VARCHAR(50) NOT NULL,

    -- Partition key
    PRIMARY KEY (id, timestamp)
) PARTITION BY RANGE (timestamp);

-- Create indexes on main table (inherited by partitions)
CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts (level);
CREATE INDEX IF NOT EXISTS idx_alerts_category ON alerts (category);
CREATE INDEX IF NOT EXISTS idx_alerts_client ON alerts (client);
CREATE INDEX IF NOT EXISTS idx_alerts_source ON alerts (source);
CREATE INDEX IF NOT EXISTS idx_alerts_level_timestamp ON alerts (level, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_client_timestamp ON alerts (client, timestamp DESC);

-- JSONB GIN indexes for efficient JSON queries
CREATE INDEX IF NOT EXISTS idx_alerts_metadata_gin ON alerts USING GIN (metadata);
CREATE INDEX IF NOT EXISTS idx_alerts_display_config_gin ON alerts USING GIN (display_config);

-- Composite index for analytics queries
CREATE INDEX IF NOT EXISTS idx_alerts_analytics ON alerts (client, category, level, timestamp DESC);

-- Text search index for title and message (full-text search)
CREATE INDEX IF NOT EXISTS idx_alerts_fulltext ON alerts USING GIN (to_tsvector('english', title || ' ' || message));

-- Add check constraints for data integrity
ALTER TABLE alerts
    DROP CONSTRAINT IF EXISTS chk_level,
    ADD CONSTRAINT chk_level CHECK (level IN ('critical', 'warning', 'notice', 'info', 'debug'));

ALTER TABLE alerts
    DROP CONSTRAINT IF EXISTS chk_category,
    ADD CONSTRAINT chk_category CHECK (category IN ('security', 'system', 'weather', 'automation', 'personal', 'network', 'application', 'news', 'business', 'hardware', 'environmental'));

ALTER TABLE alerts
    DROP CONSTRAINT IF EXISTS chk_ingestion_source,
    ADD CONSTRAINT chk_ingestion_source CHECK (ingestion_source IN ('http', 'mqtt', 'snmp', 'feed', 'manual'));

-- Ensure timestamp is reasonable (not too far in past/future)
ALTER TABLE alerts
    DROP CONSTRAINT IF EXISTS chk_timestamp_range,
    ADD CONSTRAINT chk_timestamp_range CHECK (
        timestamp >= '2020-01-01'::timestamptz
        AND timestamp <= NOW() + INTERVAL '1 day'
    );

-- Create materialized view for hourly alert summary (optional, for performance)
CREATE MATERIALIZED VIEW IF NOT EXISTS alert_summary_hourly AS
SELECT
    DATE_TRUNC('hour', timestamp) AS hour,
    level,
    category,
    client,
    COUNT(*) AS alert_count,
    COUNT(DISTINCT source) AS unique_sources
FROM alerts
GROUP BY 1, 2, 3, 4
WITH DATA;

-- Index on materialized view
CREATE INDEX IF NOT EXISTS idx_alert_summary_hourly ON alert_summary_hourly (hour DESC, client);

-- Grant permissions
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO alertmanager;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO alertmanager;

-- Success message
DO $$
BEGIN
    RAISE NOTICE 'Alert Manager schema created successfully';
END $$;
