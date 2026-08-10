"""
Pure-Python mirror of the ISO-week partition math in
sql/migrations/0001_partition_management.sql's ensure_alert_partitions().

That SQL function is the source of truth; this module exists purely so
tests/test_partition_math.py can verify the Monday-anchored ISO week/year
boundary logic (including the Dec 31 / Jan 1 ISO year-rollover and 53-week
ISO years) without a live Postgres connection for the common case. It
reimplements only the *date arithmetic* -- not the DB side (existence
checks against pg_tables, DDL, idempotency bookkeeping).

Must be kept in sync with sql/migrations/0001_partition_management.sql if
that function's date math ever changes:

  - week_monday()     mirrors DATE_TRUNC('week', p_from)::DATE
  - partition_name()  mirrors 'alerts_' || EXTRACT(ISOYEAR FROM v_week) ||
                       '_w' || LPAD(EXTRACT(WEEK FROM v_week)::TEXT, 2, '0')
  - plan_partitions() mirrors the function's WHILE v_week <= v_end loop,
                       including v_end being anchored to `today`
                       (CURRENT_DATE in SQL), not to p_from

Python's date.isocalendar() returns (ISO year, ISO week, ISO weekday) per
ISO 8601 -- the same standard Postgres's EXTRACT(ISOYEAR ...) /
EXTRACT(WEEK ...) implement (a week's ISO year is the year of its
Thursday), so the two independently-arrived-at implementations should
agree exactly.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import List, NamedTuple, Optional


class PartitionPlanEntry(NamedTuple):
    """One row of what ensure_alert_partitions(p_from, p_weeks_ahead) would
    report: the partition name and its [range_start, range_end) bounds."""
    name: str
    range_start: date
    range_end: date  # exclusive, always range_start + 7 days


def week_monday(d: date) -> date:
    """Monday of the ISO week containing `d`. Mirrors
    DATE_TRUNC('week', d)::DATE (Postgres's week truncation is Monday-anchored)."""
    return d - timedelta(days=d.isoweekday() - 1)


def partition_name(monday: date) -> str:
    """`alerts_{ISO year}_w{ISO week:02d}` for the week starting at `monday`.

    `monday` should already be week-truncated (i.e. the output of
    week_monday()) -- this function does not truncate for you, matching how
    the SQL only computes the EXTRACT(...) expression on an already-truncated
    v_week.
    """
    iso_year, iso_week, _ = monday.isocalendar()
    return f"alerts_{iso_year}_w{iso_week:02d}"


def plan_partitions(
    p_from: Optional[date],
    p_weeks_ahead: Optional[int] = 8,
    today: Optional[date] = None,
) -> List[PartitionPlanEntry]:
    """Mirrors ensure_alert_partitions(p_from, p_weeks_ahead)'s WHILE loop:
    walks Monday-anchored weeks from week_monday(p_from) through
    week_monday(today) + p_weeks_ahead*7 weeks, inclusive, returning the
    partition name and [start, end) range for each week.

    `today` defaults to date.today() but is exposed as a parameter so tests
    can pin it and get deterministic results (mirrors CURRENT_DATE in the
    SQL, which -- like here -- anchors v_end regardless of how far p_from is
    in the past or future; see plan_partitions' behavior when p_from is
    later than `today` + p_weeks_ahead weeks, below).

    Raises ValueError on None inputs, mirroring the SQL function's explicit
    `RAISE EXCEPTION` NULL guards on both parameters.

    If p_from's week is already after v_end (e.g. p_from is far enough in
    the future relative to `today`), the loop never executes and this
    returns an empty list -- exactly like the SQL function, since v_end is
    anchored to `today`, not to p_from.
    """
    if p_from is None:
        raise ValueError("p_from must not be None")
    if p_weeks_ahead is None:
        raise ValueError("p_weeks_ahead must not be None")

    if today is None:
        today = date.today()

    v_week = week_monday(p_from)
    v_end = week_monday(today) + timedelta(days=p_weeks_ahead * 7)

    entries = []
    while v_week <= v_end:
        entries.append(
            PartitionPlanEntry(
                name=partition_name(v_week),
                range_start=v_week,
                range_end=v_week + timedelta(days=7),
            )
        )
        v_week += timedelta(days=7)

    return entries
