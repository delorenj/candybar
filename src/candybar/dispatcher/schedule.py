"""Pure schedule evaluation shared by the dispatcher and the read API.

Ticks, fire instants and every window bound are UTC minutes (AD-13); a
candybar's cron is read as local wall time in its IANA timezone (AD-16, Epic
7, default UTC). Every fire instant comes from `firetimes.fire_times`, the one
DST-correct source: `matches_tick`, `fires_between`, `next_fire` and
`previous_fire` are thin views over it and take the candybar's `tz`.
Nothing here reads the wall clock: callers pass the tick minute they consumed.

Operator skips (Stories 2.3/2.4) are applied here too: the fire-time helpers
take an optional per-candybar `SkipSet` and omit the fires it covers, so the
dispatcher and the read API (Epic 5) compute the same skip-aware fires.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, tzinfo
from typing import Protocol
from uuid import UUID

from ..registry.models import SKIP_OCCURRENCE, SKIP_REST_OF_DAY
from .firetimes import (
    END_OF_TIME,
    ONE_DAY,
    ONE_MINUTE,
    fire_times,
    last_fire_before,
    local_date,
    local_day_start,
    require_five_fields,
    require_utc_minute,
)


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
    a rest-of-day skip (Story 2.4) covers its whole date, which is the date in
    the candybar's own timezone `tz` (AD-16; UTC for a UTC candybar). When
    both cover one fire the occurrence reason wins — the more specific
    override.
    """

    occurrences: frozenset[datetime] = frozenset()
    rest_of_day: frozenset[date] = frozenset()
    tz: tzinfo = UTC

    def day_of(self, fire: datetime) -> date:
        """The candybar-local calendar date of a fire instant."""
        return local_date(fire, self.tz)

    def reason_for(self, fire: datetime) -> str | None:
        if fire in self.occurrences:
            return SKIP_OCCURRENCE
        if self.rest_of_day and self.day_of(fire) in self.rest_of_day:
            return SKIP_REST_OF_DAY
        return None

    def covers(self, fire: datetime) -> bool:
        return self.reason_for(fire) is not None

    def __bool__(self) -> bool:
        return bool(self.occurrences) or bool(self.rest_of_day)


NO_SKIPS = SkipSet()


def group_skips(
    rows: Iterable[SkipRow], zones: Mapping[UUID, tzinfo] | None = None
) -> dict[UUID, SkipSet]:
    """Fold registry skip rows into one SkipSet per candybar id.

    `zones` maps a candybar id to its timezone, which decides what a
    rest-of-day date means; a candybar missing from it reads as UTC.
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
            tz=(zones or {}).get(cid, UTC),
        )
        for cid in ids
    }


def widen_skip_window(start: datetime, end: datetime | None) -> tuple[datetime, datetime | None]:
    """The skip-read window that is safe for candybars in ANY timezone.

    A rest-of-day row's date is a candybar-local date, which differs from the
    UTC date of the same instant by up to a day either way. Reading skips
    `[start - 1d, end + 1d)` therefore returns every day row that can cover a
    fire in `[start, end)` whatever the zone; the extra rows are inert
    (`SkipSet` decides precisely). `end=None` (open-ended) stays open.
    """
    return start - ONE_DAY, (end + ONE_DAY if end is not None else None)


def matches_tick(schedule: str, minute: datetime, tz: tzinfo = UTC) -> bool:
    """Whether the cron, read as wall time in `tz`, fires at the UTC tick minute."""
    require_utc_minute(minute)
    require_five_fields(schedule)
    return next(fire_times(schedule, tz, minute, minute + ONE_MINUTE), None) is not None


def ceil_minute(instant: datetime) -> datetime:
    """The first minute boundary at or after `instant` (UTC)."""
    instant = instant.astimezone(UTC)
    floor = instant.replace(second=0, microsecond=0)
    return floor if floor == instant else floor + ONE_MINUTE


def previous_fire(
    schedule: str, before: datetime, *, tz: tzinfo = UTC, skips: SkipSet = NO_SKIPS
) -> datetime | None:
    """The latest non-skipped fire time strictly before the UTC minute `before`.

    None when the schedule never fires: a syntactically valid expression such
    as `0 0 30 2 *` (Feb 30) passes registration and has no fire — that must
    read as "nothing missed", never as a dispatch failure that wedges the
    tick. Fires covered by `skips` are walked past (a finite set, so the walk
    terminates).
    """
    require_utc_minute(before)
    require_five_fields(schedule)
    return last_fire_before(schedule, tz, before, accept=lambda fire: not skips.covers(fire))


def fires_between(
    schedule: str,
    start: datetime,
    end: datetime,
    *,
    tz: tzinfo = UTC,
    skips: SkipSet = NO_SKIPS,
) -> Iterator[datetime]:
    """Non-skipped fire times `start <= t < end`, ascending (UTC minute bounds)."""
    require_utc_minute(start, "window start")
    require_utc_minute(end, "window end")
    require_five_fields(schedule)
    for fire in fire_times(schedule, tz, start, end):
        if not skips.covers(fire):
            yield fire


def next_fire(
    schedule: str, at: datetime, *, tz: tzinfo = UTC, skips: SkipSet = NO_SKIPS
) -> datetime | None:
    """The upcoming fire: the first non-skipped fire time `>= at` (UTC minute).

    The read API's upcoming-fire computation (AD-13): the same `fire_times`
    walk the dispatcher matches ticks with, honoring skips. None when the
    schedule never fires again.

    Bounded (Story 5.3): a fire on a rest-of-day date jumps straight to the
    start of the next local date, since that whole date is covered, so a
    skipped day costs one step rather than one per fire (1,440 for a minutely
    schedule). Occurrence skips are stepped past one by one.
    """
    require_utc_minute(at, "from minute")
    require_five_fields(schedule)
    start = at
    while True:
        jumped = False
        for fire in fire_times(schedule, tz, start, END_OF_TIME):
            if skips.rest_of_day and skips.day_of(fire) in skips.rest_of_day:
                start = local_day_start(skips.day_of(fire) + ONE_DAY, skips.tz)
                jumped = True
                break
            if not skips.covers(fire):
                return fire
        if not jumped:
            return None
