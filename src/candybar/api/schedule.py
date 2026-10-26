"""Schedule window rows for the CAP-8 read API (Story 5.2; AD-11, AD-13).

`GET /schedule?from=<ts>&to=<ts>` answers holocene-view-contract.md's row
`{candybarId, name, schedule, fireTime, state, lastResult?, skipped, noise}`
for every fire of every registered candybar in the half-open UTC window
`from <= fireTime < to`. This module is the pure computation, without a DB or
a clock: the route reads the registry and the wall clock and passes them in,
and 5.3 (next row) and 5.4 (stream payloads) reuse the same rows.

Fire times come from the dispatcher's own helpers (`dispatcher/schedule.py`):
the same croniter UTC evaluation, and the same `SkipSet` deciding which fires
an operator skip covers. So a row is `skipped` exactly when the dispatcher
publishes a `skipped` fact instead of a command for that tick.

States, relative to `cut` = the first tick minute strictly after `now`:
fires before `cut` have fired (or are firing) and are `past`. The single
`current` row is the global next-to-fire, the earliest non-skipped fire at or
after `cut` over all candybars (ties in row order). It is in a window only
when that fire is, so a past window, or a future window with an earlier
fire before it, has no `current` row. Everything else is `upcoming`.
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


def _fires_in(
    candybar: WindowCandybar, start: datetime, end: datetime, *, skips: SkipSet = NO_SKIPS
) -> Iterable[datetime]:
    lo = max(start, _registered_floor(candybar))
    if lo >= end:
        return ()
    return fires_between(candybar.schedule, lo, end, skips=skips)


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

        # The global next fire precedes a window that starts after `cut` when
        # any candybar has a non-skipped fire in [cut, start).
        earlier = start > cut and any(
            next(iter(_fires_in(c, cut, start, skips=skips.get(c.id, NO_SKIPS))), None)
            is not None
            for c in candybars
        )
    except OverflowError as exc:  # croniter walking past year 9999
        raise WindowError("the window is out of range") from exc

    current = None
    if not earlier:
        current = next(
            (i for i, (fire, _, covered) in enumerate(found) if fire >= cut and not covered),
            None,
        )

    rows: list[ScheduleRow] = []
    for i, (fire, candybar, covered) in enumerate(found):
        if i == current:
            state = STATE_CURRENT
        elif fire < cut:
            state = STATE_PAST
        else:
            state = STATE_UPCOMING
        rows.append(
            ScheduleRow(
                candybar_id=candybar.id,
                name=candybar.name,
                schedule=candybar.schedule,
                noise=candybar.noise,
                fire_time=fire,
                state=state,
                covered=covered,
            )
        )
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
