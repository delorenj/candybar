"""DST-correct cron fire times: the ONE source of fire instants (AD-16).

A candybar's cron is evaluated as local wall time in its IANA timezone, and
`fire_times(schedule, tz, start_utc, end_utc)` yields the UTC instants it
fires in `[start_utc, end_utc)`. The dispatcher's tick match, the read API's
window/next rows, misfire's latest-fire and the AD-20 backlog check all call
it (through the thin wrappers in `schedule.py`), so no two components can
disagree about when a job fires.

Why not croniter on aware datetimes: croniter 6.2.4 fires `40 1 * * *` in
America/New_York twice on the fall-back night (it walks the repeated hour
twice) and `croniter.match` misses spring-forward gap fires that `get_next`
returns. So croniter is used ONLY for the pure naive wall-clock walk (no
timezone, hence no DST bugs) and this module maps each wall time to real
instants with the Vixie-cron rules:

* Repeated hour (fall back, wall time exists twice): a fixed-hour expression
  fires once, at the FIRST occurrence (`fold=0`). An expression whose hour
  field is `*` fires at both occurrences, so interval jobs
  (`*/30 * * * *`, `0 * * * *`) keep their real-time cadence.
* Gap (spring forward, wall time does not exist): a fixed-hour expression
  fires once at the first existing minute after the gap (the transition
  instant), however many of its wall times fell in the gap. A wildcard-hour
  expression does not catch up the skipped wall minutes.
* Everywhere else wall time maps one-to-one to UTC.

Everything here is pure: no wall clock, no I/O. Instants are timezone-aware
UTC on minute boundaries.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, time, timedelta, tzinfo

from croniter import CroniterBadDateError, croniter

ONE_MINUTE = timedelta(minutes=1)
ONE_DAY = timedelta(days=1)

# Offset probing: a transition can only hide between two probes that read the
# same offset if two happen within one step (an offset change and its reversal
# inside two days), which no zone does.
_PROBE_STEP = timedelta(days=2)
_PROBE_MARGIN = ONE_DAY

# Chunked laziness: the walk is done in UTC chunks that start small (a caller
# that wants the first fire pays for a couple of minutes, not an hour) and
# grow fast while they come back empty, so even a yearly cron is found in a
# dozen chunks. A chunk with fires grows slowly: its consumer is usually done.
_FIRST_CHUNK = timedelta(minutes=2)
_DENSE_FIRES = 64
_DENSE_CAP = ONE_DAY
_SPARSE_CAP = timedelta(days=400)

# The search horizon of an unbounded walk (croniter overflows at year 9999).
END_OF_TIME = datetime(9999, 12, 1, tzinfo=UTC)
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def require_utc_minute(minute: datetime, what: str = "tick minute") -> None:
    if minute.tzinfo is None or minute.utcoffset() != UTC.utcoffset(None):
        raise ValueError(f"{what} must be UTC")
    if minute.second or minute.microsecond:
        raise ValueError(f"{what} must be on a minute boundary")


def require_five_fields(schedule: str) -> None:
    if len(schedule.split()) != 5:
        raise ValueError("schedule must have five fields")


@functools.lru_cache(maxsize=2048)
def never_fires(schedule: str) -> bool:
    """True for a syntactically valid cron that has no fire at all (`0 0 30 2 *`).

    Such an expression passes registration, and croniter raises
    CroniterBadDateError searching for it: that is "nothing to fire", never a
    failure that wedges the tick. A wall-clock property, so timezone-free.
    """
    try:
        croniter(schedule, datetime(2026, 1, 1)).get_next(datetime)
    except CroniterBadDateError:
        return True
    return False


def hour_is_wildcard(schedule: str) -> bool:
    """Whether the hour field is `*` (the Vixie "repeat in the fall-back hour" rule)."""
    return schedule.split()[1] in ("*", "*/1")


def _offset(instant: datetime, tz: tzinfo) -> timedelta:
    offset = instant.astimezone(tz).utcoffset()
    assert offset is not None
    return offset


def _offset_range(tz: tzinfo, lo: datetime, hi: datetime) -> tuple[timedelta, timedelta]:
    """(min, max) UTC offset over `[lo - 1d, hi + 1d]`.

    Equal ends mean no transition within a day of the chunk, so every wall
    time in it maps to UTC by one constant shift (the fast path); unequal
    ends send the chunk through the full wall-to-instant mapping.
    """
    instant = lo - _PROBE_MARGIN
    stop = hi + _PROBE_MARGIN
    low = high = _offset(instant, tz)
    while instant < stop:
        instant = min(instant + _PROBE_STEP, stop)
        offset = _offset(instant, tz)
        low, high = min(low, offset), max(high, offset)
    return low, high


def _transition_after(before: datetime, after: datetime, tz: tzinfo) -> datetime:
    """The first minute at which `tz`'s offset differs from its offset at `before`.

    `before` and `after` bracket one transition (they are the two readings of
    a gap wall time, which straddle it). Bisection on whole minutes.
    """
    base = _offset(before, tz)
    lo = int(before.timestamp()) // 60
    hi = int(after.timestamp()) // 60
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if _offset(datetime.fromtimestamp(mid * 60, UTC), tz) == base:
            lo = mid
        else:
            hi = mid
    return datetime.fromtimestamp(hi * 60, UTC)


def _map_wall(wall: datetime, tz: tzinfo, hour_wild: bool) -> tuple[datetime, ...]:
    """The UTC instants a naive wall-clock fire becomes, under the Vixie rules."""
    first = wall.replace(tzinfo=tz, fold=0).astimezone(UTC)
    second = wall.replace(tzinfo=tz, fold=1).astimezone(UTC)
    if first == second:
        return (first,)
    if first.astimezone(tz).replace(tzinfo=None) == wall:
        # Repeated hour: both readings are real instants, `first` the earlier.
        return (first, second) if hour_wild else (first,)
    # Gap: the wall time never happens. fold=1 reads the offset after the
    # transition, which lands BEFORE it in real time (and fold=0 after it).
    if hour_wild:
        return ()
    return (_transition_after(second, first, tz),)


def _chunk(
    schedule: str, tz: tzinfo, lo: datetime, hi: datetime, hour_wild: bool
) -> tuple[list[datetime], datetime | None]:
    """Every fire in `[lo, hi)` (ascending, de-duplicated) and where to look next.

    The second value is a UTC instant that certainly covers the next wall-clock
    fire after this chunk (so an empty chunk tells the caller exactly how far
    to jump), or None when the cron has no further fire at all.
    """
    low, high = _offset_range(tz, lo, hi)
    # wall = utc + offset, so the wall times that can map into [lo, hi) are
    # [lo + low, hi + high). A gap wall time maps to the transition instant,
    # which keeps it inside this range too.
    naive_lo = lo.replace(tzinfo=None) + low
    naive_hi = hi.replace(tzinfo=None) + high
    walk = croniter(schedule, naive_lo - ONE_MINUTE)  # naive: no timezone, no DST bugs
    found: set[datetime] = set()
    constant = low == high
    reach = None
    while True:
        try:
            wall = walk.get_next(datetime)
        except CroniterBadDateError:
            break
        if wall >= naive_hi:
            # Every instant this wall time can become is <= wall - low.
            reach = (wall - low).replace(tzinfo=UTC) + ONE_MINUTE
            break
        if constant:
            found.add((wall - low).replace(tzinfo=UTC))
            continue
        for instant in _map_wall(wall, tz, hour_wild):
            if lo <= instant < hi:
                found.add(instant)
    return sorted(found), reach


def fire_times(
    schedule: str, tz: tzinfo, start_utc: datetime, end_utc: datetime
) -> Iterator[datetime]:
    """The UTC instants `schedule` fires in `[start_utc, end_utc)`, ascending.

    `schedule` is a standard 5-field cron read as local wall time in `tz`;
    both bounds are UTC minute boundaries. Lazy: the window is walked in
    chunks, so taking the first fire of a long window is cheap. A cron that
    never fires yields nothing. Raises ValueError on a malformed argument
    (immediately, not on first iteration).
    """
    require_five_fields(schedule)
    require_utc_minute(start_utc, "window start")
    require_utc_minute(end_utc, "window end")
    return _fire_times(schedule, tz, start_utc, end_utc)


def _fire_times(
    schedule: str, tz: tzinfo, start_utc: datetime, end_utc: datetime
) -> Iterator[datetime]:
    if start_utc >= end_utc or never_fires(schedule):
        return
    hour_wild = hour_is_wildcard(schedule)
    lo = start_utc
    step = _FIRST_CHUNK
    while lo < end_utc:
        hi = end_utc if end_utc - lo <= step else lo + step
        fires, reach = _chunk(schedule, tz, lo, hi, hour_wild)
        yield from fires
        if reach is None:
            return  # the walk is exhausted: no later fire exists
        lo = hi
        # An empty chunk grows fast, and the next one is stretched to reach
        # the next wall-clock fire the walk already saw: a sparse cron costs
        # two chunks, not a scan.
        step = min(step * 4, _SPARSE_CAP) if not fires else min(
            step * 2, _DENSE_CAP if len(fires) >= _DENSE_FIRES else _SPARSE_CAP
        )
        if reach > lo + step:
            step = reach - lo


def _accept_all(_fire: datetime) -> bool:
    return True


def last_fire_before(
    schedule: str,
    tz: tzinfo,
    before: datetime,
    *,
    accept: Callable[[datetime], bool] = _accept_all,
) -> datetime | None:
    """The latest fire strictly before the UTC minute `before` that `accept`s it.

    Walks backwards in doubling windows (a newest-first search over the
    forward helper), so the cost is that of the fires between `before` and
    the answer. None when the schedule never fires, or nothing is accepted
    since 1970.
    """
    require_five_fields(schedule)
    require_utc_minute(before, "before")
    if never_fires(schedule):
        return None
    hi = before
    step = timedelta(hours=1)
    while hi > EPOCH:
        lo = max(hi - step, EPOCH)
        latest = None
        for fire in fire_times(schedule, tz, lo, hi):
            if accept(fire):
                latest = fire
        if latest is not None:
            return latest
        hi = lo
        step = min(step * 2, _SPARSE_CAP)
    return None


def local_day_start(day: date, tz: tzinfo) -> datetime:
    """The UTC instant the local calendar date `day` begins in `tz`.

    Local midnight; when midnight does not exist (a spring-forward at 00:00)
    the first existing minute after the gap, and when it repeats the first
    occurrence.
    """
    return _map_wall(datetime.combine(day, time(0)), tz, False)[0]


def local_date(instant: datetime, tz: tzinfo) -> date:
    """The local calendar date of a UTC instant in `tz`."""
    return instant.astimezone(tz).date()
