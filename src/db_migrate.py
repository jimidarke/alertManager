#!/usr/bin/env python3
"""
Alert Manager Database Migration Runner

Applies versioned SQL migration files from sql/migrations/ against
PostgreSQL, tracked in a schema_migrations table. Migrations are applied in
sorted order, each in its own transaction, serialized across concurrent runs
via a Postgres advisory lock. Fails fast: on any error the run stops
immediately and the process exits 1 (no partial application, no retry).

sql/init/ is the frozen empty-volume bootstrap schema and is never touched by
this script. sql/migrations/ is the source of truth for all schema changes
made after initial deployment.

Usage:
    python3 src/db_migrate.py [--dry-run] [--dsn DSN]

DSN resolution:
    --dsn, if given, is passed to psycopg2 as-is (libpq connection string or
    URI). Otherwise connection parameters are read from the same POSTGRES_*
    env vars used by src/database_logger.py: POSTGRES_HOST, POSTGRES_PORT,
    POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD.
"""

import argparse
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set

import psycopg2
from psycopg2 import errors as pg_errors

# Arbitrary fixed key for the migration runner's session-level advisory lock.
# Must stay stable across versions so concurrent invocations of this script
# actually serialize against each other.
ADVISORY_LOCK_KEY = 891273465

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "sql" / "migrations"

MIGRATION_FILENAME_RE = re.compile(r"^(\d+)_(.+)\.sql$")


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path


def discover_migrations(migrations_dir: Path) -> List[Migration]:
    """
    Find, validate, and sort migration files in migrations_dir.

    Every *.sql file must match NNNN_description.sql. Malformed filenames and
    duplicate version numbers are fatal configuration errors (fail fast)
    rather than being silently skipped.
    """
    if not migrations_dir.is_dir():
        raise FileNotFoundError(f"Migrations directory not found: {migrations_dir}")

    migrations: List[Migration] = []
    seen_versions = {}

    for sql_path in sorted(migrations_dir.glob("*.sql")):
        match = MIGRATION_FILENAME_RE.match(sql_path.name)
        if not match:
            raise ValueError(
                f"Migration file does not match NNNN_description.sql convention: {sql_path.name}"
            )

        version = int(match.group(1))
        if version in seen_versions:
            raise ValueError(
                f"Duplicate migration version {version}: "
                f"{seen_versions[version]} and {sql_path.name}"
            )

        seen_versions[version] = sql_path.name
        migrations.append(Migration(version=version, name=sql_path.name, path=sql_path))

    migrations.sort(key=lambda m: m.version)
    return migrations


def get_connection_kwargs() -> dict:
    """
    Build psycopg2 connection kwargs from POSTGRES_* env vars.

    Mirrors the env var names and defaults used by
    src/database_logger.py's _create_db_pool.
    """
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "database": os.getenv("POSTGRES_DB", "alertmanager"),
        "user": os.getenv("POSTGRES_USER", "alertmanager"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
    }


def connect(dsn: Optional[str] = None):
    """Open a psycopg2 connection, from an explicit DSN or POSTGRES_* env vars."""
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(**get_connection_kwargs())


def ensure_schema_migrations_table(conn) -> None:
    """Create the schema_migrations tracking table if it doesn't exist yet."""
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at BIGINT NOT NULL
            )
            """
        )


def get_applied_versions(conn) -> Set[int]:
    """
    Return the set of migration versions already recorded as applied.

    Tolerates a missing schema_migrations table (returns an empty set) so
    this can also be used for a read-only --dry-run preview against a
    brand-new database without creating anything.
    """
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT version FROM schema_migrations")
            return {row[0] for row in cur.fetchall()}
        except pg_errors.UndefinedTable:
            return set()


def apply_migration(conn, migration: Migration) -> None:
    """
    Apply a single migration file in its own transaction.

    The migration SQL and its schema_migrations bookkeeping row are
    committed together; on any error the transaction is rolled back and the
    exception propagates (fail fast: no partial application, no retry).
    """
    sql_text = migration.path.read_text()

    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(sql_text)
            cur.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                (migration.version, migration.name, int(time.time())),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def run(migrations_dir: Path, dsn: Optional[str] = None, dry_run: bool = False) -> int:
    """Orchestrate a migration run. Returns a process exit code (0 or 1)."""
    try:
        migrations = discover_migrations(migrations_dir)
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    try:
        conn = connect(dsn)
    except Exception as e:
        print(f"ERROR: Failed to connect to database: {e}", file=sys.stderr)
        return 1

    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))

        try:
            if dry_run:
                applied = get_applied_versions(conn)
                pending = [m for m in migrations if m.version not in applied]
                if not migrations:
                    print("No migration files found.")
                elif not pending:
                    print(f"Up to date. {len(migrations)} migration(s) already applied, nothing pending.")
                else:
                    print(f"DRY RUN: {len(pending)} migration(s) would be applied:")
                    for m in pending:
                        print(f"  {m.name}")
                return 0

            # Deliberately unguarded: a failure here (e.g. permission denied)
            # is unexpected enough that a raw traceback is more useful for
            # diagnosis than a manufactured one-line message; it still exits
            # non-zero via the interpreter's default unhandled-exception path.
            ensure_schema_migrations_table(conn)
            applied = get_applied_versions(conn)
            pending = [m for m in migrations if m.version not in applied]

            if not pending:
                print("No pending migrations.")
                return 0

            for m in pending:
                print(f"Applying {m.name}...")
                try:
                    apply_migration(conn, m)
                except Exception as e:
                    print(f"ERROR: Migration {m.name} failed: {e}", file=sys.stderr)
                    return 1
                print(f"Applied {m.name}")

            print(f"Applied {len(pending)} migration(s).")
            return 0
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
    finally:
        conn.close()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Alert Manager database migration runner")
    parser.add_argument(
        "--dry-run", action="store_true", help="Show pending migrations without applying them"
    )
    parser.add_argument(
        "--dsn", help="Full libpq connection string/URI (overrides POSTGRES_* env vars)"
    )
    args = parser.parse_args(argv)

    return run(MIGRATIONS_DIR, dsn=args.dsn, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
