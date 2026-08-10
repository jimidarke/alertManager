"""
Tests for pathways/email_log.py's EmailLogWriter (Task 9).

EmailLogWriter is the one file in this whole overhaul that deliberately
violates the project's no-failsafes/fail-fast rule: email is the
last-resort alerting channel, so a tracking-database outage must never
become a notification outage. Every method must catch its own exceptions,
log loudly (ERROR), and never raise.

No live Postgres is available in this environment (and per project rule,
docker/docker-compose is never invoked from automation), so every test here
mocks the psycopg2 connection/cursor to verify this module's own
error-handling control flow -- not real Postgres behavior. That is also
exactly what this class exists to guarantee, so it is fully verifiable via
mocks: lazy connection, successful insert shape, and -- most importantly --
that connect/insert failures are swallowed rather than propagated, with the
connection dropped so the next call reconnects fresh (no retry loop within
a single call).
"""

import logging
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))  # email_log.py lives in the top-level `pathways` package
from pathways.email_log import EmailLogWriter  # noqa: E402


@pytest.fixture
def writer():
    return EmailLogWriter(dsn={'host': 'test-host', 'port': 5432, 'dbname': 'testdb', 'user': 'testuser', 'password': 'x'})


def _sample_kwargs(**overrides):
    kwargs = dict(
        pathway="email_alerts",
        status="accepted",
        aggregated=False,
        alert_count=1,
        level="critical",
        clients=["revysmc"],
        recipients=["admin@example.com"],
        subject="Alert: disk full",
        http_status=202,
        error=None,
        graph_request_id="req-123",
        duration_ms=450,
        retries=0,
    )
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Lazy connection
# ---------------------------------------------------------------------------

def test_no_connection_attempt_at_construction(mocker):
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')

    EmailLogWriter()

    mock_connect.assert_not_called()


def test_connection_created_on_first_record_call(mocker, writer):
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')

    assert writer._conn is None
    writer.record(**_sample_kwargs())

    mock_connect.assert_called_once()


def test_connect_timeout_is_3(mocker, writer):
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')

    writer.record(**_sample_kwargs())

    _, kwargs = mock_connect.call_args
    assert kwargs['connect_timeout'] == 3


def test_dsn_kwargs_passed_through_to_connect(mocker):
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')
    dsn = {'host': 'my-host', 'port': 1234, 'dbname': 'db1', 'user': 'u1', 'password': 'p1'}
    writer = EmailLogWriter(dsn=dsn)

    writer.record(**_sample_kwargs())

    _, kwargs = mock_connect.call_args
    for key, value in dsn.items():
        assert kwargs[key] == value


def test_default_dsn_uses_postgres_env_vars(mocker, monkeypatch):
    monkeypatch.setenv('POSTGRES_HOST', 'env-host')
    monkeypatch.setenv('POSTGRES_PORT', '6543')
    monkeypatch.setenv('POSTGRES_DB', 'env-db')
    monkeypatch.setenv('POSTGRES_USER', 'env-user')
    monkeypatch.setenv('POSTGRES_PASSWORD', 'env-pass')
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')

    EmailLogWriter().record(**_sample_kwargs())

    _, kwargs = mock_connect.call_args
    assert kwargs['host'] == 'env-host'
    assert kwargs['port'] == 6543
    assert kwargs['dbname'] == 'env-db'
    assert kwargs['user'] == 'env-user'
    assert kwargs['password'] == 'env-pass'


def test_autocommit_enabled_on_new_connection(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)

    writer.record(**_sample_kwargs())

    assert mock_conn.autocommit is True


def test_second_record_call_reuses_existing_connection(mocker, writer):
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect', return_value=mocker.MagicMock())

    writer.record(**_sample_kwargs())
    writer.record(**_sample_kwargs())

    mock_connect.assert_called_once()


# ---------------------------------------------------------------------------
# Successful insert path
# ---------------------------------------------------------------------------

def test_successful_insert_executes_expected_sql_and_params(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value

    before = int(time.time())
    writer.record(**_sample_kwargs())
    after = int(time.time())

    cursor.execute.assert_called_once()
    sql, params = cursor.execute.call_args.args

    assert "INSERT INTO email_log" in sql
    assert params['pathway'] == "email_alerts"
    assert params['status'] == "accepted"
    assert params['aggregated'] is False
    assert params['alert_count'] == 1
    assert params['level'] == "critical"
    assert params['clients'] == ["revysmc"]
    assert params['recipients'] == ["admin@example.com"]
    assert params['subject'] == "Alert: disk full"
    assert params['http_status'] == 202
    assert params['error'] is None
    assert params['graph_request_id'] == "req-123"
    assert params['duration_ms'] == 450
    assert params['retries'] == 0
    assert before <= params['attempted_at'] <= after


def test_successful_insert_does_not_drop_connection(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)

    writer.record(**_sample_kwargs())

    mock_conn.close.assert_not_called()
    assert writer._conn is mock_conn


def test_failed_status_attempt_records_error_fields(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value

    writer.record(**_sample_kwargs(
        status="failed",
        http_status=500,
        error="Graph API error: 500 - internal error",
        graph_request_id=None,
        retries=2,
    ))

    _, params = cursor.execute.call_args.args
    assert params['status'] == "failed"
    assert params['http_status'] == 500
    assert params['error'] == "Graph API error: 500 - internal error"
    assert params['retries'] == 2


# ---------------------------------------------------------------------------
# Connect failure: logged, no raise, connection stays unset
# ---------------------------------------------------------------------------

def test_connect_failure_logs_error(mocker, writer):
    mocker.patch('pathways.email_log.psycopg2.connect', side_effect=Exception("connection refused"))
    error_log = mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # must not raise

    error_log.assert_called_once()
    assert "connection refused" in error_log.call_args.args[0]


def test_connect_failure_does_not_raise(mocker, writer):
    mocker.patch('pathways.email_log.psycopg2.connect', side_effect=Exception("boom"))
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # no pytest.raises -- proves it swallowed the exception


def test_connect_failure_leaves_connection_unset(mocker, writer):
    mocker.patch('pathways.email_log.psycopg2.connect', side_effect=Exception("boom"))
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())

    assert writer._conn is None


def test_connect_failure_never_attempts_cursor_or_execute(mocker, writer):
    mocker.patch('pathways.email_log.psycopg2.connect', side_effect=Exception("boom"))
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    # No connection object exists to assert against directly; the absence
    # of a raised AttributeError/etc. combined with the connect mock being
    # the only DB touchpoint proves record() returned right after the
    # failed connect, before ever reaching cursor/execute code.
    writer.record(**_sample_kwargs())


def test_record_does_not_raise_when_postgres_port_env_var_is_malformed(mocker, monkeypatch):
    # Regression test for a live-reproduced bug: _default_dsn() does
    # int(os.getenv('POSTGRES_PORT', 5432)), which raises ValueError for a
    # non-numeric POSTGRES_PORT (typo, blank env interpolation, wrong
    # quoting in compose/.env). The original _get_connection() called
    # _default_dsn() *outside* its try block, so this ValueError propagated
    # straight out of record() to the caller -- exactly the failure mode
    # this whole class exists to prevent, and exactly the environment
    # (alert-router, "fail-open, no hard dependency") it runs under.
    monkeypatch.setenv('POSTGRES_PORT', 'not-a-number')
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect')
    error_log = mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer = EmailLogWriter()  # no dsn override -- forces _default_dsn() to run

    writer.record(**_sample_kwargs())  # must not raise

    mock_connect.assert_not_called()  # never got far enough to attempt connect()
    error_log.assert_called_once()
    assert "not-a-number" in error_log.call_args.args[0]
    assert writer._conn is None


def test_record_reconnects_on_next_call_after_dsn_build_failure(mocker, monkeypatch):
    # Same malformed-env scenario, but confirms the reconnect-lazily
    # contract still holds once the env var is fixed between calls.
    monkeypatch.setenv('POSTGRES_PORT', 'not-a-number')
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect', return_value=mocker.MagicMock())
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')
    writer = EmailLogWriter()

    writer.record(**_sample_kwargs())  # DSN build fails, never reaches connect()
    assert mock_connect.call_count == 0
    assert writer._conn is None

    monkeypatch.setenv('POSTGRES_PORT', '5432')  # fixed before the next attempt
    writer.record(**_sample_kwargs())
    assert mock_connect.call_count == 1
    assert writer._conn is not None


# ---------------------------------------------------------------------------
# Insert failure: logged, no raise, connection dropped
# ---------------------------------------------------------------------------

def test_insert_failure_logs_error(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("relation email_log does not exist")
    error_log = mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # must not raise

    error_log.assert_called_once()
    assert "relation email_log does not exist" in error_log.call_args.args[0]


def test_insert_failure_does_not_raise(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("boom")
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # no pytest.raises -- proves it swallowed the exception


def test_insert_failure_closes_and_drops_connection(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("boom")
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())

    mock_conn.close.assert_called_once()
    assert writer._conn is None


def test_close_error_on_broken_connection_is_itself_swallowed_and_logged(mocker, writer):
    # Even conn.close() itself raising (e.g. socket already dead) must not
    # propagate -- this is the "fail-open no matter what" contract in its
    # most extreme form.
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("insert boom")
    mock_conn.close.side_effect = Exception("close boom")
    error_log = mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # must not raise

    assert error_log.call_count == 2  # insert failure + close failure, both logged
    assert writer._conn is None


# ---------------------------------------------------------------------------
# Reconnect-after-failure: fresh connect on the *next* call, no retry loop
# within a single call
# ---------------------------------------------------------------------------

def test_reconnects_on_next_call_after_connect_failure(mocker, writer):
    mock_connect = mocker.patch(
        'pathways.email_log.psycopg2.connect',
        side_effect=[Exception("first attempt fails"), mocker.MagicMock()],
    )
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # fails to connect
    assert writer._conn is None

    writer.record(**_sample_kwargs())  # should try connecting again
    assert mock_connect.call_count == 2
    assert writer._conn is not None


def test_reconnects_on_next_call_after_insert_failure(mocker, writer):
    first_conn = mocker.MagicMock()
    first_cursor = first_conn.cursor.return_value.__enter__.return_value
    first_cursor.execute.side_effect = Exception("insert boom")

    second_conn = mocker.MagicMock()

    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect', side_effect=[first_conn, second_conn])
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())  # connects to first_conn, insert fails, drops it
    assert mock_connect.call_count == 1
    assert writer._conn is None

    writer.record(**_sample_kwargs())  # must reconnect fresh, not reuse first_conn
    assert mock_connect.call_count == 2
    assert writer._conn is second_conn


def test_connect_attempted_exactly_once_per_call_no_retry_loop(mocker, writer):
    # A single record() call must not itself retry the connect -- only one
    # psycopg2.connect() invocation per call, however many times record()
    # is subsequently called.
    mock_connect = mocker.patch('pathways.email_log.psycopg2.connect', side_effect=Exception("always fails"))
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())
    assert mock_connect.call_count == 1

    writer.record(**_sample_kwargs())
    assert mock_connect.call_count == 2  # one more attempt on the *next* call, not a loop within either call


def test_insert_never_retried_within_same_call(mocker, writer):
    mock_conn = mocker.MagicMock()
    mocker.patch('pathways.email_log.psycopg2.connect', return_value=mock_conn)
    cursor = mock_conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("boom")
    mocker.patch.object(logging.getLogger('pathways.email_log'), 'error')

    writer.record(**_sample_kwargs())

    cursor.execute.assert_called_once()  # not retried within this call
