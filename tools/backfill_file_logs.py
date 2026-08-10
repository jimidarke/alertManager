#!/usr/bin/env python3
"""
Alert Manager - 90-day file-log backfill (one-off tool)

Parses the alert-content log files written by pathways/file_pathway.py
(FilePathway) -- `data/logs/file/{date}.log` (flat, clientless) and
`data/logs/file/{client}/{date}.log` (per-client, weekly-rotated) -- and
inserts them into the partitioned Postgres `alerts` table so historical
alert data becomes queryable ahead of the Phase 4 dashboard.

Why this exists: the `alerts` table's partition automation was broken from
2026-01-11 onward (only 8 weeks of partitions were ever created and nothing
scheduled more), so every DB insert since that date silently failed. The
file logs are the only complete record of alert history since then. This
tool is NOT part of the running service -- it is run once, manually,
against a `data/logs` directory mounted read-only, to fill that gap.

Log line format parsed (exactly what data/config/docker.json's `file_log`
pathway writes -- see that file's `format` string, the only pathway config
actually referenced by docker-compose.yml):

    {timestamp:10-digit epoch} [{level}] [{category}] {source} [{client}]: {title} - {message}

`title`/`message`/`source` have already been through FilePathway's
single-line sanitization (pathways/base_pathway.py's sanitize_for_single_line):
newlines/CR/tabs replaced (default " ↵ "), runs of whitespace collapsed,
and `message` truncated to `max_message_length` (default 500) with a "..."
suffix. Those artifacts are kept as-is by the parser -- they are just text.

Usage:
    python3 tools/backfill_file_logs.py --dry-run
    python3 tools/backfill_file_logs.py [--logs-root PATH] [--since-days 90]
        [--dsn DSN] [--force] [--batch-size 500] [--skip-refresh]

DSN resolution (mirrors src/db_migrate.py / src/database_logger.py):
    --dsn, if given, is passed to psycopg2 as-is (libpq connection string or
    URI). Otherwise connection parameters are read from POSTGRES_HOST,
    POSTGRES_PORT, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD.

Guards (all enforced before any row is inserted):
    - --since-days N (default 90): rows older than this are never inserted.
    - Rows with timestamp <= max(timestamp) of existing non-manual
      ('http'/'mqtt'/'snmp'/'feed') rows are skipped -- this tool only fills
      the gap *after* the newest row already inserted by the live pipeline,
      it never duplicates data the live pipeline already has.
    - Abort (exit 1) if any ingestion_source='manual' rows already exist,
      unless --force is passed -- prevents accidentally double-backfilling.
    - --dry-run never opens a database connection at all: it reports
      per-file parsed/rejected counts and a few sample rows, gated only by
      --since-days (the max-non-manual-timestamp guard requires a live DB
      query and so cannot be previewed in dry-run mode).

--max-timestamp EPOCH (guard override): the live max-non-manual-timestamp
    guard above is a live, unscoped `MAX(timestamp) WHERE ingestion_source
    <> 'manual'` query. If the outage that necessitated this backfill has
    already been fixed AND the live pipeline has already resumed inserting
    *before* the backfill runs, that live query returns a recent "now"-ish
    timestamp -- newer than the entire historical gap -- which would then
    cause every historical row to fail the `timestamp <= max` guard and be
    silently skipped (skipped_old), even though none of them were ever
    actually inserted. Pass --max-timestamp with the last known-good
    pre-outage epoch (e.g. queried directly against the DB filtered to
    before the fix/restart) to use that fixed boundary instead of the live
    query, restoring the guard's intended meaning.
"""

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, execute_values


# ---------------------------------------------------------------------------
# Alert payload constants -- mirrors CLAUDE.md's fixed level/category lists
# and sql/init/01-schema.sql's chk_level / chk_category CHECK constraints.
# ---------------------------------------------------------------------------

VALID_LEVELS = {"critical", "warning", "notice", "info", "debug"}
VALID_CATEGORIES = {
    "security", "system", "weather", "automation", "personal",
    "network", "application", "news", "business", "hardware", "environmental",
}

# Marks every row this tool inserts, forever (passes chk_ingestion_source;
# see sql/init/01-schema.sql). Also doubles as the "have we already run"
# guard signal for the abort-unless-force check.
INGESTION_SOURCE = "manual"


# ---------------------------------------------------------------------------
# Parser -- pure, standalone, importable. This is the piece
# tests/test_backfill_parser.py exercises directly with golden lines.
# ---------------------------------------------------------------------------

# Matches the line format written by pathways/file_pathway.py using the
# `format` template in data/config/docker.json's `file_log` pathway:
#   "{timestamp} [{level}] [{category}] {source} [{client}]: {title} - {message}"
#
# `source` is matched non-greedily so a source value that happens to contain
# "[...]" itself (sanitization does not strip brackets, only newlines/tabs/
# runs of whitespace -- see pathways/base_pathway.py's
# sanitize_for_single_line) isn't misread as the client bracket, *provided*
# it isn't also immediately followed by "]: " -- see parse_log_line's
# docstring for the one accepted ambiguity here (same spirit as the accepted
# first-" - "-wins ambiguity between title and message).
_LOG_LINE_RE = re.compile(
    r"^(?P<timestamp>\d{10}) "
    r"\[(?P<level>[^\]]+)\] "
    r"\[(?P<category>[^\]]+)\] "
    r"(?P<source>.+?) "
    r"\[(?P<client>[^\]]*)\]: "
    r"(?P<rest>.+)$"
)


def parse_log_line(line: str) -> Optional[dict]:
    """
    Parse one line written by pathways/file_pathway.py's FilePathway (the
    docker.json `file_log` pathway's format) into:

        {timestamp: int, level: str, category: str, source: str,
         client: str | None, title: str, message: str}

    Returns None if the line doesn't match the expected shape at all, or if
    it matches but `level`/`category` aren't in the fixed schema lists. The
    CALLER is responsible for counting/logging rejects with file:line
    context (see _classify_reject / process_file below) -- this function
    stays pure and silent by design so it's trivially unit-testable.

    `client` is returned as None both when the line's own [client] bracket
    is empty (true for every line in a flat, clientless file) and would
    also be None-ish for a missing bracket -- but note that for per-client
    files the caller MUST override this with the folder name regardless of
    what this function returns ("folder name wins" -- see discover_log_files
    / process_file), since the path is authoritative over the line content.

    Known accepted ambiguity: pathways/base_pathway.py's
    sanitize_for_single_line() does not escape " - " inside title/message,
    so if `title` itself happens to contain " - ", the split below (on the
    *first* " - ") can attribute part of the real title to `message`. This
    mirrors the plan's explicitly accepted parser limitation and is not
    something a regex alone can resolve without a distinct delimiter in the
    original format string.
    """
    if not line:
        return None

    stripped = line.rstrip("\r\n")
    if not stripped:
        return None

    match = _LOG_LINE_RE.match(stripped)
    if not match:
        return None

    level = match.group("level")
    category = match.group("category")
    if level not in VALID_LEVELS or category not in VALID_CATEGORIES:
        return None

    rest = match.group("rest")
    if " - " not in rest:
        return None
    title, message = rest.split(" - ", 1)

    return {
        "timestamp": int(match.group("timestamp")),
        "level": level,
        "category": category,
        "source": match.group("source"),
        "client": match.group("client") or None,
        "title": title,
        "message": message,
    }


def _classify_reject(line: str) -> str:
    """
    Best-effort human-readable reason parse_log_line(line) returned None,
    for process_file's file:line diagnostic logging. Reuses the same regex/
    validity sets as parse_log_line, but is not itself part of the parsing
    contract under test -- parse_log_line() alone is what
    tests/test_backfill_parser.py verifies for correctness. This exists
    only so log messages are more useful than a bare "skipped line".
    """
    if not line:
        return "empty line"
    stripped = line.rstrip("\r\n")
    if not stripped:
        return "empty line"

    match = _LOG_LINE_RE.match(stripped)
    if not match:
        return "line does not match expected format"

    level = match.group("level")
    category = match.group("category")
    if level not in VALID_LEVELS:
        return f"invalid level {level!r}"
    if category not in VALID_CATEGORIES:
        return f"invalid category {category!r}"
    if " - " not in match.group("rest"):
        return "missing ' - ' title/message separator"
    return "unrecognized"


# ---------------------------------------------------------------------------
# File discovery -- walks data/logs/file/ for the two known on-disk shapes.
# ---------------------------------------------------------------------------

_DATE_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")


@dataclass(frozen=True)
class LogFile:
    path: Path
    # Folder-derived client name, or None for a flat (clientless) file.
    # "Folder name wins": this value overrides whatever a per-client file's
    # own lines parse as their [client] bracket.
    client: Optional[str]


def discover_log_files(logs_root: Path, since_days: int, now: Optional[datetime] = None) -> List[LogFile]:
    """
    Discover file-pathway log files under logs_root/file/.

    Two on-disk shapes (see CLAUDE.md's Directory Structure and
    data/config/docker.json's `file_log` pathway, the only one actually
    wired into docker-compose.yml):
      - flat:       file/{date}.log             -> client=None
      - per-client: file/{client}/{date}.log     -> client={client} (folder name)

    Any first-level subdirectory of file/ is treated as a client folder --
    this also naturally picks up file/unauthenticated/{date}.log (SNMP
    traps without valid auth per CLAUDE.md), which isn't really a "client"
    but is physically identical in shape and gets backfilled with
    client="unauthenticated" like any other per-client folder.

    since_days is applied here only as a coarse file-level pre-filter (to
    avoid opening months-old files at all): a file is kept if its own
    {date} name is within since_days + 7 days of `now` -- the +7 grace
    margin exists because weekly per-client files are named for the Monday
    they were opened on, so a file's last line can be up to 6 days newer
    than its own filename date. The real per-row filter is each row's own
    timestamp, applied later by row_passes_guards() -- this function only
    decides which files get opened at all.
    """
    file_root = logs_root / "file"
    if not file_root.is_dir():
        return []

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff_date = (now - timedelta(days=since_days + 7)).date()

    results: List[LogFile] = []

    for entry in sorted(file_root.iterdir()):
        if entry.is_file():
            m = _DATE_FILENAME_RE.match(entry.name)
            if not m:
                continue
            if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff_date:
                continue
            results.append(LogFile(path=entry, client=None))

        elif entry.is_dir():
            client = entry.name
            for sub in sorted(entry.iterdir()):
                if not sub.is_file():
                    continue
                m = _DATE_FILENAME_RE.match(sub.name)
                if not m:
                    continue
                if datetime.strptime(m.group(1), "%Y-%m-%d").date() < cutoff_date:
                    continue
                results.append(LogFile(path=sub, client=client))

    return results


# ---------------------------------------------------------------------------
# Row-level guards
# ---------------------------------------------------------------------------

def row_passes_guards(timestamp: int, since_epoch: int, max_non_manual_epoch: Optional[int]) -> bool:
    """
    True if a parsed row should be inserted:
      - timestamp is within the --since-days window (timestamp >= since_epoch), and
      - timestamp is strictly newer than the newest existing non-manual row,
        i.e. not already represented by the live http/mqtt/snmp/feed
        pipeline (or there are no non-manual rows in the table yet at all).
    """
    if timestamp < since_epoch:
        return False
    if max_non_manual_epoch is not None and timestamp <= max_non_manual_epoch:
        return False
    return True


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_connection_kwargs() -> dict:
    """
    Build psycopg2 connection kwargs from POSTGRES_* env vars. Mirrors
    src/db_migrate.py's get_connection_kwargs() / src/database_logger.py's
    _create_db_pool().
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


def count_manual_rows(conn) -> bool:
    """True if any ingestion_source='manual' rows already exist in `alerts`."""
    with conn.cursor() as cur:
        cur.execute("SELECT EXISTS (SELECT 1 FROM alerts WHERE ingestion_source = 'manual')")
        (exists,) = cur.fetchone()
    return bool(exists)


def get_max_non_manual_timestamp(conn) -> Optional[int]:
    """
    Epoch seconds of the newest row NOT inserted by this tool (i.e. the
    newest row the live http/mqtt/snmp/feed pipeline actually managed to
    insert), or None if no such rows exist (empty table, or only manual
    rows present).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM MAX(timestamp))::BIGINT FROM alerts WHERE ingestion_source <> 'manual'"
        )
        (max_epoch,) = cur.fetchone()
    return int(max_epoch) if max_epoch is not None else None


# Shared INSERT shape -- deliberately mirrors src/database_logger.py's
# _INSERT_COLUMNS / _execute_batch_insert (same column list/order, same
# to_timestamp(%s) mapping for the epoch -> TIMESTAMPTZ column).
_INSERT_SQL = """
    INSERT INTO alerts (
        timestamp, level, category, title, message, source, client,
        metadata, display_config, routing, ingestion_source
    ) VALUES %s
"""
_INSERT_TEMPLATE = "(to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"


def _row_to_values(row: dict, source_file: str) -> tuple:
    """
    Map one parsed+guarded row dict, plus the file it came from, to the
    positional insert tuple. `metadata` always records provenance
    (backfilled_from); `display_config`/`routing` are always NULL since
    file-log lines never carried that data in the first place.
    """
    return (
        row["timestamp"],
        row["level"],
        row["category"],
        row["title"],
        row["message"],
        row["source"],
        row["client"],
        Json({"backfilled_from": source_file}),
        None,  # display_config
        None,  # routing
        INGESTION_SOURCE,
    )


def insert_batch(conn, rows: List[dict], source_file: str) -> int:
    """Batched insert of already-parsed, already-guard-passed rows."""
    if not rows:
        return 0
    values = [_row_to_values(row, source_file) for row in rows]
    with conn.cursor() as cur:
        execute_values(cur, _INSERT_SQL, values, template=_INSERT_TEMPLATE, page_size=len(values))
    conn.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def process_file(
    conn,
    log_file: LogFile,
    since_epoch: int,
    max_non_manual_epoch: Optional[int],
    batch_size: int,
    logger: logging.Logger,
) -> Dict[str, int]:
    """
    Stream-parse one log file, applying guards row-by-row and inserting in
    chunks of `batch_size` (rather than loading the whole file/table into
    memory) so a single very large per-client weekly file doesn't blow up
    memory. A chunk insert failure is logged and the run continues with the
    next chunk/file -- this is a one-off tool the operator can re-run with
    --force after fixing whatever broke, so it isn't worth the row-by-row
    fallback machinery src/database_logger.py needs for a long-running
    service.
    """
    stats = {"parsed": 0, "rejected": 0, "skipped_old": 0, "inserted": 0}
    file_str = str(log_file.path)
    chunk: List[dict] = []

    def flush():
        nonlocal chunk
        if not chunk:
            return
        try:
            stats["inserted"] += insert_batch(conn, chunk, file_str)
        except Exception as e:
            conn.rollback()
            logger.error(f"Insert failed for {file_str} (chunk of {len(chunk)} rows): {e}")
        chunk = []

    try:
        with log_file.path.open("r", encoding="utf-8", errors="replace") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                line = raw_line.rstrip("\n")
                if not line:
                    continue

                parsed = parse_log_line(line)
                if parsed is None:
                    stats["rejected"] += 1
                    logger.warning(f"{file_str}:{lineno}: skipping unparsed line ({_classify_reject(line)})")
                    continue

                stats["parsed"] += 1
                if not row_passes_guards(parsed["timestamp"], since_epoch, max_non_manual_epoch):
                    stats["skipped_old"] += 1
                    continue

                # Folder name wins over whatever the line itself parsed.
                parsed["client"] = log_file.client
                chunk.append(parsed)

                if len(chunk) >= batch_size:
                    flush()
    except OSError as e:
        logger.error(f"Failed to read {file_str}: {e}")
        # Flush whatever was already accumulated before the read failure --
        # no reason to discard rows that were successfully parsed just
        # because a later line in the same file couldn't be read.
        flush()
        return stats

    flush()

    logger.info(
        f"{file_str}: parsed={stats['parsed']} rejected={stats['rejected']} "
        f"skipped_old={stats['skipped_old']} inserted={stats['inserted']}"
    )
    return stats


def run_dry_run(log_files: List[LogFile], since_epoch: int, sample_size: int, logger: logging.Logger) -> None:
    """
    Report per-file parsed/rejected/in-window counts and a handful of
    sample parsed rows, without ever opening a database connection.

    NOTE: only the --since-days guard is previewable here. The
    max(non-manual timestamp) guard requires a live DB query and so is NOT
    reflected in these counts -- a real (non-dry-run) run may insert fewer
    rows than "in_window" below shows.
    """
    samples: List[str] = []
    totals = {"parsed": 0, "rejected": 0, "in_window": 0}

    for log_file in log_files:
        file_parsed = 0
        file_rejected = 0
        file_in_window = 0

        try:
            with log_file.path.open("r", encoding="utf-8", errors="replace") as fh:
                for lineno, raw_line in enumerate(fh, start=1):
                    line = raw_line.rstrip("\n")
                    if not line:
                        continue
                    parsed = parse_log_line(line)
                    if parsed is None:
                        file_rejected += 1
                        continue
                    file_parsed += 1
                    if parsed["timestamp"] < since_epoch:
                        continue
                    file_in_window += 1
                    parsed["client"] = log_file.client
                    if len(samples) < sample_size:
                        samples.append(f"  {log_file.path}:{lineno}: {parsed}")
        except OSError as e:
            logger.error(f"Failed to read {log_file.path}: {e}")
            continue

        print(f"{log_file.path}: parsed={file_parsed} rejected={file_rejected} in_window={file_in_window}")
        totals["parsed"] += file_parsed
        totals["rejected"] += file_rejected
        totals["in_window"] += file_in_window

    print(
        f"\nTOTAL: files={len(log_files)} parsed={totals['parsed']} "
        f"rejected={totals['rejected']} in_window={totals['in_window']}"
    )
    print(
        "(dry-run applies --since-days only; it does not connect to the database, "
        "so the max(non-manual timestamp) guard is not reflected above -- "
        "a real run may insert fewer rows than 'in_window'.)"
    )
    print(f"\nSample parsed rows (up to {sample_size}):")
    for line in samples:
        print(line)


def run(
    logs_root: Path,
    since_days: int = 90,
    dsn: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
    batch_size: int = 500,
    sample_size: int = 5,
    skip_refresh: bool = False,
    max_timestamp_override: Optional[int] = None,
    logger: Optional[logging.Logger] = None,
) -> int:
    """Orchestrate a full backfill run. Returns a process exit code (0 or 1)."""
    if logger is None:
        logger = logging.getLogger("backfill_file_logs")

    now = datetime.now(timezone.utc)
    since_epoch = int((now - timedelta(days=since_days)).timestamp())

    log_files = discover_log_files(logs_root, since_days, now=now)
    logger.info(f"Discovered {len(log_files)} log file(s) under {logs_root / 'file'}")

    if dry_run:
        run_dry_run(log_files, since_epoch, sample_size, logger)
        return 0

    if not log_files:
        logger.warning("No log files found under the --since-days window")

    try:
        conn = connect(dsn)
    except Exception as e:
        logger.error(f"Failed to connect to database: {e}")
        return 1

    try:
        # This guard runs before any file processing regardless of whether
        # log_files is empty, so a second invocation always aborts loudly
        # rather than silently no-op'ing past the check.
        if count_manual_rows(conn) and not force:
            logger.error(
                "Rows with ingestion_source='manual' already exist in the alerts table -- "
                "refusing to run again to avoid duplicate backfill data. Pass --force to override."
            )
            return 1

        if max_timestamp_override is not None:
            max_non_manual_epoch = max_timestamp_override
            bound_desc = f" AND timestamp > {max_non_manual_epoch} (--max-timestamp override, live query skipped)"
        else:
            max_non_manual_epoch = get_max_non_manual_timestamp(conn)
            bound_desc = (
                f" AND timestamp > {max_non_manual_epoch} (max existing non-manual timestamp)"
                if max_non_manual_epoch is not None
                else " (no existing non-manual rows -- no upper guard)"
            )
        logger.info(f"Guards: timestamp >= {since_epoch} (--since-days {since_days})" + bound_desc)

        totals = {"parsed": 0, "rejected": 0, "skipped_old": 0, "inserted": 0}
        for log_file in log_files:
            file_stats = process_file(conn, log_file, since_epoch, max_non_manual_epoch, batch_size, logger)
            for key in totals:
                totals[key] += file_stats[key]

        logger.info(
            f"Backfill complete: files={len(log_files)} parsed={totals['parsed']} "
            f"rejected={totals['rejected']} skipped_old={totals['skipped_old']} inserted={totals['inserted']}"
        )

        if not skip_refresh:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT refresh_alert_summary()")
                    result = cur.fetchone()
                conn.commit()
                logger.info(f"refresh_alert_summary(): {result[0] if result else 'no result'}")
            except Exception as e:
                conn.rollback()
                logger.warning(f"refresh_alert_summary() failed (non-fatal, rows are still inserted): {e}")

        return 0
    finally:
        conn.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="One-off backfill of pathways/file_pathway.py alert logs into the Postgres alerts table."
    )
    parser.add_argument(
        "--logs-root", default="/data/logs",
        help="Root directory containing a file/ subdirectory of alert log files (default: /data/logs)",
    )
    parser.add_argument(
        "--since-days", type=int, default=90,
        help="Only backfill alerts from the last N days (default: 90)",
    )
    parser.add_argument(
        "--dsn", help="Full libpq connection string/URI (overrides POSTGRES_* env vars)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Parse and report per-file counts/samples without connecting to or writing to the database",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Proceed even if ingestion_source='manual' rows already exist in the alerts table",
    )
    parser.add_argument(
        "--batch-size", type=int, default=500,
        help="Rows per execute_values() insert call (default: 500)",
    )
    parser.add_argument(
        "--sample-size", type=int, default=5,
        help="Number of sample parsed rows to print in --dry-run mode (default: 5)",
    )
    parser.add_argument(
        "--skip-refresh", action="store_true",
        help="Skip the refresh_alert_summary() call after a successful backfill",
    )
    parser.add_argument(
        "--max-timestamp", type=int, default=None,
        help=(
            "Override the max-non-manual-timestamp guard with this epoch-seconds value "
            "instead of live-querying it. Use when the live pipeline has already resumed "
            "inserting before this backfill runs (its live query would otherwise reflect "
            "post-fix timestamps newer than the entire historical gap, silently skipping it)."
        ),
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logger = logging.getLogger("backfill_file_logs")

    return run(
        logs_root=Path(args.logs_root),
        since_days=args.since_days,
        dsn=args.dsn,
        dry_run=args.dry_run,
        force=args.force,
        batch_size=args.batch_size,
        sample_size=args.sample_size,
        skip_refresh=args.skip_refresh,
        max_timestamp_override=args.max_timestamp,
        logger=logger,
    )


if __name__ == "__main__":
    sys.exit(main())
