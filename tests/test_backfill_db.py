"""
Tests for tools/backfill_file_logs.py's non-parser logic:

- discover_log_files(): flat vs per-client on-disk shapes, since_days+7
  file-level pre-filter, unauthenticated/ folder treated like any other
  per-client folder.
- row_passes_guards(): the pure --since-days / max-non-manual-timestamp
  guard predicate.
- count_manual_rows() / get_max_non_manual_timestamp(): SQL shape against a
  mocked cursor.
- _row_to_values() / insert_batch(): the execute_values() call shape --
  correct columns/template, ingestion_source='manual', metadata=Json({...}).
- process_file(): "folder name wins" override, invalid-line rejection
  counting, per-row guard filtering, chunked insert.
- run(): abort-unless-force when manual rows already exist, --dry-run never
  opens a DB connection, --force proceeds anyway.

No live Postgres is available in this environment (and per project rule,
docker/docker-compose is never invoked from automation). Every DB-touching
test here mocks the psycopg2 connection/cursor to verify this module's own
control flow and the exact SQL/parameter shapes it builds -- not real
Postgres behavior (real constraint enforcement, real to_timestamp()/JSONB
round-tripping, real refresh_alert_summary() semantics) must still be
verified against a scratch Postgres before this tool is run against
production log data. See the Task 13 report for that flag.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from psycopg2.extras import Json

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "tools"))

import backfill_file_logs as bf  # noqa: E402


# ---------------------------------------------------------------------------
# discover_log_files
# ---------------------------------------------------------------------------

class TestDiscoverLogFiles:
    def test_missing_file_dir_returns_empty(self, tmp_path):
        assert bf.discover_log_files(tmp_path, since_days=90) == []

    def test_flat_and_per_client_files_discovered(self, tmp_path):
        file_root = tmp_path / "file"
        file_root.mkdir()
        (file_root / "2026-07-13.log").write_text("flat\n")
        client_dir = file_root / "acmecorp"
        client_dir.mkdir()
        (client_dir / "2026-07-13.log").write_text("client\n")

        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        results = bf.discover_log_files(tmp_path, since_days=90, now=now)

        by_client = {r.client: r.path.name for r in results}
        assert by_client[None] == "2026-07-13.log"
        assert by_client["acmecorp"] == "2026-07-13.log"

    def test_unauthenticated_folder_treated_as_client(self, tmp_path):
        file_root = tmp_path / "file"
        unauth = file_root / "unauthenticated"
        unauth.mkdir(parents=True)
        (unauth / "2026-07-13.log").write_text("trap\n")

        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        results = bf.discover_log_files(tmp_path, since_days=90, now=now)

        assert len(results) == 1
        assert results[0].client == "unauthenticated"

    def test_since_days_plus_grace_excludes_old_files(self, tmp_path):
        file_root = tmp_path / "file"
        file_root.mkdir()
        (file_root / "2026-01-01.log").write_text("old\n")
        (file_root / "2026-07-13.log").write_text("recent\n")

        now = datetime(2026, 7, 20, tzinfo=timezone.utc)
        results = bf.discover_log_files(tmp_path, since_days=90, now=now)

        names = {r.path.name for r in results}
        assert "2026-07-13.log" in names
        assert "2026-01-01.log" not in names

    def test_non_date_filenames_ignored(self, tmp_path):
        file_root = tmp_path / "file"
        file_root.mkdir()
        (file_root / "not-a-date.log").write_text("junk\n")
        (file_root / "readme.txt").write_text("junk\n")

        assert bf.discover_log_files(tmp_path, since_days=90) == []


# ---------------------------------------------------------------------------
# row_passes_guards
# ---------------------------------------------------------------------------

class TestRowPassesGuards:
    def test_older_than_since_days_rejected(self):
        assert bf.row_passes_guards(100, since_epoch=200, max_non_manual_epoch=None) is False

    def test_within_window_no_max_accepted(self):
        assert bf.row_passes_guards(250, since_epoch=200, max_non_manual_epoch=None) is True

    def test_equal_to_max_non_manual_rejected(self):
        """timestamp <= max(non-manual) is skipped -- strictly newer required."""
        assert bf.row_passes_guards(300, since_epoch=200, max_non_manual_epoch=300) is False

    def test_older_than_max_non_manual_rejected(self):
        assert bf.row_passes_guards(250, since_epoch=200, max_non_manual_epoch=300) is False

    def test_newer_than_max_non_manual_accepted(self):
        assert bf.row_passes_guards(301, since_epoch=200, max_non_manual_epoch=300) is True


# ---------------------------------------------------------------------------
# count_manual_rows / get_max_non_manual_timestamp -- mocked cursor
# ---------------------------------------------------------------------------

class _FakeCursor:
    def __init__(self, fetchone_result):
        self._fetchone_result = fetchone_result
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self._fetchone_result


class _FakeConn:
    def __init__(self, fetchone_result):
        self._cursor = _FakeCursor(fetchone_result)
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class TestManualRowGuardQueries:
    def test_count_manual_rows_true(self):
        conn = _FakeConn(fetchone_result=(True,))
        assert bf.count_manual_rows(conn) is True
        sql, _ = conn._cursor.executed[0]
        assert "ingestion_source = 'manual'" in sql

    def test_count_manual_rows_false(self):
        conn = _FakeConn(fetchone_result=(False,))
        assert bf.count_manual_rows(conn) is False

    def test_get_max_non_manual_timestamp_value(self):
        conn = _FakeConn(fetchone_result=(1736000000,))
        result = bf.get_max_non_manual_timestamp(conn)
        assert result == 1736000000
        assert isinstance(result, int)
        sql, _ = conn._cursor.executed[0]
        assert "ingestion_source <> 'manual'" in sql

    def test_get_max_non_manual_timestamp_none_when_empty(self):
        conn = _FakeConn(fetchone_result=(None,))
        assert bf.get_max_non_manual_timestamp(conn) is None


# ---------------------------------------------------------------------------
# _row_to_values / insert_batch -- execute_values() call shape
# ---------------------------------------------------------------------------

class TestRowToValues:
    def test_shape_and_ingestion_source(self):
        row = {
            "timestamp": 1735689600,
            "level": "critical",
            "category": "system",
            "title": "Disk usage warning",
            "message": "Volume /data at 92% capacity",
            "source": "monitor-01",
            "client": "acmecorp",
        }
        values = bf._row_to_values(row, "/data/logs/file/acmecorp/2026-07-13.log")

        assert values[0] == 1735689600
        assert values[1] == "critical"
        assert values[2] == "system"
        assert values[3] == "Disk usage warning"
        assert values[4] == "Volume /data at 92% capacity"
        assert values[5] == "monitor-01"
        assert values[6] == "acmecorp"
        assert isinstance(values[7], Json)
        assert values[7].adapted == {"backfilled_from": "/data/logs/file/acmecorp/2026-07-13.log"}
        assert values[8] is None  # display_config
        assert values[9] is None  # routing
        assert values[10] == "manual"

    def test_client_none_for_flat_file_row(self):
        row = {
            "timestamp": 1735689600, "level": "info", "category": "system",
            "title": "t", "message": "m", "source": "s", "client": None,
        }
        values = bf._row_to_values(row, "/data/logs/file/2026-07-13.log")
        assert values[6] is None


class TestInsertBatch:
    def test_empty_rows_no_op(self):
        conn = _FakeConn(fetchone_result=None)
        assert bf.insert_batch(conn, [], "file.log") == 0
        assert conn.committed is False

    def test_calls_execute_values_with_correct_shape(self, mocker):
        mock_execute_values = mocker.patch("backfill_file_logs.execute_values")
        conn = _FakeConn(fetchone_result=None)

        rows = [
            {
                "timestamp": 1735689600 + i, "level": "critical", "category": "system",
                "title": f"t{i}", "message": f"m{i}", "source": "s", "client": "acmecorp",
            }
            for i in range(3)
        ]

        inserted = bf.insert_batch(conn, rows, "/data/logs/file/acmecorp/2026-07-13.log")

        assert inserted == 3
        assert conn.committed is True
        mock_execute_values.assert_called_once()

        _, call_args, call_kwargs = mock_execute_values.mock_calls[0]
        cur_arg, sql_arg, values_arg = call_args
        assert "INSERT INTO alerts" in sql_arg
        for col in ("timestamp", "level", "category", "title", "message",
                    "source", "client", "metadata", "display_config",
                    "routing", "ingestion_source"):
            assert col in sql_arg
        assert len(values_arg) == 3
        assert call_kwargs["template"] == bf._INSERT_TEMPLATE
        # every row tagged ingestion_source='manual'
        assert all(v[-1] == "manual" for v in values_arg)


# ---------------------------------------------------------------------------
# process_file -- folder-name-wins, reject counting, guard filtering
# ---------------------------------------------------------------------------

class TestProcessFile:
    def _make_conn_capturing_inserts(self, mocker):
        conn = _FakeConn(fetchone_result=None)
        inserted_rows = []

        def fake_insert_batch(conn_arg, rows, source_file):
            inserted_rows.extend(rows)
            return len(rows)

        mocker.patch("backfill_file_logs.insert_batch", side_effect=fake_insert_batch)
        return conn, inserted_rows

    def test_folder_name_wins_over_parsed_client(self, tmp_path, mocker):
        conn, inserted_rows = self._make_conn_capturing_inserts(mocker)

        # Line's own [client] bracket says "wrongclient" but the file lives
        # under the "acmecorp" folder -- folder must win.
        line = "1735689600 [critical] [system] monitor-01 [wrongclient]: Disk warning - 92% full"
        log_path = tmp_path / "2026-07-13.log"
        log_path.write_text(line + "\n")
        log_file = bf.LogFile(path=log_path, client="acmecorp")

        stats = bf.process_file(conn, log_file, since_epoch=0, max_non_manual_epoch=None,
                                 batch_size=500, logger=mocker.MagicMock())

        assert stats["parsed"] == 1
        assert stats["inserted"] == 1
        assert inserted_rows[0]["client"] == "acmecorp"

    def test_flat_file_client_stays_none(self, tmp_path, mocker):
        conn, inserted_rows = self._make_conn_capturing_inserts(mocker)

        line = "1735689600 [critical] [system] monitor-01 []: Disk warning - 92% full"
        log_path = tmp_path / "2026-07-13.log"
        log_path.write_text(line + "\n")
        log_file = bf.LogFile(path=log_path, client=None)

        bf.process_file(conn, log_file, since_epoch=0, max_non_manual_epoch=None,
                         batch_size=500, logger=mocker.MagicMock())

        assert inserted_rows[0]["client"] is None

    def test_invalid_line_counted_and_logged_with_file_line_context(self, tmp_path, mocker):
        conn, inserted_rows = self._make_conn_capturing_inserts(mocker)
        logger = mocker.MagicMock()

        lines = [
            "1735689600 [critical] [system] monitor-01 []: ok - fine",
            "garbage garbage garbage",
            "1735689700 [bogus] [system] monitor-01 []: bad level - oops",
        ]
        log_path = tmp_path / "2026-07-13.log"
        log_path.write_text("\n".join(lines) + "\n")
        log_file = bf.LogFile(path=log_path, client=None)

        stats = bf.process_file(conn, log_file, since_epoch=0, max_non_manual_epoch=None,
                                 batch_size=500, logger=logger)

        assert stats["parsed"] == 1
        assert stats["rejected"] == 2
        assert len(inserted_rows) == 1

        warning_calls = [c.args[0] for c in logger.warning.call_args_list]
        assert any(str(log_path) in msg and ":2:" in msg for msg in warning_calls)
        assert any(str(log_path) in msg and ":3:" in msg for msg in warning_calls)

    def test_rows_below_guard_are_skipped_not_inserted(self, tmp_path, mocker):
        conn, inserted_rows = self._make_conn_capturing_inserts(mocker)

        lines = [
            "1735689600 [critical] [system] monitor-01 []: old - before cutoff",
            "1735689900 [critical] [system] monitor-01 []: new - after cutoff",
        ]
        log_path = tmp_path / "2026-07-13.log"
        log_path.write_text("\n".join(lines) + "\n")
        log_file = bf.LogFile(path=log_path, client=None)

        stats = bf.process_file(conn, log_file, since_epoch=1735689700, max_non_manual_epoch=None,
                                 batch_size=500, logger=mocker.MagicMock())

        assert stats["parsed"] == 2
        assert stats["skipped_old"] == 1
        assert stats["inserted"] == 1
        assert inserted_rows[0]["title"] == "new"


# ---------------------------------------------------------------------------
# run() -- abort-unless-force, dry-run never touches the DB, force proceeds
# ---------------------------------------------------------------------------

class TestRunGuards:
    def test_dry_run_never_connects(self, tmp_path, mocker):
        mock_connect = mocker.patch("backfill_file_logs.connect")
        rc = bf.run(logs_root=tmp_path, dry_run=True, logger=mocker.MagicMock())
        assert rc == 0
        mock_connect.assert_not_called()

    def test_aborts_when_manual_rows_exist_without_force(self, tmp_path, mocker):
        fake_conn = mocker.MagicMock()
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=True)
        mock_get_max = mocker.patch("backfill_file_logs.get_max_non_manual_timestamp")

        rc = bf.run(logs_root=tmp_path, dsn="fake", force=False, logger=mocker.MagicMock())

        assert rc == 1
        mock_get_max.assert_not_called()
        fake_conn.close.assert_called_once()

    def test_force_proceeds_past_existing_manual_rows(self, tmp_path, mocker):
        fake_conn = mocker.MagicMock()
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=True)
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=None)

        rc = bf.run(logs_root=tmp_path, dsn="fake", force=True, skip_refresh=True,
                     logger=mocker.MagicMock())

        assert rc == 0

    def test_max_timestamp_override_skips_live_query(self, tmp_path, mocker):
        """
        --max-timestamp must bypass get_max_non_manual_timestamp() entirely
        (not just override its return value) -- the whole point is avoiding
        the live query when the live pipeline has already resumed inserting
        with newer-than-the-gap timestamps before the backfill runs.
        """
        fake_conn = mocker.MagicMock()
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        mock_get_max = mocker.patch("backfill_file_logs.get_max_non_manual_timestamp")

        rc = bf.run(logs_root=tmp_path, dsn="fake", skip_refresh=True,
                     max_timestamp_override=1735689650, logger=mocker.MagicMock())

        assert rc == 0
        mock_get_max.assert_not_called()

    def test_max_timestamp_override_used_as_guard_boundary(self, tmp_path, mocker):
        """
        End-to-end: a historical row older than "now" but newer than the
        override boundary must still be inserted, even though a live query
        (deliberately made to return a much larger, post-restart-like value)
        would have skipped it.
        """
        mock_execute_values = mocker.patch("backfill_file_logs.execute_values")

        fake_conn = mocker.MagicMock()
        fake_cursor = mocker.MagicMock()
        fake_cursor.fetchone.return_value = ("ok",)
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        # If this were ever called despite the override, its huge return
        # value would wrongly skip every row below -- proves the override
        # actually takes effect rather than merely being ignored.
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=9999999999)

        file_root = tmp_path / "file" / "acmecorp"
        file_root.mkdir(parents=True)
        (file_root / "2026-07-13.log").write_text(
            "1735689700 [critical] [system] monitor-01 [acmecorp]: gap row - would be skipped by live max\n"
        )

        rc = bf.run(logs_root=tmp_path, dsn="fake", since_days=90000, skip_refresh=True,
                     max_timestamp_override=1735689650, logger=mocker.MagicMock())

        assert rc == 0
        mock_execute_values.assert_called_once()

    def test_no_files_returns_zero_without_aborting(self, tmp_path, mocker):
        fake_conn = mocker.MagicMock()
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=None)

        rc = bf.run(logs_root=tmp_path, dsn="fake", logger=mocker.MagicMock())
        assert rc == 0

    def test_connect_failure_returns_one(self, tmp_path, mocker):
        mocker.patch("backfill_file_logs.connect", side_effect=RuntimeError("no db"))
        file_root = tmp_path / "file"
        file_root.mkdir()
        (file_root / "2026-07-13.log").write_text("1735689600 [critical] [system] s []: t - m\n")

        rc = bf.run(logs_root=tmp_path, dsn="fake", logger=mocker.MagicMock())
        assert rc == 1

    def test_refresh_alert_summary_called_by_default(self, tmp_path, mocker):
        fake_conn = mocker.MagicMock()
        fake_cursor = mocker.MagicMock()
        fake_cursor.fetchone.return_value = ("ok",)
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=None)

        rc = bf.run(logs_root=tmp_path, dsn="fake", skip_refresh=False, logger=mocker.MagicMock())

        assert rc == 0
        fake_cursor.execute.assert_any_call("SELECT refresh_alert_summary()")

    def test_skip_refresh_flag_honored(self, tmp_path, mocker):
        fake_conn = mocker.MagicMock()
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=None)

        rc = bf.run(logs_root=tmp_path, dsn="fake", skip_refresh=True, logger=mocker.MagicMock())

        assert rc == 0
        for call in fake_conn.cursor.return_value.__enter__.return_value.execute.call_args_list:
            assert "refresh_alert_summary" not in call.args[0]

    def test_end_to_end_run_inserts_only_rows_passing_both_guards(self, tmp_path, mocker):
        """
        Integration-style test through run(): a per-client file with one row
        older than the DB-max guard, one within it, filtered end to end
        through process_file's real (unmocked) logic, only insert_batch's
        eventual execute_values() call is mocked.
        """
        mock_execute_values = mocker.patch("backfill_file_logs.execute_values")

        fake_conn = mocker.MagicMock()
        fake_cursor = mocker.MagicMock()
        fake_cursor.fetchone.return_value = ("ok",)
        fake_conn.cursor.return_value.__enter__.return_value = fake_cursor
        mocker.patch("backfill_file_logs.connect", return_value=fake_conn)
        mocker.patch("backfill_file_logs.count_manual_rows", return_value=False)
        mocker.patch("backfill_file_logs.get_max_non_manual_timestamp", return_value=1735689650)

        file_root = tmp_path / "file" / "acmecorp"
        file_root.mkdir(parents=True)
        lines = [
            "1735689600 [critical] [system] monitor-01 [acmecorp]: too old - before db max",
            "1735689700 [critical] [system] monitor-01 [acmecorp]: fresh - after db max",
        ]
        (file_root / "2026-07-13.log").write_text("\n".join(lines) + "\n")

        rc = bf.run(logs_root=tmp_path, dsn="fake", since_days=90000, skip_refresh=True,
                     logger=mocker.MagicMock())

        assert rc == 0
        mock_execute_values.assert_called_once()
        _, call_args, _ = mock_execute_values.mock_calls[0]
        _, _, values_arg = call_args
        assert len(values_arg) == 1
        assert values_arg[0][3] == "fresh"  # title column
        assert values_arg[0][6] == "acmecorp"  # client column, folder-name-wins
