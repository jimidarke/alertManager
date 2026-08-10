#!/usr/bin/env python3
"""
Alert Manager - tools/reset-identity.py

Destroys the entire identity store so it can be rebuilt from scratch:
drops the six identity tables and deletes migration 0002's schema_migrations
row, so `python3 src/db_migrate.py` re-applies sql/migrations/0002_identity.sql
as if it had never run.

Every user account, role, scope, Bearer token and audit row is destroyed.
Alert data is NOT touched: `alerts` (and its partitions) and `email_log`
belong to other migrations and are never referenced here.

This script deliberately does exactly one thing. It does not re-run the
migration and it does not seed an admin, so each stage of the reset is a
separate, individually visible command:

    python3 tools/reset-identity.py          # 1. destroy
    python3 src/db_migrate.py                # 2. recreate schema + master_admin role
    python3 tools/user-manager.py init-admin <username>   # 3. seed the one account

There is no --force/--yes flag, on purpose. The only situation in which
this would run unattended is the one situation in which it must not.
Confirmation is an interactive prompt requiring the exact string
"RESET IDENTITY"; a non-TTY stdin is a hard error, not an implicit yes.

Usage:
    python3 tools/reset-identity.py [--dsn DSN]

DSN resolution matches src/db_migrate.py: --dsn is passed to psycopg2 as-is,
otherwise POSTGRES_HOST/PORT/DB/USER/PASSWORD are read from the environment.
"""

import argparse
import os
import sys

import psycopg2

# Identity tables, in drop order. CASCADE handles the FKs between them, but
# the ordering (children before parents) keeps the intent readable and means
# the statement still makes sense if CASCADE is ever removed.
IDENTITY_TABLES = [
    "auth_audit",
    "auth_tokens",
    "role_scopes",
    "user_roles",
    "roles",
    "users",
]

# The migration whose schema_migrations row must go, so db_migrate.py stops
# treating 0002 as already applied. Must stay in sync with the numeric prefix
# of sql/migrations/0002_identity.sql.
IDENTITY_MIGRATION_VERSION = 2

CONFIRMATION_PHRASE = "RESET IDENTITY"


def get_connection_kwargs() -> dict:
    """Mirror src/db_migrate.py's POSTGRES_* env resolution exactly."""
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "database": os.getenv("POSTGRES_DB", "alertmanager"),
        "user": os.getenv("POSTGRES_USER", "alertmanager"),
        "password": os.getenv("POSTGRES_PASSWORD", ""),
    }


def connect(dsn=None):
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(**get_connection_kwargs())


def summarize(conn) -> None:
    """
    Print what is about to be destroyed. A reset run against the wrong
    database is unrecoverable, so the operator sees real row counts from the
    database actually connected to before typing the confirmation.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT current_database(), inet_server_addr(), inet_server_port()")
        dbname, host, port = cur.fetchone()
        print(f"Database: {dbname} on {host or 'local socket'}:{port or '-'}")

        for table in IDENTITY_TABLES:
            try:
                cur.execute(f"SELECT count(*) FROM {table}")
                print(f"  {table:<14} {cur.fetchone()[0]:>6} row(s)  -> DROP")
            except psycopg2.errors.UndefinedTable:
                conn.rollback()
                print(f"  {table:<14} {'absent':>6}         -> skip")


def confirm() -> bool:
    if not sys.stdin.isatty():
        print(
            "ERROR: refusing to run non-interactively. This script requires a TTY "
            f'so an operator can type "{CONFIRMATION_PHRASE}" at the prompt.',
            file=sys.stderr,
        )
        return False

    print()
    print("This destroys EVERY account, role, scope, token and audit row above.")
    print("Alert history (alerts, email_log) is not affected.")
    print()
    try:
        answer = input(f'Type "{CONFIRMATION_PHRASE}" to proceed: ')
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer.strip() == CONFIRMATION_PHRASE


def reset(conn) -> None:
    """Drop the identity tables and un-record migration 0002, in one transaction."""
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {', '.join(IDENTITY_TABLES)} CASCADE")
            cur.execute(
                "DELETE FROM schema_migrations WHERE version = %s",
                (IDENTITY_MIGRATION_VERSION,),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Destroy the identity store so it can be rebuilt from scratch.",
    )
    parser.add_argument(
        "--dsn", default=None,
        help="libpq connection string or URI (default: POSTGRES_* environment variables)",
    )
    args = parser.parse_args()

    try:
        conn = connect(args.dsn)
    except Exception as e:
        print(f"ERROR: Failed to connect to database: {e}", file=sys.stderr)
        return 1

    try:
        conn.autocommit = True
        summarize(conn)

        if not confirm():
            print("Aborted. Nothing was changed.")
            return 1

        reset(conn)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print()
    print("Identity store destroyed. Next:")
    print("  python3 src/db_migrate.py")
    print("  python3 tools/user-manager.py init-admin <username>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
