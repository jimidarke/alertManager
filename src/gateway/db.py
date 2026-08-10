"""
Alert Manager - gateway.db

Owns this package's only pooled Postgres connection (a
`psycopg2.pool.ThreadedConnectionPool`) and wires it, plus a `UserStore`
instance, onto `app.extensions` so every blueprint (Task 19+) can reach
either without importing this module directly:

    from flask import current_app
    pool = current_app.extensions['db_pool']
    store = current_app.extensions['user_store']

Why a pool here but NOT inside UserStore: src/userstore/store.py
deliberately opens a fresh, short-lived connection per call (see that
module's own docstring) because account operations (login, CLI admin
actions) are low-frequency, not a hot per-request path. The gateway is a
request-serving Flask app under gunicorn with multiple threads (plan
section 4.9: 1 worker x 8 gthread threads); later gateway-owned queries
that ARE hot (dashboard summary/critical queries against the `alerts`
table, docs/OVERHAUL_PLAN.md section 4.6) need a real connection pool so
they are not opening/closing a fresh TCP+auth handshake on every request.
UserStore itself stays pool-free and framework-free; this pool exists
purely for gateway-owned queries, never for UserStore's own internal
connections.

Fail-fast: mirrors src/database_logger.py's `_create_db_pool()` exactly --
`sys.exit(1)` (after a CRITICAL log) if the pool cannot be constructed,
since psycopg2's `ThreadedConnectionPool.__init__` eagerly opens `minconn`
live connections, so a bad host/credentials fails immediately at startup
rather than on the first request. Per CLAUDE.md's "do not implement
failsafes" rule, the gateway has no degraded-DB startup mode.

--------------------------------------------------------------------------
`iso_now()` / `pooled_cursor()` / `DBUnavailable` -- shared query plumbing
--------------------------------------------------------------------------
Hoisted here in the final whole-branch cleanup pass: three near-identical
implementations of a pooled-cursor context manager (`api/routes.py`'s
`_check_db_health`, `dashboard/routes.py`'s `_pooled_cursor`, and
`admin/routes.py`'s ad hoc `getconn`/`finally: putconn` blocks) and two
copies of an ISO-8601-with-trailing-Z timestamp helper (`api/routes.py`,
`dashboard/routes.py`) had accumulated across blueprints with no shared
home. `dashboard/routes.py`'s `_pooled_cursor` was the most complete of the
three (getconn, a `RealDictCursor`, best-effort `rollback()` on a
mid-query failure, always `putconn`, normalized to one exception type), so
it moved here verbatim and every blueprint that runs ad hoc queries
against `app.extensions['db_pool']` now imports the one copy instead of
maintaining its own. This is a mechanical consolidation, not a behavior
change -- each call site's own exception handling (fail-open dict/bool vs.
fail-closed 503) is unchanged, only the connection-acquisition boilerplate
underneath it is now shared.
"""

import logging
import sys
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

import pytz
from psycopg2 import pool as psycopg2_pool
from psycopg2.extras import RealDictCursor

from userstore import UserStore

from .config import GatewayConfig

logger = logging.getLogger("gateway.db")


def iso_now() -> str:
    """UTC timestamp in the `<ISO-8601>Z` shape this package's JSON responses use uniformly."""
    return datetime.now(pytz.UTC).isoformat() + "Z"


class DBUnavailable(Exception):
    """Raised by `pooled_cursor` for any failure acquiring a connection or running a query against it."""


@contextmanager
def pooled_cursor(pool):
    """
    Borrow a connection from `pool`, yield a `RealDictCursor` (named-column
    access, matching `userstore/store.py`'s own convention), and always
    return the connection to the pool afterward -- never closes it, the
    pool owns its lifecycle. A failed `getconn()` and any exception raised
    while the cursor is in use (including from `execute`/`fetch*`) are both
    normalized to `DBUnavailable`; on the latter, a best-effort `rollback()`
    is attempted before the connection goes back to the pool, so a failed
    query doesn't leave the connection stuck in an aborted-transaction
    state for the next borrower.
    """
    try:
        conn = pool.getconn()
    except Exception as exc:
        raise DBUnavailable(str(exc)) from exc

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
    except Exception as exc:
        try:
            conn.rollback()
        except Exception:
            pass
        raise DBUnavailable(str(exc)) from exc
    finally:
        try:
            pool.putconn(conn)
        except Exception:
            pass


def create_db_pool(config: GatewayConfig) -> psycopg2_pool.ThreadedConnectionPool:
    """
    Construct a ThreadedConnectionPool from `config`. Split out from
    `init_db()` so tests (and any future caller that wants a pool without
    a Flask app, e.g. a one-off script) can exercise pool construction in
    isolation.
    """
    try:
        db_pool = psycopg2_pool.ThreadedConnectionPool(
            minconn=config.POSTGRES_POOL_MIN,
            maxconn=config.POSTGRES_POOL_MIN + config.POSTGRES_POOL_MAX_OVERFLOW,
            host=config.POSTGRES_HOST,
            port=config.POSTGRES_PORT,
            database=config.POSTGRES_DB,
            user=config.POSTGRES_USER,
            password=config.POSTGRES_PASSWORD,
        )
    except Exception:
        logger.critical("Failed to create database connection pool", exc_info=True)
        sys.exit(1)

    logger.info(
        "Database connection pool created: %s:%s/%s (min=%s, max=%s)",
        config.POSTGRES_HOST,
        config.POSTGRES_PORT,
        config.POSTGRES_DB,
        config.POSTGRES_POOL_MIN,
        config.POSTGRES_POOL_MIN + config.POSTGRES_POOL_MAX_OVERFLOW,
    )
    return db_pool


def init_db(app, config: GatewayConfig, user_store: Optional[UserStore] = None) -> None:
    """
    Build the connection pool and a UserStore, and attach both to
    `app.extensions['db_pool']` / `app.extensions['user_store']`. Called
    once from `create_app()` (gateway/__init__.py).

    `user_store` is an optional override (default: `UserStore.from_env()`,
    the standard instance every other consumer of that module builds) --
    purely a testing seam so `create_app()` can be exercised with a fake
    store without also needing a live Postgres for UserStore's own
    connect-per-call methods.
    """
    app.extensions["db_pool"] = create_db_pool(config)
    app.extensions["user_store"] = user_store if user_store is not None else UserStore.from_env()
