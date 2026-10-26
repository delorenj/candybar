"""Wave 3 (Story 7.9) DST proof: the two migrated jobs that straddle a
transition keep their wall-clock cadence through it.

Simulated-clock executor test: instead of trusting one `fire_times` window,
walk EVERY dispatcher tick minute across the transition with `matches_tick`
(exactly what the dispatcher does per tick) and collect the ticks that fire.

- ifvault-nightly   `40 1 * * *` America/New_York: 01:40 falls in the
  2026-11-01 repeated hour (both 01:40 EDT and 01:40 EST exist). Vixie
  semantics: fire once, at the FIRST occurrence (05:40Z), never twice.
- milkshake-ledger-backup `30 2 * * *` America/New_York: 02:30 does not
  exist on 2027-03-14 (spring-forward gap 02:00-03:00). Vixie semantics:
  fire once at the first existing minute after the gap, 03:00 EDT (07:00Z).
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from candybar.dispatcher.schedule import matches_tick

NY = ZoneInfo("America/New_York")
ONE_MINUTE = timedelta(minutes=1)

IFVAULT_SCHEDULE = "40 1 * * *"  # systemd:ifvault-nightly.timer
MILKSHAKE_SCHEDULE = "30 2 * * *"  # systemd:milkshake-ledger-backup.timer


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def fired_ticks(schedule: str, start: datetime, end: datetime) -> list[datetime]:
    """Every tick minute in [start, end) the dispatcher would match."""
    out = []
    tick = start
    while tick < end:
        if matches_tick(schedule, tick, NY):
            out.append(tick)
        tick += ONE_MINUTE
    return out


def test_ifvault_fires_exactly_once_in_the_repeated_hour() -> None:
    # The whole fall-back day: 2026-11-01 00:00Z through 2026-11-02 12:00Z.
    got = fired_ticks(IFVAULT_SCHEDULE, utc(2026, 11, 1), utc(2026, 11, 2, 12))
    # Once at the first occurrence (01:40 EDT = 05:40Z), NOT again at
    # 01:40 EST (06:40Z); the next day is back to normal (01:40 EST = 06:40Z).
    assert got == [utc(2026, 11, 1, 5, 40), utc(2026, 11, 2, 6, 40)]


def test_milkshake_fires_once_after_the_spring_forward_gap() -> None:
    got = fired_ticks(MILKSHAKE_SCHEDULE, utc(2027, 3, 13), utc(2027, 3, 15, 12))
    # Mar 13 02:30 EST (07:30Z) as usual; no 02:30 exists on Mar 14, so the
    # fire lands at the first existing minute after the gap: 03:00 EDT =
    # 07:00Z; Mar 15 02:30 EDT is 06:30Z.
    assert got == [utc(2027, 3, 13, 7, 30), utc(2027, 3, 14, 7, 0), utc(2027, 3, 15, 6, 30)]


def test_no_double_fire_anywhere_in_transition_weeks() -> None:
    # A wider net: the whole transition weeks for both jobs must fire exactly
    # once per day — no duplicates, no skipped days.
    for schedule in (IFVAULT_SCHEDULE, MILKSHAKE_SCHEDULE):
        fall = fired_ticks(schedule, utc(2026, 10, 26), utc(2026, 11, 9))
        spring = fired_ticks(schedule, utc(2027, 3, 8), utc(2027, 3, 22))
        for fires in (fall, spring):
            assert len(fires) == 14, (schedule, fires)
            assert len(set(fires)) == 14
            assert all(
                23 * 60 <= (b - a).total_seconds() / 60 <= 25 * 60
                for a, b in itertools.pairwise(fires)
            )
