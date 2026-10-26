"""Schedule rows for the CAP-8 read API (Stories 5.2, 5.3; AD-11, AD-13).

`GET /schedule?from=<ts>&to=<ts>` answers holocene-view-contract.md's row
`{candybarId, name, schedule, fireTime, state, lastResult?, skipped, noise}`
for every fire of every registered candybar in the half-open UTC window
`from <= fireTime < to`. `GET /schedule/next` (5.3) answers the one `current`
row alone, with one `next_fire` lookup per candybar and no window walk. This
module is the pure computation, without a DB or a clock: the routes read the
registry and the wall clock and pass them in, and 5.4 (stream payloads)
reuses the same rows.

Fire times come from the dispatcher's own helpers (`dispatcher/schedule.py`):
the same croniter UTC evaluation, and the same `SkipSet` deciding which fires
an operator skip covers. So a row is `skipped` exactly when the dispatcher
publishes a `skipped` fact instead of a command for that tick.

States, relative to `cut` = the first tick minute strictly after `now`:
fires before `cut` have fired (or are firing) and are `past`. The single
`current` row is the global next-to-fire (`global_next`, shared by both
routes), the earliest non-skipped fire at or after `cut` over all candybars
(ties in row order). It is in a window only when that fire is, so a past
window, or a future window with an earlier fire before it, has no `current`
row. Everything else is `upcoming`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from ..dispatcher.schedule import (
    NO_SKIPS,
    ONE_MINUTE,
    SkipSet,
    ceil_minute,
    fires_between,
    matches_tick,
    next_fire,
)
from ..events.vocabulary import scheduled_idempotency_key, tick_id_for_minute

# Bounds on one request. The api shares the daemon's event loop, and croniter
# costs ~10µs per fire: a day of ten minutely crons is 14,400 rows.
MAX_WINDOW = timedelta(days=31)
MAX_ROWS = 20_000

STATE_PAST = "past"
STATE_CURRENT = "current"
STATE_UPCOMING = "upcoming"


class WindowError(ValueError):
    """A window the API refuses to compute (answered as a 422)."""


@dataclass(frozen=True)
class WindowCandybar:
    """The definition fields a schedule row needs, detached from the ORM."""

    id: UUID
    name: str
    schedule: str
    noise: bool
    created_at: datetime

    @classmethod
    def of(cls, row: Any) -> WindowCandybar:
        return cls(
            id=row.id,
            name=row.name,
            schedule=row.schedule,
            noise=row.noise,
            created_at=row.created_at.astimezone(UTC),
        )


def fire_key(candybar_id: UUID, fire_time: datetime) -> str:
    """The idempotency key of the invocation that ran this fire (scheduled or misfire)."""
    return scheduled_idempotency_key(candybar_id, tick_id_for_minute(int(fire_time.timestamp()) // 60))


@dataclass(frozen=True)
class ScheduleRow:
    candybar_id: UUID
    name: str
    schedule: str
    noise: bool
    fire_time: datetime
    state: str
    covered: bool  # an operator skip (occurrence / rest_of_day) covers this fire
    last_result: str | None = None  # past rows only: the recorded invocation's result

    @property
    def skipped(self) -> bool:
        # A skip stored after the fire already ran does not rewrite history:
        # a recorded invocation means the fire was not suppressed.
        return self.covered and self.last_result is None

    @property
    def key(self) -> str:
        return fire_key(self.candybar_id, self.fire_time)

    def to_json(self) -> dict[str, Any]:
        """The contract row. `lastResult?` is omitted, never null, when unknown."""
        body: dict[str, Any] = {
            "candybarId": str(self.candybar_id),
            "name": self.name,
            "schedule": self.schedule,
            "fireTime": self.fire_time.astimezone(UTC).isoformat(),
            "state": self.state,
        }
        if self.last_result is not None:
            body["lastResult"] = self.last_result
        body["skipped"] = self.skipped
        body["noise"] = self.noise
        return body


def first_tick_after(now: datetime) -> datetime:
    """`cut`: the first tick minute strictly after `now`. Fires before it have fired."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC).replace(second=0, microsecond=0) + ONE_MINUTE


def window_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Validate the request window and return its UTC minute bounds `[lo, hi)`.

    Fires are minute-aligned, so `fire >= start` iff `fire >= ceil(start)` and
    `fire < end` iff `fire < ceil(end)`. Any offset is converted to UTC.
    """
    if start.tzinfo is None or start.utcoffset() is None:
        raise WindowError("from must carry a UTC offset")
    if end.tzinfo is None or end.utcoffset() is None:
        raise WindowError("to must carry a UTC offset")
    if end <= start:
        raise WindowError("to must be after from")
    if end - start > MAX_WINDOW:
        raise WindowError(f"the window spans more than {MAX_WINDOW.days} days")
    try:
        return ceil_minute(start), ceil_minute(end)
    except OverflowError as exc:
        raise WindowError("the window is out of range") from exc


def skip_read_start(start: datetime, now: datetime) -> datetime:
    """Where the window's skip read starts: `min(start, cut)`.

    The rows need skips over `[start, end)`. A window that starts after `cut`
    also checks for an earlier non-skipped fire over `[cut, start)`.
    """
    return min(start, first_tick_after(now))


def _registered_floor(candybar: WindowCandybar) -> datetime:
    # Fires before registration were never the candybar's (Story 2.2's rule).
    return ceil_minute(candybar.created_at)


# The global current row's identity: (fire time, candybar id), or None.
CurrentKey = tuple[datetime, UUID] | None


def row_state(fire: datetime, candybar_id: UUID, *, cut: datetime, current: CurrentKey) -> str:
    """THE state rule, shared by /schedule and the stream (Story 5.4).

    A fire before `cut` has fired: `past`. The global next-to-fire is
    `current`. Everything else is `upcoming`. `global_next` never returns a
    fire before `cut`, so checking past first only matters when the caller's
    `current` is stale. The stream keeps the current row from its last tick,
    so a row whose fire has since passed reads `past`, as a refetch would.
    """
    if fire < cut:
        return STATE_PAST
    if current is not None and (fire, candybar_id) == current:
        return STATE_CURRENT
    return STATE_UPCOMING


def current_key(found: tuple[datetime, WindowCandybar] | None) -> CurrentKey:
    """The identity of `global_next`'s answer, for `row_state`."""
    return (found[0], found[1].id) if found is not None else None


def tick_minute(tick_id: str) -> datetime:
    """The UTC minute a `tick:<epoch_minute>` names: a fire's time (AD-4)."""
    return datetime.fromtimestamp(int(tick_id.removeprefix("tick:")) * 60, UTC)


def is_fire_of(candybar: WindowCandybar, fire: datetime) -> bool:
    """Whether `fire` is one of the candybar's `/schedule` rows.

    A row is a fire of the CURRENT definition at or after registration. A run
    dispatched under an older schedule has no row in a refetch, so the
    stream does not invent one either.
    """
    return fire >= _registered_floor(candybar) and matches_tick(candybar.schedule, fire)


def schedule_row(
    candybar: WindowCandybar,
    fire: datetime,
    *,
    cut: datetime,
    current: CurrentKey,
    covered: bool,
    last_result: str | None = None,
) -> ScheduleRow:
    """One contract row for `fire`, its state decided by `row_state`."""
    return ScheduleRow(
        candybar_id=candybar.id,
        name=candybar.name,
        schedule=candybar.schedule,
        noise=candybar.noise,
        fire_time=fire,
        state=row_state(fire, candybar.id, cut=cut, current=current),
        covered=covered,
        last_result=last_result,
    )


def _fires_in(candybar: WindowCandybar, start: datetime, end: datetime) -> Iterable[datetime]:
    lo = max(start, _registered_floor(candybar))
    if lo >= end:
        return ()
    return fires_between(candybar.schedule, lo, end)


def next_fire_of(
    candybar: WindowCandybar, cut: datetime, skips: SkipSet = NO_SKIPS
) -> datetime | None:
    """A candybar's first non-skipped fire at or after `cut` (and its registration).

    One croniter lookup (`next_fire`), never a window walk: O(1 + occurrence
    skips + skipped days passed). None when the schedule never fires again.
    """
    try:
        return next_fire(candybar.schedule, max(cut, _registered_floor(candybar)), skips=skips)
    except OverflowError:  # no fire before year 9999
        return None


def global_next(
    candybars: Iterable[WindowCandybar], *, now: datetime, skips: Mapping[UUID, SkipSet]
) -> tuple[datetime, WindowCandybar] | None:
    """The global next-to-fire `(fire, candybar)`: THE definition of `current`.

    The earliest non-skipped fire at or after `cut` over all candybars, ties
    broken by name (unique), the row order. `skips` must cover every fire it
    may pass: `[cut, ∞)` for `/schedule/next`. A read that stops at a window's
    end is also exact for that window, because a fire computed past `end` is
    not one of its rows either way.
    """
    cut = first_tick_after(now)
    best: tuple[datetime, WindowCandybar] | None = None
    for candybar in candybars:
        fire = next_fire_of(candybar, cut, skips.get(candybar.id, NO_SKIPS))
        if fire is not None and (best is None or (fire, candybar.name) < (best[0], best[1].name)):
            best = (fire, candybar)
    return best


def next_row(
    candybars: Iterable[WindowCandybar], *, now: datetime, skips: Mapping[UUID, SkipSet]
) -> ScheduleRow | None:
    """`GET /schedule/next` (Story 5.3): the single `current` row, or None.

    The same row `window_rows` marks `current` in any window containing it.
    A non-skipped fire at or after `cut` has not run, so it has no
    `last_result` and is never `skipped`.
    """
    found = global_next(candybars, now=now, skips=skips)
    if found is None:
        return None
    fire, candybar = found
    return ScheduleRow(
        candybar_id=candybar.id,
        name=candybar.name,
        schedule=candybar.schedule,
        noise=candybar.noise,
        fire_time=fire,
        state=STATE_CURRENT,
        covered=False,
    )


def window_rows(
    candybars: Sequence[WindowCandybar],
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    skips: Mapping[UUID, SkipSet],
    limit: int = MAX_ROWS,
) -> list[ScheduleRow]:
    """Every fire in `[start, end)` (minute bounds), ordered by `(fireTime, name)`.

    `skips` must cover `[skip_read_start(start, now), end)`. Rows carry no
    `last_result` yet: see `past_keys` / `attach_results`. Raises WindowError
    when the window yields more than `limit` rows.
    """
    cut = first_tick_after(now)
    found: list[tuple[datetime, WindowCandybar, bool]] = []
    try:
        for candybar in candybars:
            overrides = skips.get(candybar.id, NO_SKIPS)
            for fire in _fires_in(candybar, start, end):
                found.append((fire, candybar, overrides.covers(fire)))
                if len(found) > limit:
                    raise WindowError(f"the window has more than {limit} fires; narrow it")
        # Names are unique, so (fire time, name) is a total order.
        found.sort(key=lambda item: (item[0], item[1].name))
        # `current` is the global next fire (shared with /schedule/next): a
        # window that starts after an earlier non-skipped fire has none.
        nxt = global_next(candybars, now=now, skips=skips)
    except OverflowError as exc:  # croniter walking past year 9999
        raise WindowError("the window is out of range") from exc
    current = current_key(nxt)
    return [
        schedule_row(candybar, fire, cut=cut, current=current, covered=covered)
        for fire, candybar, covered in found
    ]


def candybar_rows(
    candybar: WindowCandybar,
    start: datetime,
    end: datetime,
    *,
    now: datetime,
    skips: SkipSet,
    current: CurrentKey,
    limit: int = MAX_ROWS,
) -> list[ScheduleRow]:
    """One candybar's rows in `[start, end)`, with the global `current` given.

    The stream's `definition.changed` rows (Story 5.4): exactly the rows of
    this candybar in `window_rows` over the same window, when `current` is
    `global_next`'s answer over all candybars. `skips` must cover `[start, end)`.
    """
    cut = first_tick_after(now)
    rows: list[ScheduleRow] = []
    try:
        for fire in _fires_in(candybar, start, end):
            rows.append(
                schedule_row(candybar, fire, cut=cut, current=current, covered=skips.covers(fire))
            )
            if len(rows) > limit:
                raise WindowError(f"the window has more than {limit} fires; narrow it")
    except OverflowError as exc:  # croniter walking past year 9999
        raise WindowError("the window is out of range") from exc
    return rows


def past_keys(rows: Iterable[ScheduleRow]) -> list[str]:
    """The invocation keys to look up: past rows only (upcoming fires have not run)."""
    return [row.key for row in rows if row.state == STATE_PAST]


def attach_results(rows: Iterable[ScheduleRow], results: Mapping[str, str]) -> list[ScheduleRow]:
    """Set `last_result` on past rows whose fire has a recorded terminal invocation."""
    out: list[ScheduleRow] = []
    for row in rows:
        if row.state == STATE_PAST and (result := results.get(row.key)) is not None:
            row = replace(row, last_result=result)
        out.append(row)
    return out
