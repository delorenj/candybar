"""Story 2.1 — effectively-once dispatch proven under fault injection.

Drives the PRODUCTION `run_dispatcher` and `run_executor` role coroutines
over a real NATS JetStream and a real Postgres registry, with real child
processes, and injects the three faults the AC names:

* tick redelivery — the same minute re-published (broker dedupe on the
  tick_id-derived msg id) and replayed under a fresh msg id (beyond the
  duplicate window: the dispatcher re-evaluates, command ids are stable);
* invoke-command redelivery — executor dies after the terminal publish but
  before the ack, then restarts; the same command stored twice under distinct
  msg ids (scheduled and manual), including two copies in flight at once;
* dispatcher restart mid-tick — the role dies after publishing the first of
  two matching commands, before the tick ack; a fresh role redelivers.

…and then asserts exactly one child execution, one `invocations` row and
matching counters per `(candybar, tick)` and per manual command UUID.

Crash injection subclasses the role's `NatsEventSink` and raises a
`BaseException` subclass right after a chosen real publish: the roles'
`except Exception` floors cannot turn it into a nak, so the message is left
unacked exactly as after a process death, while the role's `finally` still
detaches and drains.

THROWAWAY BUS ONLY. The suite deletes and recreates the production-named
`BLOODBANK_EVENTS` / `BLOODBANK_COMMANDS` streams (so the durables come back
with a short ack_wait) and recreates them empty at teardown so later CI steps
still find them. It refuses any server carrying non-candybar traffic.

Env-gated: CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1 (with DATABASE_URL) and
CANDYBAR_TEST_FAULT=1 (with NATS_URL pointing at a throwaway nats-server).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4, uuid5

import nats
import pytest
from nats.js import JetStreamContext
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError
from sqlalchemy import select

from candybar.bus import NatsEventSink
from candybar.dispatcher import command_for
from candybar.dispatcher import role as dispatcher_role
from candybar.events import build_candybar_invoke, build_tick_fired, tick_event_id
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarInvokeCommand,
    Envelope,
)
from candybar.events.vocabulary import TICK_NAMESPACE
from candybar.executor import role as executor_role
from candybar.heartbeat import NatsTickSink
from candybar.registry import (
    CandybarManifest,
    create_registry_engine,
    insert_candybar,
    session_scope,
)
from candybar.registry.models import Candybar, Invocation
from candybar.registry.store import ScheduleEntry
from test_executor_integration import _ensure_schema, _make_script

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1"
    or os.environ.get("CANDYBAR_TEST_PG") != "1"
    or os.environ.get("CANDYBAR_TEST_FAULT") != "1",
    reason="requires CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1, CANDYBAR_TEST_FAULT=1 "
    "(throwaway NATS_URL + DATABASE_URL)",
)

INVOKE_SUBJECT = "bloodbank.cmd.cron.candybar.invoke"
STREAMS = (
    ("BLOODBANK_EVENTS", ["bloodbank.evt.>"], RetentionPolicy.LIMITS),
    ("BLOODBANK_COMMANDS", ["bloodbank.cmd.>"], RetentionPolicy.WORK_QUEUE),
)
CANDYBAR_SUBJECT_PREFIXES = ("bloodbank.evt.cron.", "bloodbank.cmd.cron.")
# Short crash horizons so redelivery after an injected death lands in seconds.
DISPATCHER_ACK_WAIT = 2
EXECUTOR_ACK_WAIT = 3
# Historical UTC minutes: the dispatcher evaluates any tick minute it is
# handed, and fixed minutes keep the run independent of the wall clock.
T1 = datetime(2026, 1, 1, 0, 1, tzinfo=UTC)
T2 = T1 + timedelta(minutes=1)
T3 = T1 + timedelta(minutes=2)

ENV_LOG_SCRIPT = """#!/bin/sh
printf '%s|%s|%s\\n' "$CANDYBAR_TRIGGER" "$CANDYBAR_TICK_ID" "$CANDYBAR_INVOCATION_ID" >> {log}
exit 0
"""


class InjectedCrash(BaseException):
    """Abrupt role death: bypasses every `except Exception` nak floor."""


# One-shot crash predicates per role, armed by the scenario.
FAULTS: dict[str, Callable[[Envelope], bool] | None] = {"dispatcher": None, "executor": None}


def _fault_sink(role_name: str) -> type[NatsEventSink]:
    class FaultSink(NatsEventSink):
        async def publish(self, event: Envelope) -> None:
            await super().publish(event)  # the publish really lands first
            predicate = FAULTS[role_name]
            if predicate is not None and predicate(event):
                FAULTS[role_name] = None  # one-shot
                raise InjectedCrash(f"{role_name} died after publishing {event.type}")

    return FaultSink


async def _refuse_live_bus(js: JetStreamContext) -> None:
    """Fail — loudly, touching nothing — unless every stream and stored
    subject is candybar traffic. CANDYBAR_TEST_FAULT=1 asserts a throwaway
    bus; a skip here would let CI go green with the suite silently unrun."""
    for info in await js.streams_info():
        name = info.config.name
        if name not in {s for s, _, _ in STREAMS} and not name.startswith("CANDYBAR_TEST_"):
            pytest.fail(f"refusing to reset streams: foreign stream {name!r} — not a throwaway bus")
    for name, _, _ in STREAMS:
        try:
            info = await js.stream_info(name, subjects_filter=">")
        except NotFoundError:
            continue
        foreign = [s for s in (info.state.subjects or {}) if not s.startswith(CANDYBAR_SUBJECT_PREFIXES)]
        if foreign:
            pytest.fail(f"refusing to reset {name}: carries non-candybar traffic ({foreign[:3]})")


async def _reset_streams(js: JetStreamContext) -> None:
    for name, subjects, retention in STREAMS:
        try:
            await js.delete_stream(name)
        except NotFoundError:
            pass
        await js.add_stream(StreamConfig(name=name, subjects=subjects, retention=retention))


async def _until(predicate: Callable[[], Any], what: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


async def _consumer_exists(js: JetStreamContext, stream: str, durable: str) -> bool:
    try:
        await js.consumer_info(stream, durable)
        return True
    except NotFoundError:
        return False


async def _bus_idle(js: JetStreamContext) -> bool:
    """No tick left to evaluate and no command left to execute or ack."""
    commands = await js.stream_info("BLOODBANK_COMMANDS")
    if commands.state.messages:
        return False
    try:
        ticks = await js.consumer_info("BLOODBANK_EVENTS", dispatcher_role.DURABLE)
    except NotFoundError:
        return True
    return ticks.num_pending == 0 and ticks.num_ack_pending == 0


def _lines(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    return [line.split("|") for line in path.read_text().splitlines() if line]


def test_effectively_once_under_fault_injection(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)
    monkeypatch.setattr(executor_role, "ACK_WAIT_SECONDS", EXECUTOR_ACK_WAIT)
    monkeypatch.setattr(dispatcher_role, "NatsEventSink", _fault_sink("dispatcher"))
    monkeypatch.setattr(executor_role, "NatsEventSink", _fault_sink("executor"))
    FAULTS.update(dispatcher=None, executor=None)

    suffix = uuid4().hex[:8]
    ids = {k: uuid4() for k in ("a", "b", "c")}
    logs = {k: tmp_path / f"{k}.log" for k in ids}
    schedules = {"a": "* * * * *", "b": "* * * * *", "c": "0 0 1 1 *"}  # c: manual only

    async def scenario() -> None:
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        await _refuse_live_bus(js)
        await _reset_streams(js)
        engine = create_registry_engine()
        entries: dict[str, ScheduleEntry] = {}
        running: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}

        def start(role_name: str) -> asyncio.Task[None]:
            run = (
                dispatcher_role.run_dispatcher
                if role_name == "dispatcher"
                else executor_role.run_executor
            )
            stop = asyncio.Event()
            task = asyncio.create_task(run(stop))
            running[role_name] = (task, stop)
            return task

        async def stop_role(role_name: str) -> None:
            task, stop = running.pop(role_name)
            stop.set()
            await asyncio.wait_for(task, timeout=15)

        try:
            async with session_scope(engine) as session:
                for key, cid in ids.items():
                    script = _make_script(
                        tmp_path, f"{key}.sh", ENV_LOG_SCRIPT.format(log=logs[key])
                    )
                    name = f"itest-fault-{key}-{suffix}"
                    manifest = CandybarManifest.model_validate(
                        {"name": name, "schedule": schedules[key], "entrypoint": script}
                    )
                    await insert_candybar(session, manifest, candybar_id=cid)
                    entries[key] = ScheduleEntry(cid, name, schedules[key])

            start("executor")
            start("dispatcher")
            await _until(
                lambda: _consumer_exists(js, "BLOODBANK_EVENTS", dispatcher_role.DURABLE),
                "dispatcher durable",
            )
            await _until(
                lambda: _consumer_exists(js, "BLOODBANK_COMMANDS", executor_role.DURABLE),
                "executor durable",
            )
            ticks = NatsTickSink(js)

            # --- Fault 1: tick redelivery -----------------------------------
            t1 = build_tick_fired(T1)
            first = await ticks.publish_tick(t1)
            again = await ticks.publish_tick(build_tick_fired(T1))  # re-built, same minute
            assert again.duplicate is True and again.seq == first.seq
            # Replay past the duplicate window: same fact, fresh msg id.
            await js.publish(
                t1.subject, t1.model_dump_json().encode(), headers={"Nats-Msg-Id": f"replay-{uuid4()}"}
            )
            await _until(
                lambda: all(len(_lines(logs[k])) >= 1 for k in ("a", "b")), "T1 executions"
            )
            await _until(lambda: _bus_idle(js), "T1 settle")

            # --- Fault 2: dispatcher dies mid-tick ---------------------------
            t2 = build_tick_fired(T2)
            # Scoped to this test's candybars: a stray `* * * * *` row left in a
            # shared throwaway registry must not be the one that takes the crash.
            FAULTS["dispatcher"] = lambda e: (
                isinstance(e, CandybarInvokeCommand)
                and e.data.tick_id == t2.data.tick_id
                and e.data.candybar_id in (ids["a"], ids["b"])
            )
            dispatcher_task = running["dispatcher"][0]
            await ticks.publish_tick(t2)
            with pytest.raises(InjectedCrash):
                await asyncio.wait_for(dispatcher_task, timeout=15)
            running.pop("dispatcher")
            # Exactly one of the two T2 commands made it out before the death.
            await _until(
                lambda: sum(
                    1 for k in ("a", "b") if any(l[1] == t2.data.tick_id for l in _lines(logs[k]))
                ) == 1,
                "the single pre-crash T2 execution",
            )
            start("dispatcher")  # restart: same durable, tick redelivered after ack_wait
            await _until(
                lambda: all(
                    any(l[1] == t2.data.tick_id for l in _lines(logs[k])) for k in ("a", "b")
                ),
                "T2 executions after restart",
            )
            await _until(lambda: _bus_idle(js), "T2 settle")

            # --- Fault 3: executor dies after terminal publish, before ack ---
            t3 = build_tick_fired(T3)
            FAULTS["executor"] = lambda e: (
                isinstance(e, CandybarCompletedEvent)
                and e.data.candybar_id == ids["a"]
                and e.data.tick_id == t3.data.tick_id
            )
            await ticks.publish_tick(t3)
            await _until(lambda: FAULTS["executor"] is None, "executor crash after completed")
            # B's T3 runs alongside; stopping the role while it is in flight
            # would SIGTERM it into a `failed` row. Let it record first.
            b_t3_key = f"candybar:{ids['b']}:tick:{t3.data.tick_id}"

            async def b_t3_recorded() -> bool:
                async with session_scope(engine) as session:
                    row = await session.scalar(
                        select(Invocation).where(Invocation.idempotency_key == b_t3_key)
                    )
                    return row is not None

            await _until(b_t3_recorded, "B's T3 row before the executor restart")
            await stop_role("executor")
            start("executor")  # restart: redelivery finds the row, never re-runs
            await _until(lambda: _bus_idle(js), "T3 settle")

            # --- Fault 4: the same command stored twice, distinct msg ids ----
            replayed = command_for(entries["a"], t1)  # stable identity: same key as the T1 run
            await js.publish(
                INVOKE_SUBJECT,
                replayed.model_dump_json().encode(),
                headers={"Nats-Msg-Id": f"replay-{uuid4()}"},
            )
            sink = NatsEventSink(nc)
            manual = [
                build_candybar_invoke(
                    candybar_id=ids["c"], name=entries["c"].name, trigger="manual"
                )
                for _ in range(3)
            ]
            await sink.publish(manual[0])
            await sink.publish(manual[0])  # broker dedupe on the command id
            await js.publish(  # past the window: registry dedupe on the manual key
                INVOKE_SUBJECT,
                manual[0].model_dump_json().encode(),
                headers={"Nats-Msg-Id": f"replay-{uuid4()}"},
            )
            await sink.publish(manual[1])  # a distinct UUID is a distinct invoke
            # Two copies of one key back to back: both in flight together.
            for _ in range(2):
                await js.publish(
                    INVOKE_SUBJECT,
                    manual[2].model_dump_json().encode(),
                    headers={"Nats-Msg-Id": f"concurrent-{uuid4()}"},
                )
            await _until(lambda: len(_lines(logs["c"])) >= 3, "manual executions")
            await _until(lambda: _bus_idle(js), "final settle", timeout=30)
            await asyncio.sleep(1.5)  # grace for any stray second execution

            # --- Exactly once, everywhere -------------------------------------
            tick_ids = [t.data.tick_id for t in (t1, t2, t3)]
            for key in ("a", "b"):
                runs = _lines(logs[key])
                assert sorted(l[1] for l in runs) == sorted(tick_ids), (key, runs)
                assert {l[0] for l in runs} == {"scheduled"}
            manual_runs = _lines(logs["c"])
            assert sorted(l[2] for l in manual_runs) == sorted(
                str(m.data.invocation_id) for m in manual
            )
            assert {(l[0], l[1]) for l in manual_runs} == {("manual", "")}

            async with session_scope(engine) as session:
                for key in ("a", "b"):
                    rows = (
                        await session.scalars(
                            select(Invocation).where(Invocation.candybar_id == ids[key])
                        )
                    ).all()
                    assert sorted(r.idempotency_key for r in rows) == sorted(
                        f"candybar:{ids[key]}:tick:{tid}" for tid in tick_ids
                    )
                    assert all(r.tick_id.startswith("tick:") for r in rows)
                rows = (
                    await session.scalars(
                        select(Invocation).where(Invocation.candybar_id == ids["c"])
                    )
                ).all()
                assert sorted(r.idempotency_key for r in rows) == sorted(
                    f"candybar:{ids['c']}:manual:{m.command_id}" for m in manual
                )
                for key, cid in ids.items():
                    row = await session.get(Candybar, cid)
                    assert row.total_invoked == row.total_successful == 3, key

            # Tick dedupe key: the stored tick's msg id derives from tick_id only.
            stored = await js.get_msg("BLOODBANK_EVENTS", seq=first.seq)
            assert stored.headers["Nats-Msg-Id"] == str(tick_event_id(t1.data.tick_id))
            assert str(uuid5(TICK_NAMESPACE, t1.data.tick_id)) == stored.headers["Nats-Msg-Id"]

            # One schedule evaluator across the restart: the single durable.
            consumers = await js.consumers_info("BLOODBANK_EVENTS")
            assert [c.name for c in consumers] == [dispatcher_role.DURABLE]
        finally:
            FAULTS.update(dispatcher=None, executor=None)
            for role_name in list(running):
                # Teardown must reach the stream reset even if a role already
                # died (InjectedCrash) or fails to stop; the assertion that
                # failed above is the signal, not this.
                with contextlib.suppress(Exception, InjectedCrash):
                    await stop_role(role_name)
            async with session_scope(engine) as session:
                for cid in ids.values():
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)  # cascades invocations
            await engine.dispose()
            await _reset_streams(js)  # leave empty production-named streams behind
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))


def test_restarted_and_concurrent_dispatchers_share_one_durable(monkeypatch) -> None:
    """AD-5: however many dispatcher roles start, restart or overlap, the
    events stream holds exactly one tick consumer — the production durable."""
    _ensure_schema()
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)

    async def scenario() -> None:
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        await _refuse_live_bus(js)
        await _reset_streams(js)
        try:
            for _ in range(2):  # sequential restarts
                stop = asyncio.Event()
                task = asyncio.create_task(dispatcher_role.run_dispatcher(stop))
                await _until(
                    lambda: _consumer_exists(js, "BLOODBANK_EVENTS", dispatcher_role.DURABLE),
                    "dispatcher durable",
                )
                stop.set()
                await asyncio.wait_for(task, timeout=15)
            stops = [asyncio.Event(), asyncio.Event()]  # two roles at once
            tasks = [asyncio.create_task(dispatcher_role.run_dispatcher(s)) for s in stops]

            async def both_pulling() -> bool:
                try:
                    info = await js.consumer_info("BLOODBANK_EVENTS", dispatcher_role.DURABLE)
                except NotFoundError:
                    return False
                return info.num_waiting >= 2  # a pull request from each role

            await _until(both_pulling, "both dispatcher roles pulling the one durable")
            consumers = await js.consumers_info("BLOODBANK_EVENTS")
            assert [c.name for c in consumers] == [dispatcher_role.DURABLE]
            assert consumers[0].config.filter_subject == dispatcher_role.TICK_FILTER
            for s in stops:
                s.set()
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=15)
            consumers = await js.consumers_info("BLOODBANK_EVENTS")
            assert [c.name for c in consumers] == [dispatcher_role.DURABLE]
        finally:
            await _reset_streams(js)
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
