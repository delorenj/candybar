"""Pure UTC schedule evaluation shared by the dispatcher and future read API.

All fire-time math is UTC (AD-13) over standard five-field cron via croniter.
Nothing here reads the wall clock: callers pass the tick minute they consumed.

Operator skips (Stories 2.3/2.4) are applied here too: the fire-time helpers
take an optional per-candybar `SkipSet` and omit the fires it covers, so the
dispatcher and the read API (Epic 5) compute the same skip-aware fires.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from uuid import UUID

from croniter import CroniterBadDateError, croniter

from ..registry.models import SKIP_OCCURRENCE, SKIP_REST_OF_DAY

ONE_MINUTE = timedelta(minutes=1)


class SkipRow(Protocol):
    """The fields of a registry skip row the helpers read (`SkipEntry`)."""

    candybar_id: UUID
    kind: str
    fire_time: datetime | None
    day: date | None


@dataclass(frozen=True)
class SkipSet:
    """The operator skips of ONE candybar (registry `skips` rows).

    `reason_for(fire)` is the `skipped` event reason (the skip kind) when an
    override covers that fire, else None. An occurrence skip names one fire;
    a rest-of-day skip (Story 2.4) covers its whole UTC date. When both cover
    one fire the occurrence reason wins — the more specific override.
    """

    occurrences: frozenset[datetime] = frozenset()
    rest_of_day: frozenset[date] = frozenset()

    def reason_for(self, fire: datetime) -> str | None:
        if fire in self.occurrences:
            return SKIP_OCCURRENCE
        if fire.date() in self.rest_of_day:
            return SKIP_REST_OF_DAY
        return None

    def covers(self, fire: datetime) -> bool:
        return self.reason_for(fire) is not None

    def __bool__(self) -> bool:
        return bool(self.occurrences) or bool(self.rest_of_day)


NO_SKIPS = SkipSet()


def group_skips(rows: Iterable[SkipRow]) -> dict[UUID, SkipSet]:
    """Fold registry skip rows into one SkipSet per candybar id.

    Unknown kinds are ignored, never guessed at: a newer daemon's override a
    rolled-back daemon cannot interpret must not suppress or wedge dispatch.
    """
    occurrences: dict[UUID, set[datetime]] = {}
    days: dict[UUID, set[date]] = {}
    for row in rows:
        if row.kind == SKIP_OCCURRENCE and row.fire_time is not None:
            occurrences.setdefault(row.candybar_id, set()).add(row.fire_time.astimezone(UTC))
        elif row.kind == SKIP_REST_OF_DAY and row.day is not None:
            days.setdefault(row.candybar_id, set()).add(row.day)
    ids = occurrences.keys() | days.keys()
    return {
        cid: SkipSet(
            occurrences=frozenset(occurrences.get(cid, ())),
            rest_of_day=frozenset(days.get(cid, ())),
        )
        for cid in ids
    }


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


def previous_fire(
    schedule: str, before: datetime, *, skips: SkipSet = NO_SKIPS
) -> datetime | None:
    """The latest non-skipped fire time strictly before the UTC minute `before`.

    None when the schedule never fires: a syntactically valid expression such
    as `0 0 30 2 *` (Feb 30) passes registration, and croniter raises
    CroniterBadDateError searching for it — that must read as "nothing
    missed", never as a dispatch failure that wedges the tick. Fires covered
    by `skips` are walked past (a finite set, so the walk terminates).
    """
    _require_utc_minute(before)
    _require_five_fields(schedule)
    it = croniter(schedule, before.astimezone(UTC))
    while True:
        try:
            fire = it.get_prev(datetime).astimezone(UTC)
        except CroniterBadDateError:
            return None
        if not skips.covers(fire):
            return fire


def fires_between(
    schedule: str, start: datetime, end: datetime, *, skips: SkipSet = NO_SKIPS
) -> Iterator[datetime]:
    """Non-skipped fire times `start <= t < end`, ascending (UTC minute bounds)."""
    _require_utc_minute(start, "window start")
    _require_utc_minute(end, "window end")
    _require_five_fields(schedule)
    if start >= end:
        return
    # get_next is strict, so seed one minute before `start` to include it.
    it = croniter(schedule, (start - ONE_MINUTE).astimezone(UTC))
    while True:
        try:
            fire = it.get_next(datetime).astimezone(UTC)
        except CroniterBadDateError:  # never fires again
            return
        if fire >= end:
            return
        if not skips.covers(fire):
            yield fire


def next_fire(schedule: str, at: datetime, *, skips: SkipSet = NO_SKIPS) -> datetime | None:
    """The upcoming fire: the first non-skipped fire time `>= at` (UTC minute).

    The read API's upcoming-fire computation (AD-13): the same croniter
    evaluation the dispatcher matches ticks with, honoring skips. None when
    the schedule never fires again.

    Bounded (Story 5.3): a fire on a rest-of-day date jumps straight to the
    next UTC midnight, since that whole date is covered, so a skipped day
    costs one croniter step rather than one per fire (1,440 for a minutely
    schedule). Occurrence skips are stepped past one by one.
    """
    _require_utc_minute(at, "from minute")
    _require_five_fields(schedule)
    it = croniter(schedule, (at - ONE_MINUTE).astimezone(UTC))
    while True:
        try:
            fire = it.get_next(datetime).astimezone(UTC)
        except CroniterBadDateError:
            return None
        if fire.date() in skips.rest_of_day:
            # get_next is strict: seed one minute before midnight to include it.
            midnight = datetime.combine(fire.date() + timedelta(days=1), time(0), tzinfo=UTC)
            it = croniter(schedule, midnight - ONE_MINUTE)
            continue
        if not skips.covers(fire):
            return fire
