"""Evaluate tick facts into invoke commands, without owning a clock (AD-1)."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID, uuid5

from pydantic import ValidationError

from ..events import build_candybar_invoke, parse_envelope
from ..events.envelope import CandybarInvokeCommand, TickFiredEvent
from ..events.vocabulary import tick_id_for_minute
from ..registry.store import ScheduleEntry
from .schedule import matches_tick

log = logging.getLogger(__name__)

# Stable command and invocation ids survive tick redelivery and dispatcher restarts.
DISPATCH_NAMESPACE = UUID("ab4dd080-174d-4ec6-9261-5a77cc8d2365")


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


class Dispatcher:
    """One tick is acked only after all matching commands are published."""

    def __init__(
        self,
        schedules: Callable[[], Awaitable[list[ScheduleEntry]]],
        publish: Callable[[CandybarInvokeCommand], Awaitable[None]],
    ) -> None:
        self._schedules = schedules
        self._publish = publish

    async def dispatch(self, tick: TickFiredEvent) -> int:
        minute = tick.data.minute
        if minute.utcoffset() is None or minute.utcoffset().total_seconds() != 0:
            raise ValueError("tick minute must be UTC")
        if minute.second or minute.microsecond:
            raise ValueError("tick minute must be on a minute boundary")
        if tick.data.tick_id != tick_id_for_minute(int(minute.timestamp()) // 60):
            raise ValueError("tick_id does not match tick minute")
        if tick.ordering_key != tick.data.tick_id:
            raise ValueError("tick ordering key does not match tick_id")
        count = 0
        for entry in await self._schedules():
            if matches_tick(entry.schedule, minute):
                await self._publish(command_for(entry, tick))
                count += 1
        return count

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
            dispatched = await self.dispatch(event)
        except Exception:
            log.exception("dispatch failed for %s; requesting redelivery", event.data.tick_id)
            await msg.nak(delay=1)  # type: ignore[attr-defined]
            return
        await msg.ack()  # type: ignore[attr-defined]
        log.info("tick %s dispatched %d candybars", event.data.tick_id, dispatched)
