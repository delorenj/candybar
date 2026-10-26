"""Live JetStream proof: durable pull, matching dispatch and redelivery dedupe."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from uuid import uuid4

import nats
import pytest
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import NotFoundError

from candybar.dispatcher import Dispatcher
from candybar.events import build_tick_fired, parse_envelope
from candybar.registry.store import ScheduleEntry

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1", reason="requires CANDYBAR_TEST_NATS=1"
)


def test_durable_tick_dispatch_publishes_one_effective_command():
    async def scenario():
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        events_stream = f"CANDYBAR_TEST_TICKS_{suffix.upper()}"
        commands_stream = f"CANDYBAR_TEST_COMMANDS_{suffix.upper()}"
        tick_subject = f"candybar.test.{suffix}.tick"
        command_subject = "bloodbank.cmd.cron.candybar.invoke"
        # On the live Bloodbank bus commands already belong to BLOODBANK_COMMANDS.
        # Use an isolated test subject for transport dedupe, preserving the exact
        # production command envelope as the payload.
        test_command_subject = f"candybar.test.{suffix}.command"
        durable = f"candybar-test-{suffix}"
        try:
            await js.add_stream(StreamConfig(name=events_stream, subjects=[tick_subject]))
            await js.add_stream(StreamConfig(
                name=commands_stream, subjects=[test_command_subject],
                retention=RetentionPolicy.LIMITS,
            ))
            sub = await js.pull_subscribe(
                tick_subject, durable=durable, stream=events_stream,
                config=ConsumerConfig(
                    durable_name=durable, filter_subject=tick_subject,
                    deliver_policy=DeliverPolicy.NEW, ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=1.5,
                ),
            )
            minute = datetime(2026, 10, 25, 12, 35, tzinfo=UTC)
            tick = build_tick_fired(minute)
            entry = ScheduleEntry(uuid4(), "smoke-test", "*/5 * * * *")
            sent = []

            async def schedules():
                return [entry]

            async def publish(command):
                sent.append(command)
                await js.publish(
                    test_command_subject, command.model_dump_json().encode(),
                    headers={"Nats-Msg-Id": str(command.id)},
                )

            dispatcher = Dispatcher(schedules, publish)
            await js.publish(tick_subject, tick.model_dump_json().encode())
            first = (await sub.fetch(1, timeout=5))[0]
            await dispatcher.handle_message(first)
            # Explicit duplicate fact (distinct bus msg id) must re-use command id.
            await js.publish(tick_subject, tick.model_dump_json().encode())
            second = (await sub.fetch(1, timeout=5))[0]
            await dispatcher.handle_message(second)
            assert sent[0].id == sent[1].id
            info = await js.stream_info(commands_stream)
            assert info.state.messages == 1
            fetched = await js.get_msg(commands_stream, seq=1)
            command = parse_envelope(json.loads(fetched.data))
            assert command.subject == command_subject
            assert command.idempotency_key == f"candybar:{entry.id}:tick:{tick.data.tick_id}"
            assert command.data.trigger == "scheduled"
        finally:
            for stream in (events_stream, commands_stream):
                try:
                    await js.delete_stream(stream)
                except NotFoundError:
                    continue
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
