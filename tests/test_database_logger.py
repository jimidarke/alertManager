"""
Tests for the Task 3 hardening added to src/database_logger.py:

- startup partition-check gate (sys.exit(1) on failure)
- 24h-interval gating for periodic partition maintenance inside the
  existing 60s stats loop
- batch failure isolation: rollback -> ensure_alert_partitions -> retry
  batch once -> row-by-row fallback on second failure
- heartbeat touched each healthy cycle
- MQTT clean_session=False

No live Postgres or MQTT broker is available in this environment (and per
project rule, docker/docker-compose is never invoked from automation). Every
test here mocks the psycopg2 connection/pool and the MQTT client to verify
this module's own control flow -- not real Postgres or MQTT behavior. Real
DB behavior (actual ensure_alert_partitions SQL, real constraint errors
triggering the retry path, REFRESH MATERIALIZED VIEW CONCURRENTLY semantics)
must be verified against a scratch Postgres before this branch is deployed.
"""

import json
import logging
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from database_logger import DatabaseLogger  # noqa: E402

# Captured at import time, before any test patches these class attributes,
# so tests that want the *real* method body (not the fixture's mock) can
# restore it explicitly via mocker.patch.object(DatabaseLogger, name, _REAL_*).
_REAL_ENSURE_PARTITIONS = DatabaseLogger._ensure_partitions
_REAL_DROP_OLD_PARTITIONS = DatabaseLogger._drop_old_partitions
_REAL_REFRESH_ALERT_SUMMARY = DatabaseLogger._refresh_alert_summary


# ---------------------------------------------------------------------------
# Fixture: build a DatabaseLogger instance with every network resource
# (config file is real but trivial; DB pool, MQTT client, and the startup
# partition gate) mocked out so __init__ never touches a real DB or broker.
# ---------------------------------------------------------------------------

@pytest.fixture
def make_logger(mocker, tmp_path):
    def _make(startup_partition_rows=None, startup_partition_error=None):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"mqtt": {"broker": "test-broker", "port": 1883}}))

        mocker.patch.object(
            DatabaseLogger, '_setup_logging',
            return_value=logging.getLogger('test_database_logger')
        )
        mock_pool = mocker.MagicMock(name="db_pool")
        mocker.patch.object(DatabaseLogger, '_create_db_pool', return_value=mock_pool)
        mocker.patch.object(DatabaseLogger, '_setup_mqtt', return_value=mocker.MagicMock(name="mqtt_client"))

        if startup_partition_error is not None:
            mocker.patch.object(DatabaseLogger, '_ensure_partitions', side_effect=startup_partition_error)
        else:
            rows = startup_partition_rows if startup_partition_rows is not None else [("alerts_2026_w10", "already_exists")]
            mocker.patch.object(DatabaseLogger, '_ensure_partitions', return_value=rows)

        instance = DatabaseLogger(str(config_path))

        # The startup partition gate already exercised db_pool.getconn()/
        # putconn() once during __init__ above. Reset call bookkeeping so
        # tests that assert on call counts/args measure only what happens
        # after construction, not construction-time traffic.
        mock_pool.reset_mock()

        return instance

    return _make


def _sample_alerts(n=3):
    return [
        {
            "timestamp": 1700000000 + i,
            "level": "critical",
            "category": "system",
            "title": f"alert-{i}",
            "message": f"message body {i}",
            "source": "test-source",
            "client": "testclient",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Startup partition gate
# ---------------------------------------------------------------------------

def test_startup_gate_succeeds_without_exit(make_logger):
    # If the startup gate raised (or sys.exit'd), construction itself would
    # fail -- successfully returning an instance already proves the gate
    # passed cleanly.
    instance = make_logger(startup_partition_rows=[("alerts_2026_w10", "created")])
    assert instance is not None


def test_startup_gate_returns_connection_to_pool(mocker, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mqtt": {}}))

    mocker.patch.object(DatabaseLogger, '_setup_logging', return_value=logging.getLogger('test_database_logger'))
    mock_pool = mocker.MagicMock()
    mocker.patch.object(DatabaseLogger, '_create_db_pool', return_value=mock_pool)
    mocker.patch.object(DatabaseLogger, '_setup_mqtt', return_value=mocker.MagicMock())
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', return_value=[("alerts_2026_w10", "created")])

    DatabaseLogger(str(config_path))

    mock_pool.getconn.assert_called_once()
    mock_pool.putconn.assert_called_once_with(mock_pool.getconn.return_value)


def test_startup_gate_exits_1_on_ensure_partitions_failure(make_logger):
    with pytest.raises(SystemExit) as exc_info:
        make_logger(startup_partition_error=Exception("partition function missing"))

    assert exc_info.value.code == 1


def test_startup_gate_does_not_reach_mqtt_setup_on_failure(mocker, tmp_path):
    # sys.exit(1) must happen before _setup_mqtt is ever called -- the
    # service must not start consuming MQTT messages it can't persist.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mqtt": {}}))

    mocker.patch.object(DatabaseLogger, '_setup_logging', return_value=logging.getLogger('test_database_logger'))
    mocker.patch.object(DatabaseLogger, '_create_db_pool', return_value=mocker.MagicMock())
    mock_setup_mqtt = mocker.patch.object(DatabaseLogger, '_setup_mqtt', return_value=mocker.MagicMock())
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', side_effect=Exception("boom"))

    with pytest.raises(SystemExit):
        DatabaseLogger(str(config_path))

    mock_setup_mqtt.assert_not_called()


def test_ensure_partitions_real_method_issues_correct_sql_and_commits(make_logger, mocker):
    # Unlike the fixture default (which replaces _ensure_partitions itself),
    # this test exercises the real method body against a mocked connection.
    instance = make_logger()
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', _REAL_ENSURE_PARTITIONS)  # restore real method

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("alerts_2026_w10", "created")]

    rows = instance._ensure_partitions(conn)

    assert rows == [("alerts_2026_w10", "created")]
    sql, params = cursor.execute.call_args.args
    assert "ensure_alert_partitions" in sql
    assert len(params) == 1  # (today_date,)
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_ensure_partitions_rolls_back_and_reraises_on_error(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', _REAL_ENSURE_PARTITIONS)

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("relation does not exist")

    with pytest.raises(Exception, match="relation does not exist"):
        instance._ensure_partitions(conn)

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# MQTT clean_session=False
# ---------------------------------------------------------------------------

def test_mqtt_client_created_with_clean_session_false(mocker, tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"mqtt": {"broker": "test-broker", "port": 1883}}))

    mocker.patch.object(DatabaseLogger, '_setup_logging', return_value=logging.getLogger('test_database_logger'))
    mocker.patch.object(DatabaseLogger, '_create_db_pool', return_value=mocker.MagicMock())
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', return_value=[])

    mock_client_cls = mocker.patch('database_logger.mqtt.Client')
    mock_client_cls.return_value.connect.return_value = None

    DatabaseLogger(str(config_path))

    _, kwargs = mock_client_cls.call_args
    assert kwargs['clean_session'] is False
    assert kwargs['client_id'] == "database-logger"


# ---------------------------------------------------------------------------
# 24h-interval gating for periodic maintenance
# ---------------------------------------------------------------------------

def test_is_maintenance_due_false_when_recent(make_logger):
    instance = make_logger()
    instance._last_maintenance = time.time()
    assert instance._is_maintenance_due() is False


def test_is_maintenance_due_true_after_24h_elapsed(make_logger):
    instance = make_logger()
    instance._last_maintenance = time.time() - DatabaseLogger.MAINTENANCE_INTERVAL_SECONDS - 1
    assert instance._is_maintenance_due() is True


def test_is_maintenance_due_false_just_under_24h(make_logger):
    instance = make_logger()
    instance._last_maintenance = time.time() - DatabaseLogger.MAINTENANCE_INTERVAL_SECONDS + 5
    assert instance._is_maintenance_due() is False


def test_periodic_cycle_skips_maintenance_when_not_due(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_print_statistics')
    mocker.patch.object(instance, '_is_maintenance_due', return_value=False)
    mock_maintenance = mocker.patch.object(instance, '_run_periodic_maintenance')
    mock_beat = mocker.patch('database_logger.heartbeat.beat')

    last_maintenance_before = instance._last_maintenance
    instance._run_periodic_cycle()

    mock_maintenance.assert_not_called()
    assert instance._last_maintenance == last_maintenance_before
    mock_beat.assert_called_once_with(instance.heartbeat_path)


def test_periodic_cycle_runs_maintenance_and_resets_timer_when_due(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_print_statistics')
    mocker.patch.object(instance, '_is_maintenance_due', return_value=True)
    mock_maintenance = mocker.patch.object(instance, '_run_periodic_maintenance')
    mocker.patch('database_logger.heartbeat.beat')

    stale_timestamp = time.time() - 1000
    instance._last_maintenance = stale_timestamp

    instance._run_periodic_cycle()

    mock_maintenance.assert_called_once()
    assert instance._last_maintenance > stale_timestamp


def test_periodic_cycle_touches_heartbeat_every_healthy_cycle(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_print_statistics')
    mocker.patch.object(instance, '_is_maintenance_due', return_value=True)
    mocker.patch.object(instance, '_run_periodic_maintenance')
    mock_beat = mocker.patch('database_logger.heartbeat.beat')

    instance._run_periodic_cycle()

    mock_beat.assert_called_once_with(instance.heartbeat_path)


def test_run_periodic_maintenance_calls_all_three_in_order(make_logger, mocker):
    instance = make_logger()
    order = []
    mocker.patch.object(instance, '_ensure_partitions', side_effect=lambda conn: order.append('ensure') or [])
    mocker.patch.object(instance, '_drop_old_partitions', side_effect=lambda conn: order.append('drop') or [])
    mocker.patch.object(instance, '_refresh_alert_summary', side_effect=lambda conn: order.append('refresh') or [])

    instance._run_periodic_maintenance()

    assert order == ['ensure', 'drop', 'refresh']
    instance.db_pool.putconn.assert_called()


def test_run_periodic_maintenance_logs_and_swallows_failure(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_drop_old_partitions', side_effect=Exception("lock timeout"))
    mocker.patch.object(instance, '_refresh_alert_summary')
    error_log = mocker.patch.object(instance.logger, 'error')

    # Must not raise -- periodic maintenance failure is logged loudly but
    # does not crash the process (unlike the startup gate).
    instance._run_periodic_maintenance()

    error_log.assert_called_once()
    assert "lock timeout" in error_log.call_args.args[0]
    # Connection must still be returned to the pool even on failure.
    instance.db_pool.putconn.assert_called()


def test_run_periodic_maintenance_logs_error_for_partition_that_failed_to_drop(make_logger, mocker):
    # drop_old_alert_partitions() catches its own per-partition errors and
    # returns them as ('name', 'error: ...') rows instead of raising -- this
    # must not disappear silently from the success summary.
    instance = make_logger()
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_drop_old_partitions', return_value=[
        ("alerts_2024_w01", "dropped"),
        ("alerts_2024_w02", "error: permission denied for table alerts_2024_w02"),
    ])
    mocker.patch.object(instance, '_refresh_alert_summary', return_value=[("Materialized view refreshed at now",)])
    error_log = mocker.patch.object(instance.logger, 'error')

    instance._run_periodic_maintenance()  # must not raise

    error_log.assert_called_once()
    logged = error_log.call_args.args[0]
    assert "alerts_2024_w02" in logged
    assert "permission denied" in logged


def test_run_periodic_maintenance_treats_no_old_partitions_as_success_not_error(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_drop_old_partitions', return_value=[("none", "no old partitions to drop")])
    mocker.patch.object(instance, '_refresh_alert_summary', return_value=[("Materialized view refreshed at now",)])
    error_log = mocker.patch.object(instance.logger, 'error')

    instance._run_periodic_maintenance()

    error_log.assert_not_called()


def test_run_periodic_maintenance_logs_error_for_failed_view_refresh(make_logger, mocker):
    # refresh_alert_summary() likewise catches its own exception and returns
    # an 'Error refreshing view: ...' string instead of raising.
    instance = make_logger()
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_drop_old_partitions', return_value=[])
    mocker.patch.object(
        instance, '_refresh_alert_summary',
        return_value=[("Error refreshing view: deadlock detected",)]
    )
    error_log = mocker.patch.object(instance.logger, 'error')

    instance._run_periodic_maintenance()  # must not raise

    error_log.assert_called_once()
    assert "deadlock detected" in error_log.call_args.args[0]


def test_run_periodic_maintenance_successful_refresh_logs_info_not_error(make_logger, mocker):
    instance = make_logger()
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_drop_old_partitions', return_value=[])
    mocker.patch.object(
        instance, '_refresh_alert_summary',
        return_value=[("Materialized view refreshed at 2026-07-20 12:00:00",)]
    )
    error_log = mocker.patch.object(instance.logger, 'error')

    instance._run_periodic_maintenance()

    error_log.assert_not_called()


def test_call_maintenance_function_commits_independently_per_call(make_logger, mocker):
    # Guards against a real bug: REFRESH MATERIALIZED VIEW CONCURRENTLY
    # cannot run inside an already-open transaction block, so each
    # maintenance call must be committed on its own rather than all three
    # sharing one open transaction.
    instance = make_logger()
    mocker.patch.object(DatabaseLogger, '_ensure_partitions', _REAL_ENSURE_PARTITIONS)
    mocker.patch.object(DatabaseLogger, '_drop_old_partitions', _REAL_DROP_OLD_PARTITIONS)
    mocker.patch.object(DatabaseLogger, '_refresh_alert_summary', _REAL_REFRESH_ALERT_SUMMARY)

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [("x", "y")]

    instance._ensure_partitions(conn)
    instance._drop_old_partitions(conn)
    instance._refresh_alert_summary(conn)

    assert conn.commit.call_count == 3


# ---------------------------------------------------------------------------
# Batch failure isolation: rollback -> ensure_alert_partitions -> retry
# once -> row-by-row fallback on second failure
# ---------------------------------------------------------------------------

def test_insert_alert_batch_empty_list_short_circuits(make_logger):
    instance = make_logger()
    assert instance._insert_alert_batch([]) == 0
    instance.db_pool.getconn.assert_not_called()


def test_insert_alert_batch_success_first_try_skips_recovery_path(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(3)

    mock_execute = mocker.patch.object(instance, '_execute_batch_insert')
    mock_ensure = mocker.patch.object(instance, '_ensure_partitions')
    mock_row_by_row = mocker.patch.object(instance, '_insert_alerts_row_by_row')

    result = instance._insert_alert_batch(alerts)

    assert result == len(alerts)
    mock_execute.assert_called_once()
    mock_ensure.assert_not_called()
    mock_row_by_row.assert_not_called()
    instance.db_pool.putconn.assert_called_once()


def test_insert_alert_batch_exact_sequence_on_first_failure_then_retry_succeeds(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(3)

    order = []
    mock_conn = instance.db_pool.getconn.return_value
    mock_conn.rollback.side_effect = lambda: order.append('rollback')

    mocker.patch.object(
        instance, '_execute_batch_insert',
        side_effect=[Exception("constraint violation"), None]  # fails, then succeeds
    )
    mocker.patch.object(instance, '_ensure_partitions', side_effect=lambda conn: order.append('ensure_partitions') or [])
    mock_row_by_row = mocker.patch.object(instance, '_insert_alerts_row_by_row')

    result = instance._insert_alert_batch(alerts)

    assert result == len(alerts)
    assert order == ['rollback', 'ensure_partitions']  # retry itself doesn't append, but happened after
    assert instance._execute_batch_insert.call_count == 2
    mock_row_by_row.assert_not_called()


def test_insert_alert_batch_falls_back_to_row_by_row_on_second_failure(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(3)

    call_order = []
    mock_conn = instance.db_pool.getconn.return_value
    mock_conn.rollback.side_effect = lambda: call_order.append('rollback')

    mocker.patch.object(
        instance, '_execute_batch_insert',
        side_effect=Exception("still broken")  # fails every time (first attempt + retry)
    )
    mocker.patch.object(instance, '_ensure_partitions', side_effect=lambda conn: call_order.append('ensure_partitions') or [])
    mock_row_by_row = mocker.patch.object(
        instance, '_insert_alerts_row_by_row',
        side_effect=lambda conn, a: call_order.append('row_by_row') or 2
    )

    result = instance._insert_alert_batch(alerts)

    assert result == 2
    assert instance._execute_batch_insert.call_count == 2  # first attempt + exactly one retry, no more
    mock_row_by_row.assert_called_once_with(mock_conn, alerts)
    # Exact required order: rollback (after 1st failure) -> ensure_partitions
    # -> [retry attempt happens here, itself raising] -> rollback (after 2nd
    # failure) -> row-by-row fallback.
    assert call_order == ['rollback', 'ensure_partitions', 'rollback', 'row_by_row']


def test_insert_alert_batch_ensure_partitions_failure_does_not_block_retry(make_logger, mocker):
    # ensure_alert_partitions itself erroring must not prevent the retry
    # attempt -- the original batch failure may be unrelated to partitions.
    instance = make_logger()
    alerts = _sample_alerts(2)

    mocker.patch.object(
        instance, '_execute_batch_insert',
        side_effect=[Exception("boom"), None]  # fails, then succeeds on retry
    )
    mocker.patch.object(instance, '_ensure_partitions', side_effect=Exception("ensure_alert_partitions unavailable"))
    mock_row_by_row = mocker.patch.object(instance, '_insert_alerts_row_by_row')

    result = instance._insert_alert_batch(alerts)

    assert result == len(alerts)
    assert instance._execute_batch_insert.call_count == 2
    mock_row_by_row.assert_not_called()


def test_insert_alert_batch_returns_connection_to_pool_on_every_path(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(2)

    mocker.patch.object(instance, '_execute_batch_insert', side_effect=Exception("boom"))
    mocker.patch.object(instance, '_ensure_partitions', return_value=[])
    mocker.patch.object(instance, '_insert_alerts_row_by_row', return_value=0)

    instance._insert_alert_batch(alerts)

    instance.db_pool.putconn.assert_called_once_with(instance.db_pool.getconn.return_value)


# ---------------------------------------------------------------------------
# Row-by-row fallback: per-row isolation, full-payload ERROR logging, count
# ---------------------------------------------------------------------------

def test_insert_alerts_row_by_row_all_succeed(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(3)

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value  # no side effects -> success

    inserted = instance._insert_alerts_row_by_row(conn, alerts)

    assert inserted == 3
    assert cursor.execute.call_count == 3
    assert conn.commit.call_count == 3
    conn.rollback.assert_not_called()


def test_insert_alerts_row_by_row_isolates_single_bad_row(make_logger, mocker):
    instance = make_logger()
    alerts = _sample_alerts(3)  # titles: alert-0, alert-1, alert-2

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    def execute_side_effect(query, params):
        # Fail only the middle row (identified by its title, which is
        # params[3] per _alert_to_values' column order).
        if params[3] == "alert-1":
            raise Exception("duplicate key value violates unique constraint")

    cursor.execute.side_effect = execute_side_effect
    error_log = mocker.patch.object(instance.logger, 'error')

    inserted = instance._insert_alerts_row_by_row(conn, alerts)

    assert inserted == 2  # alert-0 and alert-2 succeeded; alert-1 failed
    assert conn.commit.call_count == 2
    assert conn.rollback.call_count == 1
    error_log.assert_called_once()


def test_insert_alerts_row_by_row_logs_full_payload_on_failure(make_logger, mocker):
    instance = make_logger()
    bad_alert = {
        "timestamp": 1700000000,
        "level": "critical",
        "category": "system",
        "title": "distinctive-title-xyz",
        "message": "distinctive-message-body",
        "source": "distinctive-source",
        "client": "distinctive-client",
    }

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("insert failed")
    error_log = mocker.patch.object(instance.logger, 'error')

    inserted = instance._insert_alerts_row_by_row(conn, [bad_alert])

    assert inserted == 0
    logged_message = error_log.call_args.args[0]
    # Full payload must be present in the ERROR log, not just a summary.
    assert "distinctive-title-xyz" in logged_message
    assert "distinctive-message-body" in logged_message
    assert "distinctive-source" in logged_message
    assert "distinctive-client" in logged_message


def test_insert_alerts_row_by_row_empty_list(make_logger, mocker):
    instance = make_logger()
    conn = mocker.MagicMock()

    inserted = instance._insert_alerts_row_by_row(conn, [])

    assert inserted == 0
    conn.cursor.assert_not_called()
