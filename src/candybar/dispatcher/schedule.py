"""Pure UTC schedule evaluation shared by the dispatcher and future read API.

All fire-time math is UTC (AD-13) over standard five-field cron via croniter.
Nothing here reads the wall clock: callers pass the tick minute they consumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

from croniter import croniter

ONE_MINUTE = timedelta(minutes=1)


def _require_utc_minute(minute: datetime, what: str = "tick minute") -> None:
    if minute.tzinfo is None or minute.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{what} must be UTC")
    if minute.second or minute.microsecond:
        raise ValueError(f"{what} must be on a minute boundary")


def _require_five_fields(schedule: str) -> None:
    if len(schedule.split()) != 5:
        raise ValueError("schedule must have five fields")


def matches_tick(schedule: str, minute: datetime) -> bool:
    """Match a standard five-field cron expression against a UTC tick minute."""
    _require_utc_minute(minute)
    _require_five_fields(schedule)
    return croniter.match(schedule, minute.astimezone(UTC))


def ceil_minute(instant: datetime) -> datetime:
    """The first minute boundary at or after `instant` (UTC)."""
    instant = instant.astimezone(UTC)
    floor = instant.replace(second=0, microsecond=0)
    return floor if floor == instant else floor + ONE_MINUTE


def previous_fire(schedule: str, before: datetime) -> datetime:
    """The latest fire time strictly before the UTC minute `before`."""
    _require_utc_minute(before)
    _require_five_fields(schedule)
    fire = croniter(schedule, before.astimezone(UTC)).get_prev(datetime)
    return fire.astimezone(UTC)


def fires_between(schedule: str, start: datetime, end: datetime) -> Iterator[datetime]:
    """Fire times `start <= t < end` in ascending order (UTC minute bounds)."""
    _require_utc_minute(start, "window start")
    _require_utc_minute(end, "window end")
    _require_five_fields(schedule)
    if start >= end:
        return
    # get_next is strict, so seed one minute before `start` to include it.
    it = croniter(schedule, (start - ONE_MINUTE).astimezone(UTC))
    while True:
        fire = it.get_next(datetime).astimezone(UTC)
        if fire >= end:
            return
        yield fire
