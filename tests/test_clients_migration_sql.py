"""
Static sanity checks for sql/migrations/0004_clients.sql.

There is no live Postgres available in this environment (and the project
rule is that docker/docker-compose are never run from automation), so this
migration's SQL cannot actually be executed here. These tests do NOT
validate that the SQL is semantically correct against a real database --
only that the file is structurally sane: the table/columns/constraints/
grants are present with the expected type/shape, and the idempotency/grant
conventions match sql/init/01-schema.sql and the prior migrations.

This migration MUST be verified against a scratch Postgres instance before
this branch is deployed to prod (CREATE TABLE syntax errors, type name
typos, etc. cannot be caught by regex matching alone).
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "sql" / "migrations" / "0004_clients.sql"
PRIOR_MIGRATION_SQL = REPO_ROOT / "sql" / "migrations" / "0003_email_log.sql"

sys.path.insert(0, str(REPO_ROOT / "src"))
import db_migrate  # noqa: E402


@pytest.fixture(scope="module")
def sql_text() -> str:
    return MIGRATION_PATH.read_text()


def _extract_create_table_body(sql_text: str, table: str) -> str:
    """Pull the column-definition block out of a CREATE TABLE statement."""
    start_match = re.search(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\(", sql_text, re.IGNORECASE
    )
    assert start_match, f"CREATE TABLE IF NOT EXISTS {table} ( not found"
    start = start_match.end()

    depth = 1
    i = start
    while depth > 0:
        if sql_text[i] == "(":
            depth += 1
        elif sql_text[i] == ")":
            depth -= 1
        i += 1
    return sql_text[start : i - 1]


# ---------------------------------------------------------------------------
# File presence / migration-runner discoverability / numbering discipline
# ---------------------------------------------------------------------------

def test_migration_file_exists():
    assert MIGRATION_PATH.is_file()


def test_filename_matches_db_migrate_naming_convention():
    match = db_migrate.MIGRATION_FILENAME_RE.match(MIGRATION_PATH.name)
    assert match is not None
    assert int(match.group(1)) == 4


def test_db_migrate_discovers_it_as_version_4():
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    versions = {m.version: m.name for m in migrations}
    assert versions.get(4) == "0004_clients.sql"


def test_all_four_migrations_coexist_in_sorted_order():
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    names_in_order = [m.name for m in migrations]
    assert names_in_order == [
        "0001_partition_management.sql",
        "0002_identity.sql",
        "0003_email_log.sql",
        "0004_clients.sql",
    ]


def test_prior_migration_readable():
    assert PRIOR_MIGRATION_SQL.is_file()
    assert PRIOR_MIGRATION_SQL.stat().st_size > 0


def test_does_not_reference_other_tables(sql_text):
    # This migration must not depend on anything another migration creates
    # -- order-independence at apply time, same discipline 0002/0003 hold
    # each other to. The header comment legitimately *names* alerts/
    # role_scopes while explaining the app-layer-only design decision, so
    # strip `--` line comments before checking actual statements.
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    for table in ("alerts", "role_scopes", "users", "roles", "user_roles", "auth_tokens", "auth_audit", "email_log"):
        assert table not in code_only, f"0004_clients.sql must not reference {table!r} in real SQL"


# ---------------------------------------------------------------------------
# Table creation is idempotent (IF NOT EXISTS), matching project convention
# ---------------------------------------------------------------------------

def test_creates_clients_table_idempotently(sql_text):
    assert re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+clients\s*\(", sql_text, re.IGNORECASE
    )


def test_create_table_parens_are_balanced(sql_text):
    body = _extract_create_table_body(sql_text, "clients")
    assert body.count("(") == body.count(")")


# ---------------------------------------------------------------------------
# clients columns
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def clients_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "clients")


CLIENTS_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("name", r"VARCHAR\(255\)\s+NOT\s+NULL\s+UNIQUE"),
    ("description", r"TEXT"),
    ("status", r"VARCHAR\(20\)\s+NOT\s+NULL\s+DEFAULT\s+'active'"),
    ("created_at", r"BIGINT\s+NOT\s+NULL"),
    ("updated_at", r"BIGINT\s+NOT\s+NULL"),
]


@pytest.mark.parametrize("column_name,type_pattern", CLIENTS_COLUMNS)
def test_clients_column_present_with_expected_type(clients_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}", re.IGNORECASE
    )
    assert col_re.search(clients_body), f"clients.{column_name} not found with type {type_pattern!r}"


def test_clients_columns_appear_in_declared_order(clients_body):
    positions = []
    for column_name, _ in CLIENTS_COLUMNS:
        match = re.search(
            rf"(?:,|\A)\s*(?:--[^\n]*\n\s*)*{re.escape(column_name)}\s", clients_body
        )
        assert match, f"could not locate definition start for clients.{column_name}"
        positions.append((column_name, match.start()))
    ordered = sorted(positions, key=lambda p: p[1])
    assert [n for n, _ in ordered] == [n for n, _ in positions]


def test_clients_status_check_constraint_values(sql_text):
    match = re.search(
        r"chk_clients_status\s+CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", sql_text, re.IGNORECASE
    )
    assert match, "chk_clients_status CHECK constraint not found"
    values = [v.strip() for v in match.group(1).split(",")]
    assert values == ["'active'", "'disabled'"]


def test_clients_name_lowercase_check_constraint(sql_text):
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_clients_name_lowercase", sql_text, re.IGNORECASE
    )
    assert re.search(
        r"ADD\s+CONSTRAINT\s+chk_clients_name_lowercase\s+CHECK\s*\(\s*name\s*=\s*lower\(name\)\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_clients_status_check_uses_idempotent_drop_add_pattern(sql_text):
    assert re.search(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_clients_status", sql_text, re.IGNORECASE)
    assert re.search(r"ADD\s+CONSTRAINT\s+chk_clients_status\s+CHECK", sql_text, re.IGNORECASE)


def test_all_check_constraints_use_drop_then_add_idiom(sql_text):
    drops = set(re.findall(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+(\w+)", sql_text, re.IGNORECASE))
    adds = set(re.findall(r"ADD\s+CONSTRAINT\s+(\w+)\s+CHECK", sql_text, re.IGNORECASE))
    assert drops == adds
    assert drops == {"chk_clients_status", "chk_clients_name_lowercase"}


def test_clients_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+clients\s+TO\s+alertmanager", sql_text, re.IGNORECASE)
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+clients_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# Coarse structural sanity -- app-layer-only enforcement, no FK to clients
# ---------------------------------------------------------------------------

def test_no_partition_by_clause(sql_text):
    assert "PARTITION BY" not in sql_text.upper()


def test_no_timestamptz_used(sql_text):
    # Epoch BIGINT for all new time columns, per project convention
    # (already followed by 0003_email_log.sql's attempted_at).
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    assert "TIMESTAMPTZ" not in code_only.upper()


def test_no_foreign_key_defined(sql_text):
    # Enforcement is app-layer only, per this feature's own brief:
    # alerts.client/role_scopes.client stay plain unconstrained columns,
    # no FK referencing this table from anywhere -- and this migration
    # itself defines no FK either (nothing references `clients`, since
    # nothing exists yet that would need to).
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    assert "REFERENCES" not in code_only.upper()


def test_no_index_beyond_unique_name(sql_text):
    # Deliberately no CREATE INDEX statement -- lookups are always by the
    # already-UNIQUE `name` column; see the migration's own comment.
    assert "CREATE INDEX" not in sql_text.upper()
