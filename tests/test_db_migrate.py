"""
Tests for src/db_migrate.py

Pure-logic tests (filename discovery/sorting, DSN construction, mocked
transaction semantics) run unconditionally. A single end-to-end test against
a real Postgres is gated on AM_TEST_DSN and skips cleanly when it isn't set
(this repo's later pytest scaffold task establishes that env var convention;
this file anticipates it without building any shared fixture infrastructure).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import db_migrate  # noqa: E402


def _write(dir_path: Path, filename: str, contents: str = "SELECT 1;") -> None:
    (dir_path / filename).write_text(contents)


# ---------------------------------------------------------------------------
# discover_migrations: pure filesystem logic, no DB needed
# ---------------------------------------------------------------------------

def test_discover_migrations_sorts_by_numeric_version(tmp_path):
    _write(tmp_path, "0002_second.sql")
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0010_tenth.sql")

    migrations = db_migrate.discover_migrations(tmp_path)

    assert [m.name for m in migrations] == [
        "0001_first.sql",
        "0002_second.sql",
        "0010_tenth.sql",
    ]
    assert [m.version for m in migrations] == [1, 2, 10]


def test_discover_migrations_sorts_numerically_not_lexicographically(tmp_path):
    # A lexicographic string sort would put "10_x.sql" before "2_x.sql"
    # ('_' > '0' in ASCII); numeric sort on the parsed version must not.
    _write(tmp_path, "2_second.sql")
    _write(tmp_path, "10_tenth.sql")

    migrations = db_migrate.discover_migrations(tmp_path)

    assert [m.version for m in migrations] == [2, 10]


def test_discover_migrations_empty_dir_returns_empty_list(tmp_path):
    assert db_migrate.discover_migrations(tmp_path) == []


def test_discover_migrations_rejects_malformed_filename(tmp_path):
    _write(tmp_path, "not_a_migration.sql")

    with pytest.raises(ValueError, match="does not match"):
        db_migrate.discover_migrations(tmp_path)


def test_discover_migrations_rejects_duplicate_version(tmp_path):
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0001_also_first.sql")

    with pytest.raises(ValueError, match="Duplicate migration version"):
        db_migrate.discover_migrations(tmp_path)


def test_discover_migrations_missing_dir_raises(tmp_path):
    missing = tmp_path / "does_not_exist"

    with pytest.raises(FileNotFoundError):
        db_migrate.discover_migrations(missing)


def test_discover_migrations_ignores_non_sql_files(tmp_path):
    _write(tmp_path, "0001_first.sql")
    (tmp_path / "README.md").write_text("not a migration")

    migrations = db_migrate.discover_migrations(tmp_path)

    assert [m.name for m in migrations] == ["0001_first.sql"]


# ---------------------------------------------------------------------------
# DSN / connection kwargs construction from POSTGRES_* env vars
# ---------------------------------------------------------------------------

def test_connection_kwargs_use_postgres_env_vars(monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "dbhost")
    monkeypatch.setenv("POSTGRES_PORT", "5555")
    monkeypatch.setenv("POSTGRES_DB", "mydb")
    monkeypatch.setenv("POSTGRES_USER", "myuser")
    monkeypatch.setenv("POSTGRES_PASSWORD", "mypass")

    kwargs = db_migrate.get_connection_kwargs()

    assert kwargs == {
        "host": "dbhost",
        "port": 5555,
        "database": "mydb",
        "user": "myuser",
        "password": "mypass",
    }


def test_connection_kwargs_defaults_match_database_logger(monkeypatch):
    for var in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    kwargs = db_migrate.get_connection_kwargs()

    # Must match src/database_logger.py's _create_db_pool defaults exactly.
    assert kwargs == {
        "host": "localhost",
        "port": 5432,
        "database": "alertmanager",
        "user": "alertmanager",
        "password": "",
    }


def test_connect_uses_dsn_string_when_given(mocker):
    mock_connect = mocker.patch("db_migrate.psycopg2.connect")

    db_migrate.connect(dsn="postgresql://u:p@h:5432/d")

    mock_connect.assert_called_once_with("postgresql://u:p@h:5432/d")


def test_connect_uses_env_kwargs_when_no_dsn(mocker, monkeypatch):
    monkeypatch.setenv("POSTGRES_HOST", "dbhost")
    mock_connect = mocker.patch("db_migrate.psycopg2.connect")

    db_migrate.connect(dsn=None)

    _, kwargs = mock_connect.call_args
    assert kwargs["host"] == "dbhost"


# ---------------------------------------------------------------------------
# apply_migration transaction semantics (mocked connection, no DB)
# ---------------------------------------------------------------------------

def test_apply_migration_commits_sql_and_bookkeeping_row(mocker, tmp_path):
    _write(tmp_path, "0001_demo.sql", "CREATE TABLE demo (id INT);")
    migration = db_migrate.Migration(version=1, name="0001_demo.sql", path=tmp_path / "0001_demo.sql")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value

    db_migrate.apply_migration(conn, migration)

    executed_sql = [call.args[0] for call in cursor.execute.call_args_list]
    assert "CREATE TABLE demo (id INT);" in executed_sql
    assert any("INSERT INTO schema_migrations" in sql for sql in executed_sql)
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_apply_migration_rolls_back_and_reraises_on_error(mocker, tmp_path):
    _write(tmp_path, "0001_broken.sql", "NOT VALID SQL;")
    migration = db_migrate.Migration(version=1, name="0001_broken.sql", path=tmp_path / "0001_broken.sql")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.execute.side_effect = Exception("boom")

    with pytest.raises(Exception, match="boom"):
        db_migrate.apply_migration(conn, migration)

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# run()/main() orchestration with a mocked connection (no live DB)
# ---------------------------------------------------------------------------

def test_run_dry_run_lists_pending_without_applying(mocker, tmp_path, capsys):
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0002_second.sql")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(1,)]  # version 1 already applied

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=True)

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "0002_second.sql" in out
    assert "0001_first.sql" not in out  # already applied, must not be listed as pending
    conn.commit.assert_not_called()


def test_run_dry_run_reports_up_to_date(mocker, tmp_path, capsys):
    _write(tmp_path, "0001_first.sql")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(1,)]

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=True)

    assert exit_code == 0
    assert "Up to date" in capsys.readouterr().out


def test_run_applies_pending_migrations_in_order(mocker, tmp_path):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE b (id INT);")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []  # nothing applied yet

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 0
    executed_sql = " ".join(call.args[0] for call in cursor.execute.call_args_list if call.args)
    assert "CREATE TABLE a" in executed_sql
    assert "CREATE TABLE b" in executed_sql
    assert executed_sql.index("CREATE TABLE a") < executed_sql.index("CREATE TABLE b")


def test_run_stops_on_first_failure_and_exits_1(mocker, tmp_path):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE b (id INT);")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    def execute_side_effect(sql, *args, **kwargs):
        if "CREATE TABLE a" in sql:
            raise Exception("simulated failure")

    cursor.execute.side_effect = execute_side_effect

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 1
    executed_sql = [call.args[0] for call in cursor.execute.call_args_list if call.args]
    assert not any("CREATE TABLE b" in sql for sql in executed_sql)


def test_run_first_migration_persists_when_second_fails(mocker, tmp_path):
    # Proves, end-to-end via run(), that migration 1's own transaction
    # commits and its schema_migrations row persists even though the batch
    # as a whole fails on migration 2 -- not just that isolated commit()/
    # rollback() mocks were called somewhere.
    _write(tmp_path, "0001_first.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE b (id INT);")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    def execute_side_effect(sql, *args, **kwargs):
        if "CREATE TABLE b" in sql:
            raise Exception("simulated failure")

    cursor.execute.side_effect = execute_side_effect

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 1
    assert conn.commit.call_count == 1  # migration 1's transaction committed
    assert conn.rollback.call_count == 1  # migration 2's transaction rolled back

    inserted_versions = [
        call.args[1][0]
        for call in cursor.execute.call_args_list
        if call.args and "INSERT INTO schema_migrations" in call.args[0]
    ]
    assert inserted_versions == [1]  # only migration 1's bookkeeping row was ever inserted


def test_run_acquires_and_releases_advisory_lock_on_success(mocker, tmp_path):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE a (id INT);")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 0

    call_args_list = cursor.execute.call_args_list
    lock_calls = [c for c in call_args_list if c.args and "pg_advisory_lock(" in c.args[0]]
    unlock_calls = [c for c in call_args_list if c.args and "pg_advisory_unlock(" in c.args[0]]

    assert len(lock_calls) == 1
    assert lock_calls[0].args[1] == (db_migrate.ADVISORY_LOCK_KEY,)
    assert len(unlock_calls) == 1
    assert unlock_calls[0].args[1] == (db_migrate.ADVISORY_LOCK_KEY,)

    # Lock must be acquired before it is released.
    assert call_args_list.index(lock_calls[0]) < call_args_list.index(unlock_calls[0])


def test_run_releases_advisory_lock_even_when_migration_fails(mocker, tmp_path):
    _write(tmp_path, "0001_first.sql", "CREATE TABLE a (id INT);")
    _write(tmp_path, "0002_second.sql", "CREATE TABLE b (id INT);")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = []

    def execute_side_effect(sql, *args, **kwargs):
        if "CREATE TABLE a" in sql:
            raise Exception("simulated failure")

    cursor.execute.side_effect = execute_side_effect

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 1

    call_args_list = cursor.execute.call_args_list
    lock_calls = [c for c in call_args_list if c.args and "pg_advisory_lock(" in c.args[0]]
    unlock_calls = [c for c in call_args_list if c.args and "pg_advisory_unlock(" in c.args[0]]

    assert len(lock_calls) == 1
    # Released via the `finally` even though the apply loop raised partway through.
    assert len(unlock_calls) == 1
    assert unlock_calls[0].args[1] == (db_migrate.ADVISORY_LOCK_KEY,)
    conn.close.assert_called_once()  # connection still cleaned up on the failure path


def test_run_malformed_migration_filename_fails_before_connecting(mocker, tmp_path):
    _write(tmp_path, "bad_name.sql")

    mock_connect = mocker.patch("db_migrate.connect")

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 1
    mock_connect.assert_not_called()


def test_run_no_pending_migrations_is_a_success_noop(mocker, tmp_path):
    _write(tmp_path, "0001_first.sql")

    conn = mocker.MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = [(1,)]

    mocker.patch("db_migrate.connect", return_value=conn)

    exit_code = db_migrate.run(tmp_path, dry_run=False)

    assert exit_code == 0
    conn.commit.assert_not_called()


def test_main_parses_dry_run_and_dsn_flags(mocker):
    mock_run = mocker.patch("db_migrate.run", return_value=0)

    exit_code = db_migrate.main(["--dry-run", "--dsn", "postgresql://x"])

    assert exit_code == 0
    _, kwargs = mock_run.call_args
    assert kwargs["dry_run"] is True
    assert kwargs["dsn"] == "postgresql://x"


def test_main_defaults_dry_run_false_and_dsn_none(mocker):
    mock_run = mocker.patch("db_migrate.run", return_value=0)

    db_migrate.main([])

    _, kwargs = mock_run.call_args
    assert kwargs["dry_run"] is False
    assert kwargs["dsn"] is None


# ---------------------------------------------------------------------------
# Live-DB integration test, gated on AM_TEST_DSN (skipped without a real DB)
# ---------------------------------------------------------------------------

AM_TEST_DSN = os.environ.get("AM_TEST_DSN")


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_run_end_to_end_against_real_postgres(tmp_path):
    import psycopg2

    _write(tmp_path, "0001_create_widgets.sql", "CREATE TABLE widgets (id SERIAL PRIMARY KEY);")

    conn = psycopg2.connect(AM_TEST_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS widgets")
            cur.execute("DROP TABLE IF EXISTS schema_migrations")
        conn.commit()
    finally:
        conn.close()

    exit_code = db_migrate.run(tmp_path, dsn=AM_TEST_DSN, dry_run=False)
    assert exit_code == 0

    # Re-running must be an idempotent no-op, not a re-apply or an error.
    exit_code_again = db_migrate.run(tmp_path, dsn=AM_TEST_DSN, dry_run=False)
    assert exit_code_again == 0

    conn = psycopg2.connect(AM_TEST_DSN)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version, name FROM schema_migrations")
            assert cur.fetchall() == [(1, "0001_create_widgets.sql")]

            cur.execute("SELECT to_regclass('widgets')")
            assert cur.fetchone()[0] == "widgets"

            cur.execute("DROP TABLE widgets")
            cur.execute("DROP TABLE schema_migrations")
        conn.commit()
    finally:
        conn.close()
