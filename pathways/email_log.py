"""
Email Log Writer

Deliberate deviation from this project's fail-fast/no-failsafes rule (see
CLAUDE.md "Development Notes"): email is the last-resort alerting channel,
so an outage of the *tracking* database must never become an outage of
*notifications*. Every method here catches its own exceptions, logs loudly
(ERROR, with full context), and never raises -- callers (EmailPathway) must
never need a try/except around calls into this class.

Writes to the `email_log` table (sql/migrations/0003_email_log.sql), which
records every email send attempt -- success or failure -- made by
EmailPathway (see pathways/email_pathway.py).
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional

import psycopg2

logger = logging.getLogger(__name__)


class EmailLogWriter:
    """
    Fail-open psycopg2 writer for the `email_log` table.

    Maintains a single lazy connection in autocommit mode: no connection is
    opened until the first record() call, and any failure (connect or
    insert) drops the connection so the *next* record() call attempts a
    fresh connect -- there is no retry loop within a single call.
    """

    _INSERT_SQL = """
        INSERT INTO email_log (
            attempted_at, pathway, aggregated, alert_count, level, clients,
            recipients, subject, status, http_status, error,
            graph_request_id, duration_ms, retries
        ) VALUES (
            %(attempted_at)s, %(pathway)s, %(aggregated)s, %(alert_count)s,
            %(level)s, %(clients)s, %(recipients)s, %(subject)s, %(status)s,
            %(http_status)s, %(error)s, %(graph_request_id)s,
            %(duration_ms)s, %(retries)s
        )
    """

    def __init__(self, dsn: Optional[Dict[str, Any]] = None):
        """
        Args:
            dsn: Optional psycopg2 connection kwargs override (host, port,
                dbname, user, password). Defaults to the same POSTGRES_*
                environment variables used by src/database_logger.py, read
                lazily (at first connection attempt, not here) so a missing
                env var at construction time doesn't matter.
        """
        self._dsn_override = dsn
        self._conn = None  # lazy -- created on first record() call

    @staticmethod
    def _default_dsn() -> Dict[str, Any]:
        return {
            'host': os.getenv('POSTGRES_HOST', 'localhost'),
            'port': int(os.getenv('POSTGRES_PORT', 5432)),
            'dbname': os.getenv('POSTGRES_DB', 'alertmanager'),
            'user': os.getenv('POSTGRES_USER', 'alertmanager'),
            'password': os.getenv('POSTGRES_PASSWORD', ''),
        }

    def record(
        self,
        *,
        pathway: str,
        status: str,
        aggregated: bool = False,
        alert_count: int = 1,
        level: Optional[str] = None,
        clients: Optional[List[str]] = None,
        recipients: Optional[List[str]] = None,
        subject: Optional[str] = None,
        http_status: Optional[int] = None,
        error: Optional[str] = None,
        graph_request_id: Optional[str] = None,
        duration_ms: Optional[int] = None,
        retries: int = 0,
    ) -> None:
        """
        Record one email send attempt. Parameters map 1:1 onto the
        `email_log` columns (attempted_at is stamped internally as "now").

        Args:
            pathway: Config pathway name that made the attempt (e.g.
                "email_alerts", "email_revysmc_critical").
            status: 'accepted' (Graph API returned HTTP 202 -- queued, not
                confirmed delivered) or 'failed'.
            aggregated: True for a storm-prevention aggregated send.
            alert_count: Number of underlying alerts this send represents.
            level: Alert level driving the send. None when the failure
                happened before alert-specific context was available (e.g.
                an MSAL token failure).
            clients: Distinct client(s) covered by this send.
            recipients: Destination addresses this send was attempted to.
            subject: Rendered email subject line.
            http_status: Raw HTTP status from the Graph sendMail call. None
                when the attempt never reached that call.
            error: Error detail for failed attempts.
            graph_request_id: Microsoft Graph's request-id response header,
                when available.
            duration_ms: Wall-clock duration of the send attempt.
            retries: Number of retries performed for this attempt.

        Never raises. On any failure (connect or insert) the failure is
        logged at ERROR with full context and this method simply returns --
        callers do not need to wrap calls to this method in try/except.
        """
        conn = self._get_connection()
        if conn is None:
            return  # connect failure already logged by _get_connection()

        params = {
            'attempted_at': int(time.time()),
            'pathway': pathway,
            'aggregated': aggregated,
            'alert_count': alert_count,
            'level': level,
            'clients': clients,
            'recipients': recipients,
            'subject': subject,
            'status': status,
            'http_status': http_status,
            'error': error,
            'graph_request_id': graph_request_id,
            'duration_ms': duration_ms,
            'retries': retries,
        }

        try:
            with conn.cursor() as cur:
                cur.execute(self._INSERT_SQL, params)
        except Exception as e:
            logger.error(
                f"email_log insert failed (pathway={pathway!r}, status={status!r}): {e}",
                exc_info=True,
            )
            self._drop_connection()

    def _get_connection(self):
        """
        Return the lazily-created connection, reconnecting if none is
        currently open. Returns None (and logs at ERROR) if connecting
        fails -- there is no retry loop here, just one attempt per call.
        """
        if self._conn is not None:
            return self._conn

        try:
            dsn = self._dsn_override if self._dsn_override is not None else self._default_dsn()
            conn = psycopg2.connect(connect_timeout=3, **dsn)
            conn.autocommit = True
        except Exception as e:
            logger.error(f"email_log: failed to connect to Postgres: {e}", exc_info=True)
            return None

        self._conn = conn
        return self._conn

    def _drop_connection(self):
        """Close and discard the current connection so the next record() call reconnects fresh."""
        if self._conn is None:
            return

        try:
            self._conn.close()
        except Exception as e:
            logger.error(f"email_log: error closing broken connection: {e}", exc_info=True)
        finally:
            self._conn = None
