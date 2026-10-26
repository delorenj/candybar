"""Dispatcher schedule, identity, ack/retry, and role wiring tests."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from candybar.dispatcher import Dispatcher, command_for, matches_tick
from candybar.dispatcher.role import DURABLE, EVENT_STREAM, TICK_FILTER
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand
from candybar.registry.store import ScheduleEntry

MINUTE = datetime(2026, 10, 25, 12, 35, tzinfo=UTC)
ENTRY = ScheduleEntry(uuid4(), "smoke-test", "*/5 * * * *")
MISS = ScheduleEntry(uuid4(), "smoke-two", "0 * * * *")


class Message:
    def __init__(self, payload: bytes):
        self.data = payload
        self.acks = 0
        self.naks = 0
        self.terms = 0

    async def ack(self):
        self.acks += 1

    async def nak(self, **_kwargs):
        self.naks += 1

    async def term(self):
        self.terms += 1


def drive(coro):
    return asyncio.run(coro)


def tick_message(tick=None):
    return Message((tick or build_tick_fired(MINUTE)).model_dump_json().encode())


def test_matches_and_nonmatching_minutes():
    assert matches_tick(ENTRY.schedule, MINUTE)
    assert not matches_tick(ENTRY.schedule, MINUTE.replace(minute=36))
    with pytest.raises(ValueError, match="five fields"):
        matches_tick("* * * * * *", MINUTE)


def test_timezone_independent_of_host():
    before = os.environ.get("TZ")
    try:
        for tz in ("UTC", "America/Los_Angeles", "Asia/Tokyo"):
            os.environ["TZ"] = tz
            time.tzset()
            assert matches_tick("35 12 * * *", MINUTE)
            assert not matches_tick("35 4 * * *", MINUTE)
    finally:
        if before is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = before
        time.tzset()


def test_command_identity_is_stable_across_redeliveries():
    tick = build_tick_fired(MINUTE)
    first = command_for(ENTRY, tick)
    second = command_for(ENTRY, tick)
    assert first == second
    assert first.subject == "bloodbank.cmd.cron.candybar.invoke"
    assert first.idempotency_key == f"candybar:{ENTRY.id}:tick:{tick.data.tick_id}"
    assert first.data.trigger == "scheduled"
    assert first.data.tick_id == tick.data.tick_id
    assert first.command_id == first.id == second.command_id
    assert first.data.invocation_id == second.data.invocation_id


def test_dispatcher_acks_only_after_matching_publish():
    commands = []

    async def schedules():
        return [ENTRY, MISS]

    async def publish(command):
        commands.append(command)

    msg = tick_message()
    drive(Dispatcher(schedules, publish).handle_message(msg))
    assert (msg.acks, msg.naks, msg.terms) == (1, 0, 0)
    assert len(commands) == 1
    assert isinstance(parse_envelope(json.loads(commands[0].model_dump_json())), CandybarInvokeCommand)


def test_nonmatch_acks_without_publish():
    commands = []

    async def schedules():
        return [MISS]

    async def publish(command):
        commands.append(command)

    msg = tick_message()
    drive(Dispatcher(schedules, publish).handle_message(msg))
    assert msg.acks == 1 and not commands


def test_partial_publish_failure_naks_then_retries_same_command_ids():
    sent = []
    failing = True

    async def schedules():
        return [ENTRY, ScheduleEntry(uuid4(), "another", "* * * * *")]

    async def publish(command):
        nonlocal failing
        if command.data.name == "another" and failing:
            failing = False
            raise ConnectionError("bus unavailable")
        sent.append(command)

    dispatcher = Dispatcher(schedules, publish)
    tick = build_tick_fired(MINUTE)
    first, retry = tick_message(tick), tick_message(tick)
    drive(dispatcher.handle_message(first))
    drive(dispatcher.handle_message(retry))
    assert (first.acks, first.naks) == (0, 1)
    assert (retry.acks, retry.naks) == (1, 0)
    assert sent[0].id == sent[1].id
    assert sent[0].command_id == sent[1].command_id
    assert sent[0].idempotency_key == sent[1].idempotency_key


def test_duplicate_tick_republishes_same_wire_identity():
    commands = []

    async def schedules():
        return [ENTRY]

    async def publish(command):
        commands.append(command)

    dispatcher = Dispatcher(schedules, publish)
    tick = build_tick_fired(MINUTE)
    first, duplicate = tick_message(tick), tick_message(tick)
    drive(dispatcher.handle_message(first))
    drive(dispatcher.handle_message(duplicate))
    assert first.acks == duplicate.acks == 1
    assert commands[0] == commands[1]


@pytest.mark.parametrize("payload", [b"{broken", b"{}", b"[]"])
def test_invalid_tick_payload_is_terminated(payload):
    async def schedules():
        raise AssertionError("should not query database")

    async def publish(command):
        raise AssertionError("should not publish")

    msg = Message(payload)
    drive(Dispatcher(schedules, publish).handle_message(msg))
    assert (msg.acks, msg.naks, msg.terms) == (0, 0, 1)


def test_inconsistent_tick_identity_is_terminated():
    payload = build_tick_fired(MINUTE).model_dump(mode="json")
    payload["data"]["tick_id"] = "tick:1"
    msg = Message(json.dumps(payload).encode())

    async def schedules():
        raise AssertionError("should not query database")

    async def publish(command):
        raise AssertionError("should not publish")

    drive(Dispatcher(schedules, publish).handle_message(msg))
    assert msg.terms == 1


def test_invalid_schema_payload_is_terminated():
    payload = build_tick_fired(MINUTE).model_dump(mode="json")
    payload["data"]["minute"] = "not-a-date"
    msg = Message(json.dumps(payload).encode())

    async def schedules():
        raise AssertionError("should not query database")

    async def publish(command):
        raise AssertionError("should not publish")

    drive(Dispatcher(schedules, publish).handle_message(msg))
    assert msg.terms == 1


def test_single_durable_configuration():
    assert EVENT_STREAM == "BLOODBANK_EVENTS"
    assert TICK_FILTER == "bloodbank.evt.cron.tick.>"
    assert DURABLE == "candybar-dispatcher-ticks"
