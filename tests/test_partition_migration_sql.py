"""
Static sanity checks for sql/migrations/0001_partition_management.sql.

There is no live Postgres available in this environment (and the project
rule is that docker/docker-compose are never run from automation), so this
migration's SQL cannot actually be executed here. These tests do NOT
validate that the SQL is semantically correct against a real database --
only that the file is structurally sane: the function signature matches the
spec, the naming/idempotency building blocks match the existing reference
SQL in sql/init/, the dollar-quoted function body is balanced, and the file
ends with the exact required repair call.

This migration MUST be verified against a scratch Postgres instance before
this branch is deployed to prod.
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "sql" / "migrations" / "0001_partition_management.sql"
REFERENCE_PARTITIONS_SQL = REPO_ROOT / "sql" / "init" / "02-partitions.sql"
REFERENCE_FUNCTIONS_SQL = REPO_ROOT / "sql" / "init" / "03-functions.sql"

sys.path.insert(0, str(REPO_ROOT / "src"))
import db_migrate  # noqa: E402


@pytest.fixture(scope="module")
def sql_text() -> str:
    return MIGRATION_PATH.read_text()


# ---------------------------------------------------------------------------
# File presence / migration-runner discoverability
# ---------------------------------------------------------------------------

def test_migration_file_exists():
    assert MIGRATION_PATH.is_file()


def test_filename_matches_db_migrate_naming_convention():
    match = db_migrate.MIGRATION_FILENAME_RE.match(MIGRATION_PATH.name)
    assert match is not None
    assert int(match.group(1)) == 1


def test_db_migrate_discovers_it_as_version_1():
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    versions = {m.version: m.name for m in migrations}
    assert versions.get(1) == "0001_partition_management.sql"


def test_gitkeep_removed_now_that_a_real_migration_exists():
    # Task 1 added an empty-dir placeholder; once a real migration file
    # exists it's redundant.
    assert not (MIGRATION_PATH.parent / ".gitkeep").exists()


# ---------------------------------------------------------------------------
# Function signature matches the spec exactly
# ---------------------------------------------------------------------------

def test_create_or_replace_function_signature(sql_text):
    assert "CREATE OR REPLACE FUNCTION ensure_alert_partitions" in sql_text

    # p_from DATE, p_weeks_ahead INT DEFAULT 8 -- allow flexible whitespace
    # but require the exact parameter names, types, and default.
    sig_re = re.compile(
        r"CREATE\s+OR\s+REPLACE\s+FUNCTION\s+ensure_alert_partitions\s*\(\s*"
        r"p_from\s+DATE\s*,\s*"
        r"p_weeks_ahead\s+INT\s+DEFAULT\s+8\s*\)",
        re.IGNORECASE,
    )
    assert sig_re.search(sql_text), "function signature does not match p_from DATE, p_weeks_ahead INT DEFAULT 8"


def test_return_type_matches_spec(sql_text):
    return_re = re.compile(
        r"RETURNS\s+TABLE\s*\(\s*partition_name\s+text\s*,\s*status\s+text\s*\)",
        re.IGNORECASE,
    )
    assert return_re.search(sql_text), "RETURNS TABLE(partition_name text, status text) not found"


def test_uses_plpgsql(sql_text):
    assert re.search(r"\$\$\s*LANGUAGE\s+plpgsql", sql_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Closing repair call: exact required arguments, and it's the final statement
# ---------------------------------------------------------------------------

def test_ends_with_exact_repair_call(sql_text):
    stripped = sql_text.rstrip()
    assert stripped.endswith("SELECT * FROM ensure_alert_partitions(DATE '2026-01-05', 8);"), (
        "file must end with the exact repair call "
        "SELECT * FROM ensure_alert_partitions(DATE '2026-01-05', 8);"
    )


def test_repair_call_appears_exactly_once(sql_text):
    calls = re.findall(r"SELECT \* FROM ensure_alert_partitions\(", sql_text)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Naming / boundary building blocks match sql/init/02-partitions.sql exactly
# ---------------------------------------------------------------------------

def test_reference_files_readable():
    # Sanity: the reference files this migration is supposed to stay
    # consistent with actually exist and are non-empty.
    assert REFERENCE_PARTITIONS_SQL.is_file()
    assert REFERENCE_FUNCTIONS_SQL.is_file()
    assert REFERENCE_PARTITIONS_SQL.stat().st_size > 0
    assert REFERENCE_FUNCTIONS_SQL.stat().st_size > 0


def test_partition_naming_uses_same_building_blocks_as_reference(sql_text):
    reference_text = REFERENCE_PARTITIONS_SQL.read_text() + REFERENCE_FUNCTIONS_SQL.read_text()

    # The exact naming expression from the Phase 0 block / 02-partitions.sql:
    # 'alerts_' || EXTRACT(ISOYEAR FROM ...) || '_w' || LPAD(EXTRACT(WEEK FROM ...)::TEXT, 2, '0')
    for fragment in ("EXTRACT(ISOYEAR FROM", "EXTRACT(WEEK FROM", "LPAD(", "2, '0'", "'alerts_'", "'_w'"):
        assert fragment in sql_text, f"missing naming fragment {fragment!r} in migration"
        assert fragment in reference_text, f"reference files no longer contain {fragment!r} (test itself stale?)"


def test_partition_table_name_prefix_matches_pg_tables_convention(sql_text):
    # Idempotency check must query pg_tables in the public schema, same as
    # both reference files.
    assert "pg_tables" in sql_text
    assert re.search(r"schemaname\s*=\s*'public'", sql_text)


def test_week_boundary_uses_date_trunc_week(sql_text):
    # Same Monday-anchored boundary as the Phase 0 block / reference files:
    # DATE_TRUNC('week', ...).
    assert re.search(r"DATE_TRUNC\(\s*'week'", sql_text, re.IGNORECASE)


def test_partition_upper_bound_is_seven_days_after_start(sql_text):
    # FOR VALUES FROM (start) TO (start + 7), same as the Phase 0 block.
    assert re.search(r"FOR VALUES FROM\s*\(\s*%L\s*\)\s*TO\s*\(\s*%L\s*\)", sql_text)
    assert re.search(r"v_week\s*\+\s*7", sql_text)


# ---------------------------------------------------------------------------
# Idempotency / error-handling design intent
# ---------------------------------------------------------------------------

def test_checks_existence_before_creating(sql_text):
    assert re.search(r"IF\s+EXISTS\s*\(", sql_text, re.IGNORECASE)
    assert "already_exists" in sql_text
    assert "'created'" in sql_text


def test_does_not_swallow_errors_with_exception_handler(sql_text):
    # Unlike sql/init/03-functions.sql's create_alert_partitions (which
    # catches EXCEPTION WHEN OTHERS and downgrades errors to a status row),
    # this function must let real errors propagate so the migration
    # runner's transaction rolls back loudly instead of silently reporting
    # 'error: ...' as if it were a normal result row.
    assert "EXCEPTION WHEN OTHERS" not in sql_text


def test_validates_null_inputs_rather_than_silently_no_opping(sql_text):
    # DATE_TRUNC('week', NULL) is NULL, and `NULL <= v_end` is NULL (falsy),
    # so an un-validated NULL p_from would silently produce zero rows
    # instead of surfacing a caller bug. Require explicit validation.
    assert re.search(r"p_from\s+IS\s+NULL", sql_text, re.IGNORECASE)
    assert re.search(r"RAISE\s+EXCEPTION", sql_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Coarse structural sanity: balanced dollar-quoting and BEGIN/END
# ---------------------------------------------------------------------------

def test_dollar_quoting_is_balanced(sql_text):
    # Every function body is opened and closed with a bare $$ tag pair.
    assert sql_text.count("$$") % 2 == 0
    assert sql_text.count("$$") >= 2


def test_begin_end_blocks_balanced(sql_text):
    begins = len(re.findall(r"\bBEGIN\b", sql_text, re.IGNORECASE))
    ends = len(re.findall(r"\bEND\s*(?:\$\$|;|IF|LOOP)?", sql_text, re.IGNORECASE))
    # Every BEGIN needs a matching END (function body, IF, LOOP all close
    # with some form of END). Not a real parser -- just a smoke check that
    # counts aren't wildly mismatched (e.g. a missing END entirely).
    assert ends >= begins > 0


def test_no_semicolon_inside_dynamic_execute_format_string(sql_text):
    # A stray semicolon inside the format() string passed to EXECUTE would
    # break the dynamic CREATE TABLE statement. Spot-check the one dynamic
    # SQL string in this file.
    match = re.search(r"EXECUTE\s+format\(\s*'([^']*)'", sql_text, re.DOTALL)
    assert match, "expected exactly one EXECUTE format(...) dynamic statement"
    assert ";" not in match.group(1)


def test_grants_execute_to_alertmanager_role(sql_text):
    # Matches the GRANT EXECUTE convention used for every function in
    # sql/init/03-functions.sql.
    assert re.search(
        r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+ensure_alert_partitions\([^)]*\)\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )
