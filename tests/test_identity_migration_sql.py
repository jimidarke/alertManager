"""
Static sanity checks for sql/migrations/0002_identity.sql.

There is no live Postgres available in this environment (and the project
rule is that docker/docker-compose are never run from automation), so this
migration's SQL cannot actually be executed here. These tests do NOT
validate that the SQL is semantically correct against a real database --
only that the file is structurally sane: every table/column/constraint/FK
from the Task 14 brief is present with the expected type/shape, the
idempotency/grant/index conventions match sql/init/01-schema.sql and the
prior migrations, and the master_admin seed is idempotent.

This migration MUST be verified against a scratch Postgres instance before
this branch is deployed to prod (CREATE TABLE syntax errors, type name
typos, FK/PK definitions, etc. cannot be caught by regex matching alone).
"""

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = REPO_ROOT / "sql" / "migrations" / "0002_identity.sql"
REFERENCE_SCHEMA_SQL = REPO_ROOT / "sql" / "init" / "01-schema.sql"
PRIOR_MIGRATION_SQL = REPO_ROOT / "sql" / "migrations" / "0001_partition_management.sql"
NEXT_MIGRATION_SQL = REPO_ROOT / "sql" / "migrations" / "0003_email_log.sql"

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
    assert int(match.group(1)) == 2


def test_db_migrate_discovers_it_as_version_2():
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    versions = {m.version: m.name for m in migrations}
    assert versions.get(2) == "0002_identity.sql"


def test_all_migrations_coexist_in_sorted_order():
    # 0001 -> 0002 -> 0003 -> 0004 must all be discoverable together with no
    # duplicate-version collision (discover_migrations raises on that), and
    # sorted() must yield exactly this order.
    migrations = db_migrate.discover_migrations(MIGRATION_PATH.parent)
    names_in_order = [m.name for m in migrations]
    assert names_in_order == [
        "0001_partition_management.sql",
        "0002_identity.sql",
        "0003_email_log.sql",
        "0004_clients.sql",
    ]


def test_reference_files_readable():
    for path in (REFERENCE_SCHEMA_SQL, PRIOR_MIGRATION_SQL, NEXT_MIGRATION_SQL):
        assert path.is_file()
        assert path.stat().st_size > 0


def test_does_not_reference_email_log_table(sql_text):
    # This file must not depend on anything 0003_email_log.sql creates
    # (brief: "this file must not depend on anything 0003 creates" --
    # order-independence at apply time). The header comment legitimately
    # *names* 0003/email_log while explaining the numbering decision, so
    # strip `--` line comments before checking actual statements.
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    assert "email_log" not in code_only


def test_0003_does_not_reference_identity_tables():
    # Symmetric check: 0003 doesn't reach back into this migration's tables
    # either (guards against the *other* file having grown a dependency
    # since it shipped).
    next_text = NEXT_MIGRATION_SQL.read_text()
    for table in ("users", "roles", "user_roles", "role_scopes", "auth_tokens", "auth_audit"):
        assert table not in next_text


# ---------------------------------------------------------------------------
# Table creation is idempotent (IF NOT EXISTS), matching project convention
# ---------------------------------------------------------------------------

ALL_TABLES = ["users", "roles", "user_roles", "role_scopes", "auth_tokens", "auth_audit"]


@pytest.mark.parametrize("table", ALL_TABLES)
def test_creates_table_idempotently(sql_text, table):
    assert re.search(
        rf"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+{re.escape(table)}\s*\(", sql_text, re.IGNORECASE
    )


@pytest.mark.parametrize("table", ALL_TABLES)
def test_create_table_parens_are_balanced(sql_text, table):
    body = _extract_create_table_body(sql_text, table)
    assert body.count("(") == body.count(")")


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def users_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "users")


USERS_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("username", r"VARCHAR\(255\)\s+NOT\s+NULL\s+UNIQUE"),
    ("password_hash", r"TEXT\s+NOT\s+NULL"),
    ("status", r"VARCHAR\(20\)\s+NOT\s+NULL"),
    ("default_client", r"VARCHAR\(255\)"),
    ("must_change_password", r"BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+FALSE"),
    ("failed_attempts", r"INTEGER\s+NOT\s+NULL\s+DEFAULT\s+0"),
    ("locked_until", r"BIGINT"),
    # No users.expires_at: removed in v3.0.0's auth reset (see the migration's
    # own header). auth_tokens.expires_at below is a different column and stays.
    ("last_login_at", r"BIGINT"),
    ("created_at", r"BIGINT\s+NOT\s+NULL"),
    ("updated_at", r"BIGINT\s+NOT\s+NULL"),
]


@pytest.mark.parametrize("column_name,type_pattern", USERS_COLUMNS)
def test_users_column_present_with_expected_type(users_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}", re.IGNORECASE
    )
    assert col_re.search(users_body), f"users.{column_name} not found with type {type_pattern!r}"


def test_users_columns_appear_in_brief_order(users_body):
    positions = []
    for column_name, _ in USERS_COLUMNS:
        match = re.search(
            rf"(?:,|\A)\s*(?:--[^\n]*\n\s*)*{re.escape(column_name)}\s", users_body
        )
        assert match, f"could not locate definition start for users.{column_name}"
        positions.append((column_name, match.start()))
    ordered = sorted(positions, key=lambda p: p[1])
    assert [n for n, _ in ordered] == [n for n, _ in positions]


def test_users_username_check_constraint(sql_text):
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_users_username_lowercase", sql_text, re.IGNORECASE
    )
    assert re.search(
        r"ADD\s+CONSTRAINT\s+chk_users_username_lowercase\s+CHECK\s*\(\s*username\s*=\s*lower\(username\)\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_users_status_check_constraint_values(sql_text):
    match = re.search(r"CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", sql_text, re.IGNORECASE)
    assert match, "status CHECK constraint not found"
    values = [v.strip() for v in match.group(1).split(",")]
    assert values == ["'active'", "'disabled'"]


def test_users_status_check_uses_idempotent_drop_add_pattern(sql_text):
    assert re.search(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_users_status", sql_text, re.IGNORECASE)
    assert re.search(r"ADD\s+CONSTRAINT\s+chk_users_status\s+CHECK", sql_text, re.IGNORECASE)


def test_users_no_rate_limit_field(sql_text):
    # Brief: rate_limit is a deliberately dropped dead field (never
    # enforced in users.json; lockout replaces the security intent). The
    # header comment legitimately *names* rate_limit while explaining that
    # decision, so strip `--` line comments before checking actual
    # statements for a real column/reference.
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    assert "rate_limit" not in code_only.lower()


def test_users_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+users\s+TO\s+alertmanager", sql_text, re.IGNORECASE)
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+users_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def roles_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "roles")


ROLES_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("name", r"VARCHAR\(255\)\s+NOT\s+NULL\s+UNIQUE"),
    ("description", r"TEXT"),
    ("is_system", r"BOOLEAN\s+NOT\s+NULL\s+DEFAULT\s+FALSE"),
]


@pytest.mark.parametrize("column_name,type_pattern", ROLES_COLUMNS)
def test_roles_column_present_with_expected_type(roles_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}", re.IGNORECASE
    )
    assert col_re.search(roles_body), f"roles.{column_name} not found with type {type_pattern!r}"


def test_roles_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+roles\s+TO\s+alertmanager", sql_text, re.IGNORECASE)
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+roles_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# user_roles
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def user_roles_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "user_roles")


def test_user_roles_has_both_fk_columns(user_roles_body):
    assert re.search(r"user_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)", user_roles_body, re.IGNORECASE)
    assert re.search(r"role_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+roles\s*\(\s*id\s*\)", user_roles_body, re.IGNORECASE)


def test_user_roles_fks_cascade_on_delete(user_roles_body):
    # Brief: "user_roles references both users and roles with ON DELETE
    # CASCADE (deleting a user or role should clean up the join row)".
    user_fk = re.search(
        r"user_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)\s*ON\s+DELETE\s+CASCADE",
        user_roles_body,
        re.IGNORECASE,
    )
    role_fk = re.search(
        r"role_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+roles\s*\(\s*id\s*\)\s*ON\s+DELETE\s+CASCADE",
        user_roles_body,
        re.IGNORECASE,
    )
    assert user_fk, "user_roles.user_id must be ON DELETE CASCADE"
    assert role_fk, "user_roles.role_id must be ON DELETE CASCADE"


def test_user_roles_composite_primary_key_prevents_duplicates(user_roles_body):
    assert re.search(r"PRIMARY\s+KEY\s*\(\s*user_id\s*,\s*role_id\s*\)", user_roles_body, re.IGNORECASE)


def test_user_roles_no_surrogate_id_column(user_roles_body):
    # Brief lists only user_id/role_id for this join table -- a composite
    # PK on the pair is the whole point, not a separate id.
    assert not re.search(r"(?<![A-Za-z0-9_])id\s+BIGSERIAL", user_roles_body, re.IGNORECASE)


def test_user_roles_role_id_index(sql_text):
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_user_roles_role_id\s+ON\s+user_roles\s*\(\s*role_id\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_user_roles_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+user_roles\s+TO\s+alertmanager", sql_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# role_scopes
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def role_scopes_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "role_scopes")


def test_role_scopes_fk_cascades_on_delete(role_scopes_body):
    assert re.search(
        r"role_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+roles\s*\(\s*id\s*\)\s*ON\s+DELETE\s+CASCADE",
        role_scopes_body,
        re.IGNORECASE,
    )


def test_role_scopes_client_column(role_scopes_body):
    assert re.search(r"(?<![A-Za-z0-9_])client\s+TEXT\s+NOT\s+NULL", role_scopes_body, re.IGNORECASE)


def test_role_scopes_permission_column_type(role_scopes_body):
    assert re.search(
        r"(?<![A-Za-z0-9_])permission\s+VARCHAR\(50\)\s+NOT\s+NULL", role_scopes_body, re.IGNORECASE
    )


def test_role_scopes_permission_check_values(sql_text):
    match = re.search(r"CHECK\s*\(\s*permission\s+IN\s*\(([^)]*)\)\s*\)", sql_text, re.IGNORECASE | re.DOTALL)
    assert match, "permission CHECK constraint not found"
    values = [v.strip() for v in match.group(1).split(",")]
    # 'thresholds:write' and 'users:manage' were removed in v3.0.0: both were
    # grantable via the admin UI and enforced by zero code.
    assert values == ["'*'", "'logs:read'", "'alerts:write'"]


def test_role_scopes_permission_check_uses_idempotent_drop_add_pattern(sql_text):
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_role_scopes_permission", sql_text, re.IGNORECASE
    )
    assert re.search(r"ADD\s+CONSTRAINT\s+chk_role_scopes_permission\s+CHECK", sql_text, re.IGNORECASE)


def test_role_scopes_uniqueness_on_role_client_permission(role_scopes_body):
    # Brief: "reasonable uniqueness constraint (no duplicate
    # (role,client,permission) tuples)" -- satisfied via composite PK here.
    assert re.search(
        r"PRIMARY\s+KEY\s*\(\s*role_id\s*,\s*client\s*,\s*permission\s*\)", role_scopes_body, re.IGNORECASE
    )


def test_role_scopes_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+role_scopes\s+TO\s+alertmanager", sql_text, re.IGNORECASE)


# ---------------------------------------------------------------------------
# auth_tokens
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def auth_tokens_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "auth_tokens")


AUTH_TOKENS_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("name", r"VARCHAR\(255\)\s+NOT\s+NULL"),
    ("token_sha256", r"CHAR\(64\)\s+NOT\s+NULL\s+UNIQUE"),
    ("user_id", r"BIGINT\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)\s*ON\s+DELETE\s+RESTRICT"),
    ("status", r"VARCHAR\(20\)\s+NOT\s+NULL"),
    ("expires_at", r"BIGINT"),
    ("last_used_at", r"BIGINT"),
    ("created_at", r"BIGINT\s+NOT\s+NULL"),
]


@pytest.mark.parametrize("column_name,type_pattern", AUTH_TOKENS_COLUMNS)
def test_auth_tokens_column_present_with_expected_type(auth_tokens_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}", re.IGNORECASE
    )
    assert col_re.search(auth_tokens_body), f"auth_tokens.{column_name} not found with type {type_pattern!r}"


def test_auth_tokens_user_id_is_restrict_not_cascade(auth_tokens_body):
    # Deliberately NOT CASCADE: tools/user-manager.py's `delete` is a
    # distinct verb from `disable` (docs/OVERHAUL_PLAN.md section 4.2), and
    # this table's own status='revoked' soft-delete design exists
    # specifically so token-mutation history in auth_audit survives --
    # CASCADE on user deletion would silently erase that same history,
    # contradicting the table's own design. RESTRICT forces an explicit
    # token revoke/cleanup before a user row can be deleted.
    assert re.search(
        r"user_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)\s*ON\s+DELETE\s+RESTRICT",
        auth_tokens_body,
        re.IGNORECASE,
    )
    assert not re.search(
        r"user_id\s+BIGINT\s+NOT\s+NULL\s+REFERENCES\s+users\s*\(\s*id\s*\)\s*ON\s+DELETE\s+CASCADE",
        auth_tokens_body,
        re.IGNORECASE,
    )


def test_auth_tokens_status_check_values(sql_text):
    match = re.search(
        r"chk_auth_tokens_status\s+CHECK\s*\(\s*status\s+IN\s*\(([^)]*)\)\s*\)", sql_text, re.IGNORECASE
    )
    assert match, "auth_tokens status CHECK constraint not found"
    values = [v.strip() for v in match.group(1).split(",")]
    assert values == ["'active'", "'revoked'"]


def test_auth_tokens_status_check_uses_idempotent_drop_add_pattern(sql_text):
    assert re.search(
        r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+chk_auth_tokens_status", sql_text, re.IGNORECASE
    )
    assert re.search(r"ADD\s+CONSTRAINT\s+chk_auth_tokens_status\s+CHECK", sql_text, re.IGNORECASE)


def test_auth_tokens_user_id_index(sql_text):
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_auth_tokens_user_id\s+ON\s+auth_tokens\s*\(\s*user_id\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_auth_tokens_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+auth_tokens\s+TO\s+alertmanager", sql_text, re.IGNORECASE)
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+auth_tokens_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# auth_audit
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def auth_audit_body(sql_text) -> str:
    return _extract_create_table_body(sql_text, "auth_audit")


AUTH_AUDIT_COLUMNS = [
    ("id", r"BIGSERIAL\s+PRIMARY\s+KEY"),
    ("at", r"BIGINT\s+NOT\s+NULL"),
    ("event", r"TEXT\s+NOT\s+NULL"),
    ("username", r"TEXT"),
    ("source_ip", r"TEXT"),
    ("detail", r"JSONB"),
]


@pytest.mark.parametrize("column_name,type_pattern", AUTH_AUDIT_COLUMNS)
def test_auth_audit_column_present_with_expected_type(auth_audit_body, column_name, type_pattern):
    col_re = re.compile(
        rf"(?<![A-Za-z0-9_]){re.escape(column_name)}\s+{type_pattern}", re.IGNORECASE
    )
    assert col_re.search(auth_audit_body), f"auth_audit.{column_name} not found with type {type_pattern!r}"


def test_auth_audit_event_not_check_constrained(sql_text):
    # Brief: "don't over-constrain with a CHECK since this list will grow".
    assert not re.search(r"CHECK\s*\(\s*event\s+IN", sql_text, re.IGNORECASE)


def test_auth_audit_username_nullable(auth_audit_body):
    # username must NOT be NOT NULL -- a login attempt against a
    # nonexistent username still needs a row.
    match = re.search(r"(?<![A-Za-z0-9_])username\s+TEXT([^,]*)", auth_audit_body, re.IGNORECASE)
    assert match
    assert "NOT NULL" not in match.group(1).upper()


def test_auth_audit_indexes(sql_text):
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_auth_audit_at\s+ON\s+auth_audit\s*\(\s*at\s+DESC\s*\)",
        sql_text,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_auth_audit_event\s+ON\s+auth_audit\s*\(\s*event\s*\)",
        sql_text,
        re.IGNORECASE,
    )
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_auth_audit_username\s+ON\s+auth_audit\s*\(\s*username\s*\)",
        sql_text,
        re.IGNORECASE,
    )


def test_auth_audit_index_naming_matches_alerts_convention():
    # sql/init/01-schema.sql names its timestamp index idx_alerts_timestamp
    # (idx_<table>_<column>) with the same ON (<column> DESC) shape
    # idx_auth_audit_at mirrors.
    reference_text = REFERENCE_SCHEMA_SQL.read_text()
    assert re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS\s+idx_alerts_timestamp\s+ON\s+alerts\s*\(\s*timestamp\s+DESC\s*\)",
        reference_text,
        re.IGNORECASE,
    )


def test_auth_audit_grants(sql_text):
    assert re.search(r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+auth_audit\s+TO\s+alertmanager", sql_text, re.IGNORECASE)
    assert re.search(
        r"GRANT\s+ALL\s+PRIVILEGES\s+ON\s+SEQUENCE\s+auth_audit_id_seq\s+TO\s+alertmanager",
        sql_text,
        re.IGNORECASE,
    )


# ---------------------------------------------------------------------------
# master_admin seed: idempotent, correct scope
# ---------------------------------------------------------------------------

def test_seeds_master_admin_role_idempotently(sql_text):
    assert re.search(
        r"INSERT\s+INTO\s+roles\s*\(\s*name\s*,\s*description\s*,\s*is_system\s*\)\s*"
        r"VALUES\s*\(\s*'master_admin'\s*,[^)]*,\s*TRUE\s*\)\s*"
        r"ON\s+CONFLICT\s*\(\s*name\s*\)\s+DO\s+NOTHING",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )


def test_master_admin_is_flagged_system(sql_text):
    match = re.search(r"INSERT\s+INTO\s+roles\s*\([^)]*\)\s*VALUES\s*\(([^)]*)\)", sql_text, re.IGNORECASE)
    assert match
    assert re.search(r"TRUE", match.group(1), re.IGNORECASE)


def test_seeds_master_admin_wildcard_scope_idempotently(sql_text):
    seed_match = re.search(
        r"INSERT\s+INTO\s+role_scopes\s*\(\s*role_id\s*,\s*client\s*,\s*permission\s*\)\s*"
        r"SELECT\s+r\.id\s*,\s*'\*'\s*,\s*'\*'\s*"
        r"FROM\s+roles\s+r\s*"
        r"WHERE\s+r\.name\s*=\s*'master_admin'\s*"
        r"ON\s+CONFLICT\s*\(\s*role_id\s*,\s*client\s*,\s*permission\s*\)\s+DO\s+NOTHING",
        sql_text,
        re.IGNORECASE | re.DOTALL,
    )
    assert seed_match, "master_admin ('*','*') role_scopes seed not found or not idempotent"


def test_seed_statements_appear_exactly_once(sql_text):
    assert len(re.findall(r"INSERT\s+INTO\s+roles", sql_text, re.IGNORECASE)) == 1
    assert len(re.findall(r"INSERT\s+INTO\s+role_scopes", sql_text, re.IGNORECASE)) == 1


def test_seed_uses_on_conflict_not_unguarded_insert(sql_text):
    # Every INSERT in this file must be defensively idempotent -- a bare
    # INSERT without ON CONFLICT would fail (or duplicate) on a re-run.
    inserts = re.findall(r"INSERT\s+INTO\s+\w+[^;]*;", sql_text, re.IGNORECASE | re.DOTALL)
    assert len(inserts) == 2
    for stmt in inserts:
        assert re.search(r"ON\s+CONFLICT", stmt, re.IGNORECASE), f"non-idempotent INSERT: {stmt[:80]!r}"


# ---------------------------------------------------------------------------
# Coarse structural sanity
# ---------------------------------------------------------------------------

def test_no_partition_by_clause(sql_text):
    # None of these tables are partitioned, unlike alerts.
    assert "PARTITION BY" not in sql_text.upper()

def test_no_timestamptz_used(sql_text):
    # Brief: epoch BIGINT for all new time columns in this migration; only
    # alerts.timestamp stays TIMESTAMPTZ (a different table, not touched
    # here).
    code_only = re.sub(r"--[^\n]*", "", sql_text)
    assert "TIMESTAMPTZ" not in code_only.upper()


def test_all_check_constraints_use_drop_then_add_idiom(sql_text):
    # Same convention as chk_level / chk_category / chk_ingestion_source in
    # sql/init/01-schema.sql and chk_email_log_status in 0003, so
    # re-applying this file is a safe no-op.
    drops = set(re.findall(r"DROP\s+CONSTRAINT\s+IF\s+EXISTS\s+(\w+)", sql_text, re.IGNORECASE))
    adds = set(re.findall(r"ADD\s+CONSTRAINT\s+(\w+)\s+CHECK", sql_text, re.IGNORECASE))
    assert drops == adds
    assert drops == {"chk_users_status", "chk_users_username_lowercase", "chk_role_scopes_permission", "chk_auth_tokens_status"}


def test_reference_schema_uses_same_check_idiom():
    reference_text = REFERENCE_SCHEMA_SQL.read_text()
    assert "DROP CONSTRAINT IF EXISTS chk_level" in reference_text
    assert "ADD CONSTRAINT chk_level CHECK" in reference_text
