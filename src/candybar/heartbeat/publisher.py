"""Heartbeat publisher: the only sleep-until-a-time loop in the system (AD-1).

Sleeps to each UTC minute boundary and publishes exactly one
`bloodbank.cron.tick.fired` fact per boundary (AD-4). The tick is built for
the SCHEDULED boundary — captured before sleeping — so clock skew at publish
time can never change a tick's identity (AD-4: wall-clock-derived, never
receipt time). Clock and sleep are injectable so tests drive virtual time;
production wiring lives in `role.py`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from nats.js import JetStreamContext
from nats.js.api import PubAck

from ..events import build_tick_fired
from ..events.envelope import TickFiredEvent
from .clock import next_minute_boundary, seconds_until

log = logging.getLogger(__name__)

NowFn = Callable[[], datetime]
SleepFn = Callable[[float], Awaitable[None]]

PUBLISH_MAX_ATTEMPTS = 3
PUBLISH_BACKOFF_SECONDS = 1.0


class TickSink(Protocol):
    """Where built ticks go. Production: JetStream (NatsTickSink); tests: in-memory."""

    async def publish_tick(self, event: TickFiredEvent) -> None: ...


class NatsTickSink:
    """JetStream sink: publishes envelope JSON with `Nats-Msg-Id` = event id (AD-14).

    The header makes the stream dedupe retried publishes of the same event
    (within the stream's duplicate window), so a heartbeat retry or a bus-side
    replay of the same envelope never stores a second copy.
    """

    def __init__(self, js: JetStreamContext) -> None:
        self._js = js

    async def publish_tick(self, event: TickFiredEvent) -> PubAck:  # type: ignore[override]
        ack = await self._js.publish(
            event.subject,
            event.model_dump_json().encode("utf-8"),
            headers={"Nats-Msg-Id": str(event.id)},
        )
        if ack.duplicate:
            log.info("tick %s deduped by stream (duplicate ack, seq %d)", event.data.tick_id, ack.seq)
        else:
            log.info("tick %s published (seq %d)", event.data.tick_id, ack.seq)
        return ack


class Heartbeat:
    """The minute-tick loop. Owns the only clock in the process (AD-1)."""

    def __init__(
        self,
        sink: TickSink,
        *,
        now: NowFn | None = None,
        sleep: SleepFn | None = None,
        publish_max_attempts: int = PUBLISH_MAX_ATTEMPTS,
        publish_backoff: float = PUBLISH_BACKOFF_SECONDS,
    ) -> None:
        self._sink = sink
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep
        self._publish_max_attempts = publish_max_attempts
        self._publish_backoff = publish_backoff

    async def run(self, stop: asyncio.Event) -> None:
        """Publish one tick per UTC minute boundary until `stop` is set."""
        while not stop.is_set():
            boundary = next_minute_boundary(self._now())
            if not await self._sleep_until(boundary, stop):
                break
            # Built for the captured boundary — never re-read the clock here.
            await self._publish_with_retry(build_tick_fired(boundary))

    async def _sleep_until(self, boundary: datetime, stop: asyncio.Event) -> bool:
        """True when the boundary was reached; False when stopped first.

        With no injected sleep (production), waits on the stop event with the
        boundary as timeout so shutdown is immediate rather than up to a
        minute late. A boundary already in the past (wake skew) fires
        immediately — late-but-correct beats dropped (CAP-1: small skew
        tolerated; gaps look like downtime to the dispatcher).
        """
        if self._sleep is not None:
            await self._sleep(seconds_until(boundary, self._now()))
            return not stop.is_set()
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds_until(boundary, self._now()))
        except TimeoutError:
            return True
        return False

    async def _pause(self, seconds: float) -> None:
        if self._sleep is not None:
            await self._sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    async def _publish_with_retry(self, event: TickFiredEvent) -> None:
        """Retry the SAME event (same id → Nats-Msg-Id dedupes), then drop.

        Missed ticks are the dispatcher's misfire domain (AD-6), not the
        heartbeat's: after PUBLISH_MAX_ATTEMPTS the tick is dropped and the
        loop moves on to the next boundary.
        """
        for attempt in range(1, self._publish_max_attempts + 1):
            try:
                await self._sink.publish_tick(event)
                return
            except Exception:
                log.exception(
                    "tick %s publish attempt %d/%d failed",
                    event.data.tick_id,
                    attempt,
                    self._publish_max_attempts,
                )
                if attempt < self._publish_max_attempts:
                    await self._pause(self._publish_backoff)
        log.error(
            "dropping tick %s after %d failed publish attempts",
            event.data.tick_id,
            self._publish_max_attempts,
        )
