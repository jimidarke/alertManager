#!/usr/bin/env python3
"""
Alert Manager Database Logger

Subscribes to MQTT alerts/input topic and writes alerts to PostgreSQL database.
Supports connection pooling, batch insertion, and graceful shutdown.
"""

import os
import sys
import json
import time
import signal
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from threading import Thread, Lock, Event
from queue import Queue, Empty

import paho.mqtt.client as mqtt
import psycopg2
from psycopg2 import pool, extras
from psycopg2.extras import Json

from utils import heartbeat


class DatabaseLogger:
    """Database logger for Alert Manager"""

    # How often (seconds) the 60s stats loop also runs partition maintenance
    # (ensure_alert_partitions + drop_old_alert_partitions + refresh_alert_summary).
    MAINTENANCE_INTERVAL_SECONDS = 24 * 60 * 60

    def __init__(self, config_path: str):
        """Initialize database logger"""
        self.config = self._load_config(config_path)
        self.running = Event()
        self.running.set()

        # Setup logging
        self.logger = self._setup_logging()

        # Heartbeat file, polled by the container healthcheck to detect a
        # hung/dead process (see src/utils/heartbeat.py).
        self.heartbeat_path = os.getenv('HEARTBEAT_FILE', '/data/logs/ops/alert-database-logger.heartbeat')

        # Alert queue for batch processing
        self.alert_queue = Queue(maxsize=10000)
        self.queue_lock = Lock()

        # Statistics
        self.stats = {
            'alerts_received': 0,
            'alerts_inserted': 0,
            'alerts_failed': 0,
            'batches_processed': 0,
            'start_time': time.time()
        }
        self.stats_lock = Lock()

        # Database connection pool
        self.db_pool = self._create_db_pool()

        # Partition startup gate: this service is the sole writer to the
        # partitioned `alerts` table and cannot function without partitions
        # for the current/near-future weeks existing. Fail fast rather than
        # start accepting alerts we can't insert.
        self._ensure_partitions_or_exit()

        # 24h maintenance timer for the periodic partition/summary upkeep
        # run from inside the existing 60s stats loop (see _run_periodic_cycle).
        # Set to "now" (not 0) so maintenance runs 24h after startup, since
        # the startup gate above just ran ensure_alert_partitions itself.
        self._last_maintenance = time.time()

        # MQTT client
        self.mqtt_client = self._setup_mqtt()

        # Batch processing thread
        self.batch_thread = None

    def _load_config(self, config_path: str) -> Dict[str, Any]:
        """Load configuration from JSON file"""
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
            return config
        except Exception as e:
            print(f"ERROR: Failed to load config from {config_path}: {e}", file=sys.stderr)
            sys.exit(1)

    def _setup_logging(self) -> logging.Logger:
        """Setup logging configuration"""
        log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
        log_file = os.getenv('LOG_FILE', '/data/logs/ops/alert-database-logger.{date}.log')

        # Replace {date} with current UTC date
        log_file = log_file.replace('{date}', datetime.utcnow().strftime('%Y-%m-%d'))

        # Create log directory if needed
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)

        # Configure logging
        logging.basicConfig(
            level=getattr(logging, log_level),
            format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout)
            ]
        )

        logger = logging.getLogger('database_logger')
        logger.info(f"Database Logger starting (log level: {log_level})")
        return logger

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

            pool_size = int(os.getenv('POSTGRES_POOL_SIZE', 10))
            max_overflow = int(os.getenv('POSTGRES_MAX_OVERFLOW', 20))

            self.logger.info(f"Creating database connection pool: {db_config['host']}:{db_config['port']}/{db_config['database']}")

            db_pool = pool.ThreadedConnectionPool(
                minconn=pool_size,
                maxconn=pool_size + max_overflow,
                **db_config
            )

            self.logger.info(f"Database connection pool created (size: {pool_size}, max: {pool_size + max_overflow})")
            return db_pool

        except Exception as e:
            self.logger.critical(f"Failed to create database connection pool: {e}", exc_info=True)
            sys.exit(1)

    def _setup_mqtt(self) -> mqtt.Client:
        """Setup MQTT client"""
        mqtt_config = self.config.get('mqtt', {})

        broker = mqtt_config.get('broker', os.getenv('MQTT_BROKER', 'mqtt-broker'))
        port = mqtt_config.get('port', int(os.getenv('MQTT_PORT', 1883)))
        topic = mqtt_config.get('input_topic', 'alerts/input')

        self.logger.info(f"Setting up MQTT client: {broker}:{port}")

        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="database-logger",
            clean_session=False
        )

        client.on_connect = self._on_mqtt_connect
        client.on_message = self._on_mqtt_message
        client.on_disconnect = self._on_mqtt_disconnect

        # Store topic for subscription
        client.user_data_set({'topic': topic})

        try:
            client.connect(broker, port, keepalive=60)
            self.logger.info(f"Connected to MQTT broker: {broker}:{port}")
        except Exception as e:
            self.logger.critical(f"Failed to connect to MQTT broker: {e}", exc_info=True)
            sys.exit(1)

        return client

    def _on_mqtt_connect(self, client, userdata, flags, reason_code, properties):
        """MQTT connect callback"""
        if reason_code == 0:
            topic = userdata.get('topic', 'alerts/input')
            client.subscribe(topic, qos=1)
            self.logger.info(f"Connected to MQTT broker and subscribed to {topic}")
        else:
            self.logger.error(f"MQTT connection failed with reason code: {reason_code}")

    def _on_mqtt_message(self, client, userdata, msg):
        """MQTT message callback"""
        try:
            # Parse JSON payload
            alert = json.loads(msg.payload.decode('utf-8'))

            # Add to queue
            with self.stats_lock:
                self.stats['alerts_received'] += 1

            self.alert_queue.put(alert, block=False)

        except json.JSONDecodeError as e:
            self.logger.warning(f"Invalid JSON received on {msg.topic}: {e}")
        except Exception as e:
            self.logger.error(f"Error processing MQTT message: {e}", exc_info=True)

    def _on_mqtt_disconnect(self, client, userdata, reason_code, properties):
        """MQTT disconnect callback"""
        if reason_code != 0:
            self.logger.warning(f"MQTT disconnected with reason code: {reason_code}")

    # Shared INSERT shape for both the batch (execute_values) path and the
    # row-by-row fallback path.
    _INSERT_COLUMNS = """
        INSERT INTO alerts (
            timestamp, level, category, title, message, source, client,
            metadata, display_config, routing, ingestion_source
        )
    """

    @staticmethod
    def _alert_to_values(alert: Dict[str, Any]) -> tuple:
        """Map a single alert dict to the positional insert tuple"""
        return (
            alert.get('timestamp'),
            alert.get('level', 'info'),
            alert.get('category', 'system'),
            alert.get('title', ''),
            alert.get('message', ''),
            alert.get('source', 'unknown'),
            alert.get('client'),
            Json(alert.get('metadata')) if alert.get('metadata') else None,
            Json(alert.get('display_config')) if alert.get('display_config') else None,
            Json(alert.get('routing')) if alert.get('routing') else None,
            'mqtt'  # ingestion_source
        )

    def _execute_batch_insert(self, conn, alerts: List[Dict[str, Any]]) -> None:
        """
        Single attempt at a bulk insert of the whole batch on the given
        connection. Raises on failure; caller is responsible for rollback.
        """
        values = [self._alert_to_values(alert) for alert in alerts]

        insert_query = self._INSERT_COLUMNS + " VALUES %s"

        with conn.cursor() as cur:
            extras.execute_values(
                cur,
                insert_query,
                values,
                template="(to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                page_size=100
            )

        conn.commit()

    def _insert_alerts_row_by_row(self, conn, alerts: List[Dict[str, Any]]) -> int:
        """
        Fallback path when a batch fails twice: insert one alert at a time
        so a single bad row can't sink the whole batch. Rows that fail
        individually are logged loudly (ERROR, full payload) and counted as
        failed rather than silently dropped.

        Returns:
            Number of alerts successfully inserted
        """
        insert_query = self._INSERT_COLUMNS + """
            VALUES (to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        inserted = 0
        for alert in alerts:
            try:
                with conn.cursor() as cur:
                    cur.execute(insert_query, self._alert_to_values(alert))
                conn.commit()
                inserted += 1
            except Exception as e:
                conn.rollback()
                self.logger.error(
                    f"Row-by-row insert failed for alert, full payload={json.dumps(alert, default=str)}: {e}",
                    exc_info=True
                )

        self.logger.warning(
            f"Row-by-row fallback complete: {inserted}/{len(alerts)} inserted, "
            f"{len(alerts) - inserted} failed"
        )
        return inserted

    def _insert_alert_batch(self, alerts: List[Dict[str, Any]]) -> int:
        """
        Insert batch of alerts into database.

        On failure: rollback, run ensure_alert_partitions (the most common
        cause of a batch failure is a missing partition), retry the batch
        once, and if that also fails, fall back to row-by-row insertion.

        Returns:
            Number of alerts successfully inserted
        """
        if not alerts:
            return 0

        conn = None
        try:
            conn = self.db_pool.getconn()

            try:
                self._execute_batch_insert(conn, alerts)
                return len(alerts)
            except Exception as e:
                self.logger.error(f"Failed to insert alert batch ({len(alerts)} alerts): {e}", exc_info=True)
                conn.rollback()

            # Repair any missing partitions before retrying. If this itself
            # fails, log loudly but still attempt the retry below -- the
            # original batch failure may be unrelated to partitions.
            try:
                self._ensure_partitions(conn)
            except Exception as partition_err:
                self.logger.error(
                    f"ensure_alert_partitions failed while recovering from batch failure: {partition_err}",
                    exc_info=True
                )

            try:
                self._execute_batch_insert(conn, alerts)
                self.logger.info(f"Batch retry succeeded ({len(alerts)} alerts) after partition check")
                return len(alerts)
            except Exception as retry_err:
                self.logger.error(
                    f"Batch retry failed ({len(alerts)} alerts), falling back to row-by-row insert: {retry_err}",
                    exc_info=True
                )
                conn.rollback()
                return self._insert_alerts_row_by_row(conn, alerts)

        except Exception as e:
            self.logger.error(f"Unexpected error inserting alert batch: {e}", exc_info=True)
            return 0

        finally:
            if conn:
                self.db_pool.putconn(conn)

    def _batch_processor(self):
        """Background thread for batch processing alerts"""
        batch_size = int(os.getenv('DB_BATCH_SIZE', 100))
        batch_timeout = float(os.getenv('DB_BATCH_TIMEOUT', 5.0))

        self.logger.info(f"Batch processor started (size: {batch_size}, timeout: {batch_timeout}s)")

        batch = []
        last_flush = time.time()

        while self.running.is_set():
            try:
                # Get alert from queue with timeout
                try:
                    alert = self.alert_queue.get(timeout=1.0)
                    batch.append(alert)
                except Empty:
                    pass

                # Check if batch should be flushed
                current_time = time.time()
                should_flush = (
                    len(batch) >= batch_size or
                    (len(batch) > 0 and (current_time - last_flush) >= batch_timeout)
                )

                if should_flush:
                    # Insert batch
                    inserted = self._insert_alert_batch(batch)

                    # Update statistics
                    with self.stats_lock:
                        self.stats['alerts_inserted'] += inserted
                        self.stats['alerts_failed'] += len(batch) - inserted
                        self.stats['batches_processed'] += 1

                    if inserted > 0:
                        self.logger.debug(f"Inserted batch of {inserted} alerts")

                    # Clear batch and reset timer
                    batch.clear()
                    last_flush = current_time

            except Exception as e:
                self.logger.error(f"Error in batch processor: {e}", exc_info=True)
                time.sleep(1)

        # Flush remaining alerts on shutdown
        if batch:
            self.logger.info(f"Flushing {len(batch)} remaining alerts before shutdown")
            inserted = self._insert_alert_batch(batch)
            with self.stats_lock:
                self.stats['alerts_inserted'] += inserted

    def _print_statistics(self):
        """Print statistics to log"""
        with self.stats_lock:
            uptime = time.time() - self.stats['start_time']
            rate = self.stats['alerts_inserted'] / uptime if uptime > 0 else 0

            self.logger.info(
                f"Statistics: Received={self.stats['alerts_received']}, "
                f"Inserted={self.stats['alerts_inserted']}, "
                f"Failed={self.stats['alerts_failed']}, "
                f"Batches={self.stats['batches_processed']}, "
                f"Rate={rate:.2f} alerts/sec, "
                f"Uptime={uptime:.0f}s"
            )

    def _call_maintenance_function(self, conn, sql: str, params: Optional[tuple] = None) -> List[tuple]:
        """
        Run a single maintenance SQL statement (a `SELECT * FROM some_function(...)`
        call) as its own committed transaction and return its result rows.

        Each call is committed on its own -- rather than batching all three
        maintenance calls into one open transaction -- because
        refresh_alert_summary()'s `REFRESH MATERIALIZED VIEW CONCURRENTLY`
        cannot run inside an already-open transaction block.
        """
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
            conn.commit()
            return rows
        except Exception:
            conn.rollback()
            raise

    def _ensure_partitions(self, conn) -> List[tuple]:
        """Call ensure_alert_partitions(today) on conn, committed on its own"""
        today = datetime.utcnow().date()
        return self._call_maintenance_function(
            conn,
            "SELECT partition_name, status FROM ensure_alert_partitions(%s)",
            (today,)
        )

    def _drop_old_partitions(self, conn) -> List[tuple]:
        """Call drop_old_alert_partitions() on conn, committed on its own"""
        return self._call_maintenance_function(
            conn,
            "SELECT partition_name, status FROM drop_old_alert_partitions()"
        )

    def _refresh_alert_summary(self, conn) -> List[tuple]:
        """Call refresh_alert_summary() on conn, committed on its own"""
        return self._call_maintenance_function(conn, "SELECT refresh_alert_summary()")

    def _ensure_partitions_or_exit(self):
        """
        Startup gate: this service cannot insert alerts into weeks that have
        no partition, so verify/create them before doing anything else.
        Fails fast (sys.exit(1)) on error -- no failsafe, no degraded start.
        """
        conn = None
        try:
            conn = self.db_pool.getconn()
            rows = self._ensure_partitions(conn)
            created = [name for name, status in rows if status == 'created']
            self.logger.info(
                f"Startup partition check: {len(rows)} week(s) verified"
                + (f", created: {created}" if created else "")
            )
        except Exception as e:
            self.logger.critical(f"Startup partition check failed, cannot start without partitions: {e}", exc_info=True)
            sys.exit(1)
        finally:
            if conn:
                self.db_pool.putconn(conn)

    def _is_maintenance_due(self) -> bool:
        """Whether >=24h have elapsed since the last partition maintenance run"""
        return (time.time() - self._last_maintenance) >= self.MAINTENANCE_INTERVAL_SECONDS

    def _run_periodic_maintenance(self):
        """
        24h partition/summary upkeep: ensure_alert_partitions (keep future
        weeks pre-created), drop_old_alert_partitions (retention), and
        refresh_alert_summary (materialized view). Logged loudly on failure
        but does not crash the service -- the startup gate already
        guarantees headroom, so a single missed maintenance cycle is not
        immediately fatal and will be retried on the next 24h tick.
        """
        conn = None
        try:
            conn = self.db_pool.getconn()

            partitions = self._ensure_partitions(conn)
            created = [name for name, status in partitions if status == 'created']
            self.logger.info(
                f"Partition maintenance: {len(partitions)} week(s) verified"
                + (f", created: {created}" if created else "")
            )

            # drop_old_alert_partitions() catches its own per-partition errors
            # and returns them as ordinary ('name', 'error: ...') rows rather
            # than raising (see sql/init/03-functions.sql) -- filter those
            # out of the success summary and log them loudly instead of
            # letting them disappear silently.
            dropped = self._drop_old_partitions(conn)
            dropped_names = [name for name, status in dropped if status == 'dropped']
            drop_errors = [
                (name, status) for name, status in dropped
                if status not in ('dropped', 'no old partitions to drop')
            ]
            self.logger.info(f"Old partition cleanup: dropped={dropped_names or 'none'}")
            for name, status in drop_errors:
                self.logger.error(f"Failed to drop old partition {name}: {status}")

            # refresh_alert_summary() likewise catches its own exception and
            # returns an 'Error refreshing view: ...' string instead of
            # raising -- detect that and log it at ERROR, not INFO.
            refreshed = self._refresh_alert_summary(conn)
            refresh_result = refreshed[0][0] if refreshed else 'no result'
            if isinstance(refresh_result, str) and refresh_result.startswith('Error'):
                self.logger.error(f"Materialized view refresh failed: {refresh_result}")
            else:
                self.logger.info(f"Materialized view refresh: {refresh_result}")

        except Exception as e:
            self.logger.error(f"Periodic partition maintenance failed: {e}", exc_info=True)
        finally:
            if conn:
                self.db_pool.putconn(conn)

    def _run_periodic_cycle(self):
        """
        One iteration of the 60s stats loop: print stats, run 24h partition
        maintenance when due, and touch the heartbeat file to signal the
        cycle completed without the process dying.
        """
        self._print_statistics()

        if self._is_maintenance_due():
            self._run_periodic_maintenance()
            self._last_maintenance = time.time()

        heartbeat.beat(self.heartbeat_path)

    def start(self):
        """Start the database logger"""
        self.logger.info("Starting database logger")

        # Start batch processor thread
        self.batch_thread = Thread(target=self._batch_processor, daemon=True)
        self.batch_thread.start()

        # Start MQTT loop
        self.mqtt_client.loop_start()

        self.logger.info("Database logger started successfully")

        # Print statistics periodically
        try:
            while self.running.is_set():
                time.sleep(60)
                self._run_periodic_cycle()

        except KeyboardInterrupt:
            self.logger.info("Keyboard interrupt received")
            self.shutdown()

    def shutdown(self):
        """Graceful shutdown"""
        self.logger.info("Shutting down database logger...")

        # Stop accepting new messages
        self.running.clear()

        # Stop MQTT
        self.mqtt_client.loop_stop()
        self.mqtt_client.disconnect()

        # Wait for batch processor to finish
        if self.batch_thread and self.batch_thread.is_alive():
            self.logger.info("Waiting for batch processor to finish...")
            self.batch_thread.join(timeout=10)

        # Print final statistics
        self._print_statistics()

        # Close database pool
        if self.db_pool:
            self.db_pool.closeall()

        self.logger.info("Database logger stopped")


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print("\nReceived shutdown signal, stopping...", file=sys.stderr)
    if hasattr(signal_handler, 'logger_instance'):
        signal_handler.logger_instance.shutdown()
    sys.exit(0)


def main():
    """Main entry point"""
    # Parse command line arguments
    import argparse

    parser = argparse.ArgumentParser(description='Alert Manager Database Logger')
    parser.add_argument('--config', required=True, help='Path to configuration file')
    args = parser.parse_args()

    # Create logger instance
    logger = DatabaseLogger(args.config)

    # Setup signal handlers
    signal_handler.logger_instance = logger
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start logger
    logger.start()


if __name__ == '__main__':
    main()
