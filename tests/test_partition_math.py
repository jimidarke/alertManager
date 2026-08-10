"""
Tests for the ISO-week partition math behind
sql/migrations/0001_partition_management.sql's ensure_alert_partitions().

Two layers, following the pattern already established in test_db_migrate.py
(pure-logic tests run unconditionally; a single live-Postgres test is gated
on AM_TEST_DSN and skips cleanly when it isn't set):

1. Unconditional tests against tests/partition_math.py, a pure-Python
   mirror of the SQL's DATE_TRUNC('week', ...) / EXTRACT(ISOYEAR ...) /
   EXTRACT(WEEK ...) date math. These are the primary correctness signal
   and run in every environment, no DB required -- including for the tricky
   ISO year/week-53 boundary reasoning Task 2's reviewer verified by hand
   (see .superpowers/sdd/task-2-report.md's "Manual trace-through" section).
   They're deterministic because `today` is passed explicitly rather than
   read off the real wall clock.

   NOTE: this mirror lives under tests/, not src/utils/, specifically so it
   is test-only code that is never copied into the Docker image (it has no
   caller outside this test suite -- shipping it under src/ would imply it
   is live production code). See sql/migrations/0001_partition_management.sql's
   comment on ensure_alert_partitions for the back-reference the other
   direction.

2. A single live-Postgres test that applies the real migration file to a
   scratch DB and calls the actual SQL function (not the Python mirror), to
   confirm the two independently-written implementations agree, and that
   rows land in the correct physical partition on either side of the
   Dec 2026 / Jan 2027 ISO year-boundary week. Gated on AM_TEST_DSN exactly
   like test_db_migrate.py's test_run_end_to_end_against_real_postgres;
   skips cleanly with a clear reason when unset.

No live Postgres is available in this sandbox, so test (2) has never
actually executed here -- see task-6-report.md's concerns section.
"""

import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import partition_math  # noqa: E402


# ---------------------------------------------------------------------------
# week_monday() / partition_name(): the two atomic building blocks
# ---------------------------------------------------------------------------

def test_week_monday_is_a_noop_for_a_monday():
    assert partition_math.week_monday(date(2026, 3, 16)) == date(2026, 3, 16)


def test_week_monday_truncates_midweek_dates_back_to_monday():
    assert partition_math.week_monday(date(2026, 3, 18)) == date(2026, 3, 16)  # Wednesday
    assert partition_math.week_monday(date(2026, 3, 22)) == date(2026, 3, 16)  # Sunday


def test_partition_name_normal_week():
    # A plain mid-year week -- task instructions' "normal week" example.
    assert partition_math.partition_name(date(2026, 3, 16)) == "alerts_2026_w12"


def test_partition_name_matches_repair_call_first_week():
    # The migration's closing repair call starts from ensure_alert_partitions
    # ('2026-01-05', 8). 2026-01-05 is already a Monday and starts ISO week 2
    # of 2026 (ISO week 1 is Mon Dec 29 2025 - Sun Jan 4 2026, since Jan 1
    # 2026 is a Thursday).
    assert partition_math.partition_name(date(2026, 1, 5)) == "alerts_2026_w02"


def test_partition_name_iso_year_boundary_week_53():
    # The Dec 31 / Jan 1 boundary case Task 2 traced by hand: the week of
    # Mon Dec 28 2026 - Sun Jan 3 2027 is ISO week 53 of *2026* (named for
    # its Thursday, Dec 31 2026) even though its date range spans into
    # January 2027. 2026 is a 53-ISO-week year (Jan 1 2026 falls on a
    # Thursday).
    assert partition_math.partition_name(date(2026, 12, 28)) == "alerts_2026_w53"


def test_partition_name_first_week_of_next_iso_year():
    # The very next Monday after week 53 above starts ISO week 1 of 2027.
    assert partition_math.partition_name(date(2027, 1, 4)) == "alerts_2027_w01"


# ---------------------------------------------------------------------------
# plan_partitions(): full WHILE-loop mirror
# ---------------------------------------------------------------------------

def test_plan_partitions_rejects_none_p_from():
    with pytest.raises(ValueError):
        partition_math.plan_partitions(None, 8, today=date(2026, 7, 20))


def test_plan_partitions_rejects_none_weeks_ahead():
    with pytest.raises(ValueError):
        partition_math.plan_partitions(date(2026, 1, 5), None, today=date(2026, 7, 20))


def test_plan_partitions_matches_manual_trace_for_repair_call():
    # task-2-report.md's "Illustrative run against today" section, traced
    # entirely by hand: ensure_alert_partitions('2026-01-05', 8) run on
    # Monday 2026-07-20 walks 37 Mondays and the last is alerts_2026_w38
    # starting 2026-09-14.
    plan = partition_math.plan_partitions(date(2026, 1, 5), 8, today=date(2026, 7, 20))

    assert len(plan) == 37
    assert plan[0].name == "alerts_2026_w02"
    assert plan[0].range_start == date(2026, 1, 5)
    assert plan[-1].name == "alerts_2026_w38"
    assert plan[-1].range_start == date(2026, 9, 14)


def test_plan_partitions_spans_iso_year_boundary_contiguously():
    plan = partition_math.plan_partitions(date(2026, 12, 1), 5, today=date(2026, 12, 1))

    names = [e.name for e in plan]
    assert "alerts_2026_w53" in names
    assert "alerts_2027_w01" in names

    w53 = next(e for e in plan if e.name == "alerts_2026_w53")
    w01 = next(e for e in plan if e.name == "alerts_2027_w01")
    assert w53.range_start == date(2026, 12, 28)
    assert w53.range_end == date(2027, 1, 4)
    assert w01.range_start == date(2027, 1, 4)
    assert w01.range_end == date(2027, 1, 11)

    # No gap or overlap across the ISO-year rollover: w53's exclusive end is
    # exactly w01's start, despite the name jumping from 2026 to 2027.
    assert w53.range_end == w01.range_start


def test_plan_partitions_ranges_are_contiguous_no_gaps_or_overlaps():
    plan = partition_math.plan_partitions(date(2026, 1, 5), 8, today=date(2026, 7, 20))

    for prev_entry, cur_entry in zip(plan, plan[1:]):
        assert prev_entry.range_end == cur_entry.range_start
        assert (cur_entry.range_end - cur_entry.range_start) == timedelta(days=7)


def test_plan_partitions_v_end_anchored_to_today_not_p_from():
    # v_end = DATE_TRUNC('week', CURRENT_DATE) + p_weeks_ahead*7 -- anchored
    # to `today`, not `p_from` (task-2-report.md design decision #1). A
    # p_from far enough beyond today + p_weeks_ahead weeks produces an empty
    # plan (v_week starts past v_end), not an error.
    plan = partition_math.plan_partitions(date(2027, 6, 1), 8, today=date(2026, 7, 20))
    assert plan == []


# ---------------------------------------------------------------------------
# Live-DB test: does the real SQL function agree with the Python mirror?
# Gated on AM_TEST_DSN, skipped cleanly without a live Postgres available.
# ---------------------------------------------------------------------------

AM_TEST_DSN = os.environ.get("AM_TEST_DSN")

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent / "sql" / "migrations" / "0001_partition_management.sql"
)


@pytest.mark.skipif(not AM_TEST_DSN, reason="AM_TEST_DSN not set; no live Postgres available")
def test_ensure_alert_partitions_matches_python_mirror_and_routes_rows_correctly():
    import psycopg2

    migration_sql = _MIGRATION_PATH.read_text()

    conn = psycopg2.connect(AM_TEST_DSN)
    try:
        with conn.cursor() as cur:
            # Pin the session timezone so CURRENT_DATE / the boundary-insert
            # checks below are unambiguous regardless of the scratch
            # server's configured default (this project's containers run
            # with TZ=America/Edmonton; the partition math itself is
            # timezone-agnostic DATE arithmetic, but CURRENT_DATE and
            # TIMESTAMPTZ literal parsing are not).
            cur.execute("SET TIME ZONE 'UTC'")

            # Minimal stand-in for sql/init/01-schema.sql's alerts table --
            # only what ensure_alert_partitions() needs to attach partitions
            # to (partition key + PK), not the full production schema
            # (indexes, JSONB columns, check constraints are irrelevant
            # here).
            cur.execute("DROP TABLE IF EXISTS alerts CASCADE")
            cur.execute(
                """
                CREATE TABLE alerts (
                    id BIGSERIAL,
                    timestamp TIMESTAMPTZ NOT NULL,
                    PRIMARY KEY (id, timestamp)
                ) PARTITION BY RANGE (timestamp)
                """
            )
        conn.commit()

        with conn.cursor() as cur:
            # Applies the real migration file text -- function definition,
            # GRANT, and its closing repair call -- in one multi-statement
            # execute(), the same technique src/db_migrate.py's
            # apply_migration() uses. Assumes AM_TEST_DSN's role is named
            # `alertmanager` (this project's POSTGRES_USER convention;
            # required for the migration's `GRANT ... TO alertmanager` to
            # succeed).
            cur.execute(migration_sql)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT CURRENT_DATE")
            today = cur.fetchone()[0]

            # p_weeks_ahead is computed dynamically, not hardcoded, so this
            # test reaches the fixed 2027-01-04 checkpoint regardless of the
            # real date it happens to run on: v_end is anchored to
            # CURRENT_DATE, not p_from (see
            # partition_math.plan_partitions' docstring).
            this_monday = partition_math.week_monday(today)
            target_monday = date(2027, 1, 4)
            weeks_ahead = max(8, ((target_monday - this_monday).days // 7) + 1)

            cur.execute(
                "SELECT partition_name, status FROM ensure_alert_partitions(%s, %s)",
                (date(2026, 1, 5), weeks_ahead),
            )
            rows = dict(cur.fetchall())
        conn.commit()

        expected_plan = partition_math.plan_partitions(date(2026, 1, 5), weeks_ahead, today=today)
        expected_names = {entry.name for entry in expected_plan}

        # The SQL function's own set of created/pre-existing partition names
        # must exactly match what the independently-written Python mirror
        # predicts for the same (p_from, p_weeks_ahead, today).
        assert set(rows.keys()) == expected_names
        assert all(status in ("created", "already_exists") for status in rows.values())

        for checkpoint in ("alerts_2026_w02", "alerts_2026_w12", "alerts_2026_w53", "alerts_2027_w01"):
            assert checkpoint in rows

        with conn.cursor() as cur:
            # Boundary check: a row a second before the ISO year rollover
            # must physically land in the 2026 week-53 partition; a row
            # exactly at the rollover must land in the 2027 week-1
            # partition -- verifying the FOR VALUES FROM/TO bounds
            # themselves, not just the reported partition names.
            cur.execute(
                "INSERT INTO alerts (timestamp) VALUES (%s) RETURNING tableoid::regclass::text",
                ("2027-01-03 23:59:59+00",),
            )
            assert cur.fetchone()[0] == "alerts_2026_w53"

            cur.execute(
                "INSERT INTO alerts (timestamp) VALUES (%s) RETURNING tableoid::regclass::text",
                ("2027-01-04 00:00:00+00",),
            )
            assert cur.fetchone()[0] == "alerts_2027_w01"
        conn.commit()

        # Re-running with the same arguments must be a pure idempotent
        # no-op: every partition already exists now, none re-created.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT partition_name, status FROM ensure_alert_partitions(%s, %s)",
                (date(2026, 1, 5), weeks_ahead),
            )
            rerun_rows = dict(cur.fetchall())
        conn.commit()

        assert set(rerun_rows.keys()) == expected_names
        assert all(status == "already_exists" for status in rerun_rows.values())
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS alerts CASCADE")
        conn.commit()
        conn.close()
