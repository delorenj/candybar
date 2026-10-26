"""Live-NATS integration tests for the heartbeat — env-gated, real JetStream.

Run with `CANDYBAR_TEST_NATS=1` (NATS_URL env, default nats://localhost:4222).
CI runs these against a pinned nats-server started with `-js`; locally they
hit bloodbank-nats. The literal 5-minute real-time subscriber window (the
story AC) additionally requires `CANDYBAR_TICK_E2E=1` and is run locally, not
in CI. The accelerated variant drives virtual minutes through the REAL
publish/consume path, proving the mechanics without 5 wall minutes.

Publishing real `bloodbank.cron.tick.fired` facts to the live bus is
sanctioned: story 1.2 registered the cron domain `active` precisely so first
emission lands here. Consumers created by these tests are ephemeral or
uniquely-named-and-deleted durables; nothing test-owned persists.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import nats
import pytest
from nats.js import JetStreamContext
from nats.js.api import ConsumerConfig, DeliverPolicy, StorageType, StreamConfig
from nats.js.errors import NotFoundError

from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import TickFiredEvent
from candybar.heartbeat import Heartbeat, NatsTickSink, next_minute_boundary

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1",
    reason="live-NATS integration tests need CANDYBAR_TEST_NATS=1",
)

STREAM = "BLOODBANK_EVENTS"
TICK_SUBJECT = "bloodbank.evt.cron.tick.fired"
TIME_SCALE = 60.0  # accelerated test: 60 virtual seconds per 1 wall second
E2E_MINUTES = 5
E2E_SKEW_TOLERANCE_SECONDS = 5.0


async def _connect() -> nats.NATS:
    return await nats.connect(
        os.environ.get("NATS_URL", "nats://localhost:4222"),
        name="candybar-heartbeat-test",
    )


async def _ensure_events_stream(js: JetStreamContext) -> None:
    """Create BLOODBANK_EVENTS on a fresh server (CI); a no-op on the live bus."""
    try:
        await js.stream_info(STREAM)
    except NotFoundError:
        await js.add_stream(
            StreamConfig(name=STREAM, subjects=["bloodbank.evt.>"], storage=StorageType.FILE)
        )


def _epoch_minute(dt: datetime) -> int:
    return int(dt.timestamp()) // 60


class ScaledVirtualClock:
    """Virtual clock whose sleeps pass in real time divided by TIME_SCALE."""

    def __init__(self, start: datetime) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds / TIME_SCALE)
        self.t += timedelta(seconds=seconds)


class CollectingSubscriber:
    """Ephemeral push subscriber parsing every tick it receives."""

    def __init__(self) -> None:
        self.received: list[tuple[TickFiredEvent, float]] = []  # (event, wall receipt time)

    async def __call__(self, msg) -> None:  # nats.Msg — duck-typed callback
        event = parse_envelope(json.loads(msg.data))
        assert isinstance(event, TickFiredEvent)
        self.received.append((event, datetime.now(UTC).timestamp()))


def test_virtual_minute_ticks_flow_over_real_jetstream() -> None:
    """Accelerated subscriber observation: one tick per (virtual) minute."""

    async def scenario() -> None:
        nc = await _connect()
        try:
            js = nc.jetstream()
            await _ensure_events_stream(js)
            subscriber = CollectingSubscriber()
            await js.subscribe(TICK_SUBJECT, cb=subscriber, deliver_policy=DeliverPolicy.NEW)

            start = datetime.now(UTC).replace(second=0, microsecond=0)
            clock = ScaledVirtualClock(start)

            published: list[TickFiredEvent] = []

            class WatchSink(NatsTickSink):
                async def publish_tick(self, event: TickFiredEvent):  # type: ignore[override]
                    ack = await super().publish_tick(event)
                    published.append(event)
                    return ack

            stop = asyncio.Event()
            hb = Heartbeat(WatchSink(js), now=clock.now, sleep=clock.sleep)

            async def watch() -> None:
                while len(published) < E2E_MINUTES and not stop.is_set():
                    await asyncio.sleep(0.05)
                stop.set()

            await asyncio.gather(hb.run(stop), watch())
            # Give the subscriber a moment to receive the last ticks.
            deadline = time.monotonic() + 10
            while len(subscriber.received) < E2E_MINUTES and time.monotonic() < deadline:
                await asyncio.sleep(0.05)

            assert len(subscriber.received) >= E2E_MINUTES
            ticks = [e for e, _ in subscriber.received[:E2E_MINUTES]]
            minutes = [_epoch_minute(e.data.minute) for e in ticks]
            assert minutes == [minutes[0] + i for i in range(len(minutes))]  # consecutive
            for event in ticks:
                assert event.ordering_key == event.data.tick_id == f"tick:{_epoch_minute(event.data.minute)}"
            # Small skew: 5 ticks at 1 tick/wall-second must land well inside 15s.
            span = subscriber.received[E2E_MINUTES - 1][1] - subscriber.received[0][1]
            assert span < 15.0
        finally:
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_duplicate_publish_is_deduped_by_msg_id() -> None:
    """Same event published twice: second ack is duplicate with the same seq."""

    async def scenario() -> None:
        nc = await _connect()
        try:
            js = nc.jetstream()
            await _ensure_events_stream(js)
            sink = NatsTickSink(js)
            event = build_tick_fired(next_minute_boundary(datetime.now(UTC)))
            ack1 = await sink.publish_tick(event)
            ack2 = await sink.publish_tick(event)
            assert not ack1.duplicate  # server omits the flag (None) when not a duplicate
            assert ack2.duplicate is True
            assert ack1.seq == ack2.seq
        finally:
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_redelivered_tick_carries_same_tick_id() -> None:
    """At-least-once redelivery: the bus replays the identical payload."""

    async def scenario() -> None:
        nc = await _connect()
        try:
            js = nc.jetstream()
            await _ensure_events_stream(js)
            durable = f"candybar-test-redelivery-{uuid4().hex[:8]}"
            sub = await js.pull_subscribe(
                TICK_SUBJECT,
                durable=durable,
                config=ConsumerConfig(ack_wait=1.5, deliver_policy=DeliverPolicy.NEW),
            )
            try:
                event = build_tick_fired(next_minute_boundary(datetime.now(UTC)))
                await NatsTickSink(js).publish_tick(event)

                first = (await sub.fetch(1, timeout=10))[0]
                # Do NOT ack: wait out ack_wait so the server redelivers.
                await asyncio.sleep(2.0)
                second = (await sub.fetch(1, timeout=10))[0]

                first_payload = json.loads(first.data)
                second_payload = json.loads(second.data)
                assert second_payload["id"] == first_payload["id"] == str(event.id)
                assert (
                    second_payload["data"]["tick_id"]
                    == first_payload["data"]["tick_id"]
                    == event.data.tick_id
                )
                await second.ack()
            finally:
                await js.delete_consumer(STREAM, durable)
        finally:
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


@pytest.mark.skipif(
    os.environ.get("CANDYBAR_TICK_E2E") != "1",
    reason="literal 5-minute real-time run; enable with CANDYBAR_TICK_E2E=1",
)
def test_five_minute_window_one_tick_per_minute() -> None:
    """The story AC verbatim: a subscriber observes one tick per minute with
    small skew over a 5-minute window, with the production clock and sleep."""

    async def scenario() -> None:
        nc = await _connect()
        try:
            js = nc.jetstream()
            await _ensure_events_stream(js)
            subscriber = CollectingSubscriber()
            sub = await js.subscribe(TICK_SUBJECT, cb=subscriber, deliver_policy=DeliverPolicy.NEW)

            hb = Heartbeat(NatsTickSink(js))  # production wiring: real clock, real sleep
            stop = asyncio.Event()
            task = asyncio.create_task(hb.run(stop))
            try:
                deadline = time.monotonic() + (E2E_MINUTES + 1) * 60
                while len(subscriber.received) < E2E_MINUTES and time.monotonic() < deadline:
                    await asyncio.sleep(1.0)
            finally:
                stop.set()
                await asyncio.wait_for(task, timeout=30)
                await sub.unsubscribe()

            received = subscriber.received[: E2E_MINUTES]
            assert len(received) == E2E_MINUTES, (
                f"expected {E2E_MINUTES} ticks in the window, got {len(received)}"
            )
            minutes = [_epoch_minute(e.data.minute) for e, _ in received]
            assert len(set(minutes)) == E2E_MINUTES  # no duplicates
            assert minutes == [minutes[0] + i for i in range(E2E_MINUTES)]  # consecutive
            for event, receipt in received:
                boundary = event.data.minute.timestamp()
                skew = receipt - boundary
                assert 0 <= skew < E2E_SKEW_TOLERANCE_SECONDS, (
                    f"tick {event.data.tick_id} received {skew:.2f}s after its boundary"
                )
        finally:
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=(E2E_MINUTES + 2) * 60))
