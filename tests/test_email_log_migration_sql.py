"""
Static sanity checks for sql/migrations/0003_email_log.sql.

There is no live Postgres available in this environment (and the project
rule is that docker/docker-compose are never run from automation), so this
migration's SQL cannot actually be executed here. These tests do NOT
validate that the SQL is semantically correct against a real database --
only that the file is structurally sane: the table/column/type list matches
the brief exactly, the CHECK constraint values match, the index exists, and
the file follows the naming/idempotency/grant conventions established by
sql/init/01-schema.sql and sql/migrations/0001_partition_management.sql.

This migration MUST be verified against a scratch Postgres instance before
this branch is deployed to prod (CREATE TABLE syntax errors, type name
typos, etc. cannot be caught by regex matching alone).
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "sql" / "migrations" / "0003_email_log.sql"
REFERENCE_SCHEMA_SQL = REPO_ROOT / "sql" / "init" / "01-schema.sql"
PRIOR_MIGRATION_SQL = REPO_ROOT / "sql" / "migrations" / "0001_partition_management.sql"

sys.path.insert(0, str(REPO_ROOT / "src"))
import db_migrate  # noqa: E402


@pytest.fixture(scope="module")
def sql_text() -> str:
    return MIGRATION_PATH.read_text()


# ---------------------------------------------------------------------------
# File presence / migration-runner discoverability / numbering discipline
# ---------------------------------------------------------------------------

def test_migration_file_exists():
    assert MIGRATION_PATH.is_file()


def test_filename_matches_db_migrate_naming_convention():
    match = db_migrate.MIGRATION_FILENAME_RE.match(MIGRATION_PATH.name)
    assert match is not None
    assert int(match.group(1)) == 3


def test_db_migrate_discovers_it_as_version_3():
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    versions = {m.version: m.name for m in migrations}
    assert versions.get(3) == "0003_email_log.sql"


def test_0002_is_not_reused_by_this_migration():
    # 0002 was reserved for the Phase 4 identity schema migration landing
    # later on this branch (Task 14: sql/migrations/0002_identity.sql) --
    # this migration (0003_email_log.sql) must not claim that number
    # itself. Task 14 has since landed, so 0002_identity.sql legitimately
    # exists now; what this test actually guards is that 0003 still claims
    # version 3, not that 0002 is absent.
    assert (MIGRATION_PATH.parent / "0002_identity.sql").exists()
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    versions = {m.version: m.name for m in migrations}
    assert versions.get(2) == "0002_identity.sql"
    assert versions.get(3) == "0003_email_log.sql"


def test_reference_files_readable():
    assert REFERENCE_SCHEMA_SQL.is_file()
    assert REFERENCE_SCHEMA_SQL.stat().st_size > 0
    assert PRIOR_MIGRATION_SQL.is_file()
    assert PRIOR_MIGRATION_SQL.stat().st_size > 0


# ---------------------------------------------------------------------------
# Table + column list matches the brief exactly (name, type, order)
# ---------------------------------------------------------------------------

def test_creates_email_log_table_idempotently(sql_text):
    # IF NOT EXISTS matches the idempotent CREATE TABLE convention used
    # throughout sql/init/01-schema.sql.
    assert re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+email_log\s*\(", sql_text, re.IGNORECASE)


def _extract_create_table_body(sql_text: str) -> str:
    """Pull the column-definition block out of the CREATE TABLE statement."""
    start_match = re.search(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+email_log\s*\(", sql_text, re.IGNORECASE)
    assert start_match, "CREATE TABLE IF NOT EXISTS email_log ( not found"
    start = start_match.end()

    # Walk forward tracking paren depth to find the matching close paren --
    # simple but sufficient since there is no nested parenthesised
    # expression that itself contains unbalanced parens in this file.
    depth = 1
    i = start
    while depth > 0:
        if sql_text[i] == "(":
            depth += 1
        elif sql_text[i] == ")":
            depth -= 1
        i += 1
    return sql_text[start : i - 1]


@pytest.fixture(scope="module")
def create_table_body(sql_text) -> str:
    return _extract_create_table_body(sql_text)


def test_create_table_parens_are_balanced(sql_text):
    # Coarse structural sanity: the CREATE TABLE statement's parens close
    # somewhere in the file (extraction above would raise/IndexError
    # otherwise -- this test makes that failure mode explicit).
    body = _extract_create_table_body(sql_text)
    assert body.count("(") == body.count(")")


# Exact columns from the Task 8 brief, in brief order: (name, type-pattern).
EXPECTED_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("attempted_at", r"BIGINT\s+NOT\s+NULL"),
    ("pathway", r"VARCHAR\(255\)\s+NOT\s+NULL"),
    ("aggregated", r"BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+FALSE"),
    ("alert_count", r"INTEGER\s+NOT\s+NULL\s+DEFAULT\s+1"),
    ("level", r"VARCHAR\(20\)"),
    ("clients", r"TEXT\[\]"),
    ("recipients", r"TEXT\[\]"),
    ("subject", r"TEXT"),
    ("status", r"VARCHAR\(20\)\s+NOT\s+NULL"),
    ("http_status", r"INTEGER"),
    ("error", r"VARCHAR\(2000\)"),
    ("graph_request_id", r"VARCHAR\(255\)"),
    ("duration_ms", r"INTEGER"),
    ("retries", r"INTEGER\s+NOT\s+NULL\s+DEFAULT\s+0"),
]


@pytest.mark.parametrize("column_name,type_pattern", EXPECTED_COLUMNS)
def test_column_present_with_expected_type(create_table_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}",
        re.IGNORECASE,
    )
    assert col_re.search(create_table_body), (
        f"column {column_name!r} not found with expected type pattern {type_pattern!r}"
    )


def test_columns_appear_in_brief_order(create_table_body):
    positions = []
    for column_name, _ in EXPECTED_COLUMNS:
        # Match the column name at the start of a definition line/segment
        # (preceded by start-of-body, a comma, or whitespace/newline after a
        # comma) to avoid matching the name inside a comment.
        match = re.search(rf"(?:,|\A)\s*(?:--[^\n]*\n\s*)*{re.escape(column_name)}\s", create_table_body)
        assert match, f"could not locate definition start for column {column_name!r}"
        positions.append((column_name, match.start()))

    ordered = sorted(positions, key=lambda p: p[1])
    assert [name for name, _ in ordered] == [name for name, _ in positions], (
        "columns are not declared in the same order as the Task 8 brief"
    )


def test_all_and_only_expected_columns_present(create_table_body):
    # Column names are whatever precedes the first whitespace-then-type-word
    # on each comma-separated top-level segment. This is a coarse but
    # effective check that nothing was added or dropped relative to the
    # brief's column list.
    expected_names = {name for name, _ in EXPECTED_COLUMNS}
    found_names = set(re.findall(r"(?:^|,)\s*(?:--[^\n]*\n\s*)*([a-z_]+)\s+[A-Z]", create_table_body, re.MULTILINE))
    assert found_names == expected_names, f"column set mismatch: {found_names.symmetric_difference(expected_names)}"


# ---------------------------------------------------------------------------
# status CHECK constraint: idempotent DROP/ADD pattern + exact values
# ---------------------------------------------------------------------------

def test_status_check_constraint_values(sql_text):
    assert re.search(
        r"CHECK\s*\(\s*status\s+IN\s*\(\s*'accepted'\s*,\s*'failed'\s*\)\s*\)",
        sql_text,
        re.IGNORECASE,
    ), "status CHECK constraint must be CHECK (status IN ('accepted', 'failed'))"


def test_status_check_constraint_uses_idempotent_drop_add_pattern(sql_text):
    # Same convention as chk_level / chk_category / chk_ingestion_source in
    # sql/init/01-schema.sql: DROP CONSTRAINT IF EXISTS <name> then ADD
    # CONSTRAINT <name> CHECK (...), so re-running this file is a no-op.
    assert re.search(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_email_log_status", sql_text, re.IGNORECASE)
    assert re.search(r"ADD\s+CONSTRAINT\s+chk_email_log_status\s+CHECK", sql_text, re.IGNORECASE)


def test_reference_schema_uses_same_constraint_idiom():
    # Confirms the idiom this migration mirrors actually exists in
    # sql/init/01-schema.sql (guards against the reference convention
    # itself drifting and this test going stale/misleading).
    reference_text = REFERENCE_SCHEMA_SQL.read_text()
    assert "DROP CONSTRAINT IF EXISTS chk_level" in reference_text
    assert "ADD CONSTRAINT chk_level CHECK" in reference_text


def test_no_other_status_values_permitted(sql_text):
    # Spot-check nothing sneaks a third status value into the constraint
    # (e.g. 'pending', 'retrying').
    match = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", sql_text, re.IGNORECASE)
    assert match
    values = [v.strip() for v in match.group(1).split(",")]
    assert values == ["'accepted'", "'failed'"]


# ---------------------------------------------------------------------------
# Index on attempted_at DESC
# ---------------------------------------------------------------------------

def test_index_on_attempted_at_desc(sql_text):
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_email_log_attempted_at\s+ON\s+email_log\s*\(\s*attempted_at\s+DESC\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_index_naming_matches_alerts_convention():
    # sql/init/01-schema.sql names its timestamp index idx_alerts_timestamp
    # (idx_<table>_<column>) with the same ON (<column> DESC) shape this
    # migration's idx_email_log_attempted_at mirrors.
    reference_text = REFERENCE_SCHEMA_SQL.read_text()
    assert re.search(r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_alerts_timestamp\s+ON\s+alerts\s*\(\s*timestamp\s+DESC\s*\)", reference_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# Grants: this table/sequence needs an explicit grant (created after
# 01-schema.sql's blanket GRANT ALL ran)
# ---------------------------------------------------------------------------

def test_grants_table_privileges_to_alertmanager(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+email_log\s+TO\s+alertmanager", sql_text, re.IGNORECASE)


def test_grants_sequence_privileges_to_alertmanager(sql_text):
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+email_log_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


def test_grant_idiom_mirrors_prior_migration():
    # sql/migrations/0001_partition_management.sql establishes the pattern
    # of explicitly granting to alertmanager for objects a migration adds
    # (rather than assuming the original blanket grant covers them).
    prior_text = PRIOR_MIGRATION_SQL.read_text()
    assert re.search(r"GRANT\s+EXECUTE\s+ON\s+FUNCTION\s+ensure_alert_partitions.*TO\s+alertmanager", prior_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# No accidental partitioning / TIMESTAMPTZ drift
# ---------------------------------------------------------------------------

def test_not_partitioned(sql_text):
    # Brief is explicit: single unpartitioned table (volume is trivial).
    assert "PARTITION BY" not in sql_text.upper()


def test_attempted_at_is_bigint_not_timestamptz(create_table_body):
    # Check the actual column definitions, not the file's prose -- both the
    # header comment and several inline column comments legitimately
    # mention TIMESTAMPTZ to explain why it was NOT chosen here, unlike
    # alerts.timestamp. Strip `--` line comments before checking.
    code_only = re.sub(r"--[^\n]*", "", create_table_body)
    assert re.search(r"attempted_at\s+BIGINT", code_only, re.IGNORECASE)
    assert "TIMESTAMPTZ" not in code_only.upper()
