"""
Database Analyzer

SQL-based analyzer for querying alerts from PostgreSQL database.
Provides efficient time-series queries and aggregations.
"""

import os
import re
import logging
from typing import Dict, Any, List, Optional
from datetime import datetime, timedelta

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor, Json

from .base_analyzer import BaseAnalyzer


class DatabaseAnalyzer(BaseAnalyzer):
    """Database-based analyzer with SQL queries"""

    def __init__(self, name: str, config: Dict[str, Any], logger: logging.Logger, db_pool=None):
        """
        Initialize database analyzer

        Args:
            name: Analyzer name
            config: Configuration dictionary
            logger: Logger instance
            db_pool: Optional shared database connection pool
        """
        super().__init__(name, config, logger)

        # Use provided pool or create new one
        self.db_pool = db_pool if db_pool else self._create_db_pool()
        self.owns_pool = db_pool is None

    def _create_db_pool(self) -> psycopg2.pool.ThreadedConnectionPool:
        """Create PostgreSQL connection pool"""
        try:
            db_config = {
                'host': os.getenv('POSTGRES_HOST', 'localhost'),
                'port': int(os.getenv('POSTGRES_PORT', 5432)),
                'database': os.getenv('POSTGRES_DB', 'alertmanager'),
                'user': os.getenv('POSTGRES_USER', 'alertmanager'),
                'password': os.getenv('POSTGRES_PASSWORD', ''),
            }

            self.logger.info(f"Creating database connection pool for analyzer: {db_config['host']}:{db_config['port']}")

            db_pool = pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=5,
                **db_config
            )

            return db_pool

        except Exception as e:
            self.logger.error(f"Failed to create database connection pool: {e}", exc_info=True)
            raise

    def query_alerts(
        self,
        days_back: int = None,
        filters: Dict[str, Any] = None,
        limit: int = None,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """
        Query alerts from database with efficient SQL

        Args:
            days_back: Number of days to look back
            filters: Dictionary of filters (level, category, client, source, etc.)
            limit: Maximum number of results
            offset: Pagination offset

        Returns:
            List of alert dictionaries
        """
        if days_back is None:
            days_back = self.config.get('analysis_period_days', 7)

        # Build WHERE clause from query definition and filters
        where_clauses = []
        params = []

        # Time range
        start_time = datetime.utcnow() - timedelta(days=days_back)
        where_clauses.append("timestamp >= %s")
        params.append(start_time)

        # Client filter
        client = self.config.get('client')
        if client:
            where_clauses.append("client = %s")
            params.append(client)

        # Apply query definition filters
        if self.query_def:
            pattern_matching = self.query_def.get('log_pattern_matching', {})

            # Level filter
            level_filter = pattern_matching.get('level_filter')
            if level_filter:
                placeholders = ','.join(['%s'] * len(level_filter))
                where_clauses.append(f"level IN ({placeholders})")
                params.extend(level_filter)

            # Category filter
            category_filter = pattern_matching.get('category_filter')
            if category_filter:
                placeholders = ','.join(['%s'] * len(category_filter))
                where_clauses.append(f"category IN ({placeholders})")
                params.extend(category_filter)

            # Title pattern (regex match)
            title_pattern = pattern_matching.get('title_pattern')
            if title_pattern:
                flags = pattern_matching.get('title_pattern_flags', [])
                if 'IGNORECASE' in flags:
                    where_clauses.append("title ~* %s")  # Case-insensitive regex
                else:
                    where_clauses.append("title ~ %s")
                params.append(title_pattern)

        # Additional filters
        if filters:
            for key, value in filters.items():
                if isinstance(value, list):
                    placeholders = ','.join(['%s'] * len(value))
                    where_clauses.append(f"{key} IN ({placeholders})")
                    params.extend(value)
                else:
                    where_clauses.append(f"{key} = %s")
                    params.append(value)

        # Build query
        where_sql = " AND ".join(where_clauses) if where_clauses else "TRUE"

        query = f"""
            SELECT
                id, timestamp, level, category, title, message, source, client,
                metadata, display_config, routing, created_at, ingestion_source
            FROM alerts
            WHERE {where_sql}
            ORDER BY timestamp DESC
        """

        if limit:
            query += f" LIMIT {limit} OFFSET {offset}"

        # Execute query
        conn = None
        try:
            conn = self.db_pool.getconn()

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(query, params)
                results = cur.fetchall()

            # Convert to dictionaries and handle timestamps
            alerts = []
            for row in results:
                alert = dict(row)
                # Convert timestamp to Unix epoch for consistency
                if isinstance(alert['timestamp'], datetime):
                    alert['timestamp'] = int(alert['timestamp'].timestamp())
                if isinstance(alert['created_at'], datetime):
                    alert['created_at'] = int(alert['created_at'].timestamp())
                alerts.append(alert)

            self.logger.debug(f"Query returned {len(alerts)} alerts")
            return alerts

        except Exception as e:
            self.logger.error(f"Failed to query alerts: {e}", exc_info=True)
            return []

        finally:
            if conn:
                self.db_pool.putconn(conn)

    def aggregate_by_field(
        self,
        group_by_field: str,
        days_back: int = None,
        filters: Dict[str, Any] = None,
        sort_by: str = None,
        sort_order: str = 'desc'
    ) -> List[Dict[str, Any]]:
        """
        Aggregate alerts by a field extracted from message using query definition

        This method queries alerts, extracts fields using regex patterns,
        and performs grouping/aggregation in Python.

        Args:
            group_by_field: Field name to group by (from query definition)
            days_back: Number of days to analyze
            filters: Additional filters
            sort_by: Field to sort by (defaults to count)
            sort_order: 'asc' or 'desc'

        Returns:
            List of aggregated results
        """
        # Query alerts
        alerts = self.query_alerts(days_back=days_back, filters=filters)

        if not alerts:
            return []

        # Extract fields from messages using query definition
        extracted_records = []
        for alert in alerts:
            extracted = self.extract_fields_from_alert(alert)
            if extracted:
                extracted_records.append(extracted)

        if not extracted_records:
            self.logger.warning("No records matched extraction patterns")
            return []

        # Get aggregation config from query definition
        aggregation_config = self.query_def.get('aggregation', {}) if self.query_def else {}
        group_by_fields = aggregation_config.get('group_by', [group_by_field])

        # Perform grouping
        grouped = {}
        for record in extracted_records:
            # Create key from group_by fields
            key = tuple(record.get(f, '') for f in group_by_fields)

            if key not in grouped:
                grouped[key] = {
                    **{f: record.get(f, '') for f in group_by_fields},
                    'count': 0,
                    'first_seen': record['timestamp'],
                    'last_seen': record['timestamp'],
                    'sources': set(),
                    'devices': set() if 'device' in record else None
                }

            grouped[key]['count'] += 1
            grouped[key]['first_seen'] = min(grouped[key]['first_seen'], record['timestamp'])
            grouped[key]['last_seen'] = max(grouped[key]['last_seen'], record['timestamp'])
            grouped[key]['sources'].add(record.get('source', 'unknown'))

            if 'device' in record:
                grouped[key]['devices'].add(record['device'])

        # Convert to list and clean up
        results = []
        for item in grouped.values():
            # Convert sets to lists for JSON serialization
            item['sources'] = list(item['sources'])
            item['source_count'] = len(item['sources'])

            if item['devices'] is not None:
                item['devices'] = list(item['devices'])
                item['device_count'] = len(item['devices'])

            results.append(item)

        # Sort results
        if not sort_by:
            sort_by = aggregation_config.get('sort_by', 'count')

        results.sort(
            key=lambda x: x.get(sort_by, 0),
            reverse=(sort_order == 'desc')
        )

        self.logger.info(f"Aggregated {len(extracted_records)} records into {len(results)} groups")
        return results

    def extract_fields_from_alert(self, alert: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Extract data fields from alert using query definition

        Args:
            alert: Alert dictionary from database

        Returns:
            Dictionary of extracted fields or None if required fields missing
        """
        if not self.query_def:
            return alert

        data_extraction = self.query_def.get('data_extraction', {})
        fields_config = data_extraction.get('fields', [])

        extracted = {
            'timestamp': alert['timestamp'],
            'source': alert['source'],
            'client': alert.get('client', ''),
            'level': alert['level'],
            'category': alert['category']
        }

        message = alert.get('message', '')

        for field_config in fields_config:
            field_name = field_config['name']
            pattern = field_config['pattern']
            required = field_config.get('required', False)
            default = field_config.get('default')

            match = re.search(pattern, message)

            if match:
                value = match.group(1) if match.groups() else match.group(0)
                extracted[field_name] = value
            elif required:
                return None
            elif default is not None:
                extracted[field_name] = default

        return extracted

    def execute_custom_query(self, sql: str, params: tuple = None) -> List[Dict[str, Any]]:
        """
        Execute custom SQL query

        Args:
            sql: SQL query string
            params: Query parameters tuple

        Returns:
            List of result dictionaries
        """
        conn = None
        try:
            conn = self.db_pool.getconn()

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(sql, params or ())
                results = cur.fetchall()

            return [dict(row) for row in results]

        except Exception as e:
            self.logger.error(f"Failed to execute custom query: {e}", exc_info=True)
            return []

        finally:
            if conn:
                self.db_pool.putconn(conn)

    def get_alert_statistics(self, days_back: int = 7) -> Dict[str, Any]:
        """
        Get alert statistics for time period

        Args:
            days_back: Number of days to analyze

        Returns:
            Dictionary of statistics
        """
        start_time = datetime.utcnow() - timedelta(days=days_back)

        sql = """
            SELECT
                COUNT(*) as total_alerts,
                COUNT(DISTINCT client) as unique_clients,
                COUNT(DISTINCT source) as unique_sources,
                MIN(timestamp) as earliest_alert,
                MAX(timestamp) as latest_alert,
                COUNT(*) FILTER (WHERE level = 'critical') as critical_count,
                COUNT(*) FILTER (WHERE level = 'warning') as warning_count,
                COUNT(*) FILTER (WHERE level = 'notice') as notice_count,
                COUNT(*) FILTER (WHERE level = 'info') as info_count,
                COUNT(*) FILTER (WHERE level = 'debug') as debug_count
            FROM alerts
            WHERE timestamp >= %s
        """

        results = self.execute_custom_query(sql, (start_time,))

        if results:
            stats = results[0]
            # Convert timestamps
            if stats.get('earliest_alert'):
                stats['earliest_alert'] = int(stats['earliest_alert'].timestamp())
            if stats.get('latest_alert'):
                stats['latest_alert'] = int(stats['latest_alert'].timestamp())
            return stats

        return {}

    def __del__(self):
        """Cleanup database connection pool if owned"""
        if self.owns_pool and self.db_pool:
            try:
                self.db_pool.closeall()
            except:
                pass
