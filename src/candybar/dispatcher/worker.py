"""Evaluate tick facts into invoke commands, without owning a clock (AD-1).

Misfire coalescing (Story 2.2, AD-6) rides the same tick path: a durable
cursor holds the newest tick minute fully dispatched. A consumed tick more
than one minute past it is a gap; per candybar, only the LATEST missed fire
in that gap runs (`trigger=misfire`), and earlier missed fires inside the 24h
catch-up window are recorded as `skipped` (`reason=misfire_coalesced`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid5

from pydantic import ValidationError

from ..events import build_candybar_invoke, build_candybar_skipped, parse_envelope
from ..events.envelope import (
    CandybarInvokeCommand,
    CandybarSkippedEvent,
    Envelope,
    TickFiredEvent,
)
from ..events.vocabulary import tick_id_for_minute
from ..registry.store import ScheduleEntry
from .schedule import (
    ONE_MINUTE,
    ceil_minute,
    fires_between,
    matches_tick,
    previous_fire,
)

log = logging.getLogger(__name__)

# Stable command and invocation ids survive tick redelivery and dispatcher restarts.
DISPATCH_NAMESPACE = UUID("ab4dd080-174d-4ec6-9261-5a77cc8d2365")

# AD-6: earlier missed fires are individually recorded only inside this
# window before the detecting tick; the latest missed fire always runs.
CATCH_UP_WINDOW = timedelta(hours=24)
MISFIRE_COALESCED = "misfire_coalesced"


def _tick_id_of(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def command_for(entry: ScheduleEntry, tick: TickFiredEvent) -> CandybarInvokeCommand:
    identity = f"{entry.id}:{tick.data.tick_id}"
    return build_candybar_invoke(
        candybar_id=entry.id,
        name=entry.name,
        trigger="scheduled",
        tick_id=tick.data.tick_id,
        command_id=uuid5(DISPATCH_NAMESPACE, f"command:{identity}"),
        invocation_id=uuid5(DISPATCH_NAMESPACE, f"invocation:{identity}"),
        event_id=uuid5(DISPATCH_NAMESPACE, f"command:{identity}"),
        issued_at=tick.data.minute,
    )


def misfire_command_for(
    entry: ScheduleEntry, fire: datetime, tick: TickFiredEvent
) -> CandybarInvokeCommand:
    """The coalesced catch-up run for a missed fire time.

    Its idempotency key is the missed minute's scheduled key
    (`candybar:<id>:tick:<missed tick_id>`), so a fire time executes at most
    once whether it went out as scheduled (pre-crash) or as misfire. Ids are
    keyed on (candybar, missed tick) only: re-detection by any later tick
    rebuilds the same command.
    """
    missed = _tick_id_of(fire)
    identity = f"{entry.id}:{missed}"
    command_id = uuid5(DISPATCH_NAMESPACE, f"misfire-command:{identity}")
    return build_candybar_invoke(
        candybar_id=entry.id,
        name=entry.name,
        trigger="misfire",
        tick_id=missed,
        command_id=command_id,
        invocation_id=uuid5(DISPATCH_NAMESPACE, f"misfire-invocation:{identity}"),
        event_id=command_id,
        issued_at=tick.data.minute,
    )


def misfire_skipped_for(
    entry: ScheduleEntry, fire: datetime, tick: TickFiredEvent
) -> CandybarSkippedEvent:
    """The `skipped` fact for an earlier missed fire that coalescing dropped."""
    missed = _tick_id_of(fire)
    return build_candybar_skipped(
        candybar_id=entry.id,
        name=entry.name,
        tick_id=missed,
        reason=MISFIRE_COALESCED,
        correlationid=tick.correlationid,
        causationid=tick.id,
        event_id=uuid5(DISPATCH_NAMESPACE, f"skipped:{MISFIRE_COALESCED}:{entry.id}:{missed}"),
    )


@dataclass(frozen=True)
class MisfirePlan:
    """What one gap coalesces to for one candybar."""

    entry: ScheduleEntry
    latest: datetime
    skipped: list[datetime] = field(default_factory=list)


def plan_misfires(
    entries: list[ScheduleEntry],
    cursor: datetime,
    minute: datetime,
    *,
    window: timedelta = CATCH_UP_WINDOW,
) -> list[MisfirePlan]:
    """Missed fires `cursor < t < minute` (and `t >= created_at`), coalesced.

    Pure: the latest missed fire per candybar runs; earlier ones inside
    `[minute - window, latest)` are listed for `skipped` facts, oldest first.
    """
    plans: list[MisfirePlan] = []
    window_start = minute - window
    for entry in entries:
        floor = cursor + ONE_MINUTE
        if entry.created_at is not None:
            floor = max(floor, ceil_minute(entry.created_at))
        latest = previous_fire(entry.schedule, minute)
        if latest < floor:
            continue  # no missed fire for this candybar
        earlier = list(fires_between(entry.schedule, max(floor, window_start), latest))
        plans.append(MisfirePlan(entry=entry, latest=latest, skipped=earlier))
    return plans


class TickCursor(Protocol):
    """Durable last-seen tick (registry row in production)."""

    async def load(self) -> datetime | None: ...

    async def advance(self, minute: datetime) -> None: ...


def _num_pending(msg: object) -> int:
    """Ticks queued behind this one on the durable; 0 when unknown.

    nats raises on a message without a JetStream reply subject, and test
    doubles carry no metadata at all: both mean "no backlog evidence".
    """
    try:
        pending = msg.metadata.num_pending  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - NotJSMessageError, AttributeError, parse errors
        return 0
    return pending if isinstance(pending, int) and pending > 0 else 0


class Dispatcher:
    """One tick is acked only after all matching commands are published."""

    def __init__(
        self,
        schedules: Callable[[], Awaitable[list[ScheduleEntry]]],
        publish: Callable[[Envelope], Awaitable[None]],
        cursor: TickCursor | None = None,
        *,
        catch_up_window: timedelta = CATCH_UP_WINDOW,
    ) -> None:
        self._schedules = schedules
        self._publish = publish
        self._cursor = cursor
        self._window = catch_up_window

    async def dispatch(self, tick: TickFiredEvent, *, pending: int = 0) -> int:
        """Publish the tick's scheduled commands (after any misfire coalescing).

        Returns the number of scheduled commands. With a cursor wired: a tick
        ahead of the cursor with newer ticks queued behind it (`pending`) is a
        backlog tick and dispatches nothing — the newest tick coalesces the
        whole gap; the cursor advances only after every publish succeeded.
        """
        minute = tick.data.minute
        if minute.utcoffset() is None or minute.utcoffset().total_seconds() != 0:
            raise ValueError("tick minute must be UTC")
        if minute.second or minute.microsecond:
            raise ValueError("tick minute must be on a minute boundary")
        if tick.data.tick_id != tick_id_for_minute(int(minute.timestamp()) // 60):
            raise ValueError("tick_id does not match tick minute")
        if tick.ordering_key != tick.data.tick_id:
            raise ValueError("tick ordering key does not match tick_id")

        last = await self._cursor.load() if self._cursor is not None else None
        ahead = last is not None and minute > last
        if ahead and pending > 0:
            log.info(
                "tick %s is backlog (%d newer queued); deferring to the newest tick",
                tick.data.tick_id,
                pending,
            )
            return 0

        entries = await self._schedules()
        if ahead and minute - last > ONE_MINUTE:  # type: ignore[operator]
            await self._coalesce(entries, last, tick)  # type: ignore[arg-type]
        count = 0
        for entry in entries:
            if matches_tick(entry.schedule, minute):
                await self._publish(command_for(entry, tick))
                count += 1
        if self._cursor is not None and (last is None or minute > last):
            await self._cursor.advance(minute)
        return count

    async def _coalesce(self, entries: list[ScheduleEntry], last: datetime, tick: TickFiredEvent) -> None:
        minute = tick.data.minute
        if minute - last > self._window:
            log.warning(
                "dispatcher gap %s..%s exceeds the %s catch-up window: missed fires before %s "
                "are not individually recorded; each candybar's latest missed fire still runs",
                last.isoformat(),
                minute.isoformat(),
                self._window,
                (minute - self._window).isoformat(),
            )
        plans = plan_misfires(entries, last, minute, window=self._window)
        for plan in plans:
            for fire in plan.skipped:
                await self._publish(misfire_skipped_for(plan.entry, fire, tick))
            await self._publish(misfire_command_for(plan.entry, plan.latest, tick))
        log.info(
            "tick %s closed gap after %s: %d misfire run(s), %d coalesced fire(s) skipped",
            tick.data.tick_id,
            last.isoformat(),
            len(plans),
            sum(len(p.skipped) for p in plans),
        )

    async def handle_message(self, msg: object) -> None:
        """Terminate poison payloads; retry operational failures without acking."""
        try:
            event = parse_envelope(json.loads(msg.data))  # type: ignore[attr-defined]
            if not isinstance(event, TickFiredEvent):
                raise TypeError("not a tick fact")
            # Invalid tick identity must not become a retry loop.
            minute = event.data.minute
            if (
                minute.utcoffset() is None
                or minute.utcoffset().total_seconds() != 0
                or minute.second
                or minute.microsecond
                or event.data.tick_id != tick_id_for_minute(int(minute.timestamp()) // 60)
                or event.ordering_key != event.data.tick_id
            ):
                raise ValueError("invalid tick identity or UTC minute")
        except (ValueError, TypeError, AttributeError, ValidationError) as exc:
            log.warning("discarding invalid tick: %s", exc)
            await msg.term()  # type: ignore[attr-defined]
            return

        try:
            dispatched = await self.dispatch(event, pending=_num_pending(msg))
        except Exception:
            log.exception("dispatch failed for %s; requesting redelivery", event.data.tick_id)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        await msg.ack()  # type: ignore[attr-defined]
        log.info("tick %s dispatched %d candybars", event.data.tick_id, dispatched)
