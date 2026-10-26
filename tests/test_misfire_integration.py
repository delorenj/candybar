"""Story 2.2 — misfire coalesce-to-latest, proven live (AD-6).

Drives the PRODUCTION `run_dispatcher` and `run_executor` role coroutines
over a real NATS JetStream and a real Postgres registry, with real child
processes, through both downtime shapes the dispatcher can wake up to:

* whole-daemon downtime — no ticks were published while it was down; the
  cursor sits N>1 fires behind the first tick it consumes;
* dispatcher-only downtime — the heartbeat kept publishing while the
  dispatcher role was stopped, so a real tick backlog is queued on the
  durable; it must coalesce, never replay as scheduled fires;

…plus the 24h cap. It asserts exactly one child run per candybar with
`CANDYBAR_TRIGGER=misfire` and the latest missed tick_id, the matching
`invocations` row, one `bloodbank.cron.candybar.skipped`
(`reason=misfire_coalesced`) per earlier missed fire on BLOODBANK_EVENTS,
and normal scheduled dispatch for candybars with nothing missed.

THROWAWAY BUS ONLY (same guard as the Story 2.1 fault suite: it resets the
production-named streams). The registry cursor row is restored at teardown.
Env-gated: CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1, CANDYBAR_TEST_FAULT=1.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import nats
import pytest
from nats.js import JetStreamContext
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError
from sqlalchemy import delete, select, update

from candybar.dispatcher import role as dispatcher_role
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarSkippedEvent
from candybar.events.vocabulary import tick_id_for_minute
from candybar.executor import role as executor_role
from candybar.heartbeat import NatsTickSink
from candybar.registry import (
    CandybarManifest,
    advance_dispatch_cursor,
    create_registry_engine,
    get_dispatch_cursor,
    insert_candybar,
    session_scope,
)
from candybar.registry.models import Candybar, DispatchCursor, Invocation
from test_dispatch_fault_injection_integration import (
    DISPATCHER_ACK_WAIT,
    ENV_LOG_SCRIPT,
    EXECUTOR_ACK_WAIT,
    _bus_idle,
    _consumer_exists,
    _lines,
    _refuse_live_bus,
    _reset_streams,
    _until,
)
from test_executor_integration import _ensure_schema, _make_script

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1"
    or os.environ.get("CANDYBAR_TEST_PG") != "1"
    or os.environ.get("CANDYBAR_TEST_FAULT") != "1",
    reason="requires CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1, CANDYBAR_TEST_FAULT=1 "
    "(throwaway NATS_URL + DATABASE_URL)",
)

SKIPPED_SUBJECT = "bloodbank.evt.cron.candybar.skipped"
# Historical UTC minutes: the dispatcher keys off the ticks it consumes, never
# the wall clock (AD-1), so the scenario is independent of when it runs.
BASE = datetime(2025, 6, 2, 10, 0, tzinfo=UTC)  # the cursor before downtime #1
REGISTERED = datetime(2025, 1, 1, tzinfo=UTC)  # well before every fire tested


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


async def _skipped_events(js: JetStreamContext) -> list[CandybarSkippedEvent]:
    """Every skipped fact stored on BLOODBANK_EVENTS (ephemeral reader)."""
    sub = await js.pull_subscribe(
        SKIPPED_SUBJECT,
        stream="BLOODBANK_EVENTS",
        config=ConsumerConfig(
            filter_subject=SKIPPED_SUBJECT,
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.NONE,
            inactive_threshold=5,
        ),
    )
    out: list[CandybarSkippedEvent] = []
    try:
        while True:
            try:
                batch = await sub.fetch(256, timeout=1)
            except (TimeoutError, nats.errors.TimeoutError):
                break
            for msg in batch:
                event = parse_envelope(json.loads(msg.data))
                assert isinstance(event, CandybarSkippedEvent)
                out.append(event)
    finally:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()
    return out


def _skipped_for(events: list[CandybarSkippedEvent], cid: UUID) -> list[str]:
    return sorted(
        (e.data.tick_id for e in events if e.data.candybar_id == cid),
        key=lambda t: int(t[5:]),
    )


def test_misfire_coalesces_after_daemon_and_dispatcher_downtime(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)
    monkeypatch.setattr(executor_role, "ACK_WAIT_SECONDS", EXECUTOR_ACK_WAIT)

    suffix = uuid4().hex[:8]
    schedules = {
        "every5": "*/5 * * * *",  # misses fires in both gaps
        "daily": "23 10 * * *",  # nothing missed; matches the first tick exactly
        "yearly": "0 0 1 1 *",  # nothing missed, nothing matching
        "hourly": "0 * * * *",  # the 24h cap case (registered for scenario C)
    }
    ids = {k: uuid4() for k in schedules}
    logs = {k: tmp_path / f"{k}.log" for k in schedules}

    async def scenario() -> None:
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        await _refuse_live_bus(js)
        await _reset_streams(js)
        engine = create_registry_engine()
        running: dict[str, tuple[asyncio.Task[None], asyncio.Event]] = {}

        def start(role_name: str) -> None:
            run = (
                dispatcher_role.run_dispatcher
                if role_name == "dispatcher"
                else executor_role.run_executor
            )
            stop = asyncio.Event()
            running[role_name] = (asyncio.create_task(run(stop)), stop)

        async def stop_role(role_name: str) -> None:
            task, stop = running.pop(role_name)
            stop.set()
            await asyncio.wait_for(task, timeout=15)

        async def register(keys: tuple[str, ...]) -> None:
            async with session_scope(engine) as session:
                for key in keys:
                    script = _make_script(tmp_path, f"{key}.sh", ENV_LOG_SCRIPT.format(log=logs[key]))
                    manifest = CandybarManifest.model_validate(
                        {"name": f"itest-misfire-{key}-{suffix}", "schedule": schedules[key],
                         "entrypoint": script}
                    )
                    await insert_candybar(session, manifest, candybar_id=ids[key])
                # Registered "long ago": the historical fires below are not
                # excluded by the created_at floor.
                await session.execute(
                    update(Candybar)
                    .where(Candybar.id.in_([ids[k] for k in keys]))
                    .values(created_at=REGISTERED)
                )

        async def rows(key: str) -> list[Invocation]:
            async with session_scope(engine) as session:
                return list(
                    (await session.scalars(
                        select(Invocation).where(Invocation.candybar_id == ids[key])
                    )).all()
                )

        async def settle(what: str) -> None:
            await _until(lambda: _bus_idle(js), what, timeout=30)
            await asyncio.sleep(1.5)  # grace for any stray extra execution

        # The cursor row is shared registry state: snapshot, set, restore.
        async with session_scope(engine) as session:
            original = await get_dispatch_cursor(session, dispatcher_role.DURABLE)
            await session.execute(
                delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
            )
            await advance_dispatch_cursor(session, dispatcher_role.DURABLE, BASE)

        try:
            await register(("every5", "daily", "yearly"))
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

            # --- A: whole-daemon downtime 10:00 -> first tick 10:23 ----------
            await ticks.publish_tick(build_tick_fired(at(23)))
            await _until(lambda: len(_lines(logs["every5"])) >= 1, "every5 misfire run")
            await _until(lambda: len(_lines(logs["daily"])) >= 1, "daily scheduled run")
            await settle("A settle")

            assert _lines(logs["every5"])[0][:2] == ["misfire", tid(at(20))]
            assert [l[:2] for l in _lines(logs["every5"])] == [["misfire", tid(at(20))]]
            assert [l[:2] for l in _lines(logs["daily"])] == [["scheduled", tid(at(23))]]
            assert _lines(logs["yearly"]) == []
            (row,) = await rows("every5")
            assert (row.trigger, row.tick_id, row.result) == ("misfire", tid(at(20)), "success")
            assert row.idempotency_key == f"candybar:{ids['every5']}:tick:{tid(at(20))}"
            assert row.id == UUID(_lines(logs["every5"])[0][2])
            (daily_row,) = await rows("daily")
            assert daily_row.trigger == "scheduled"
            skipped = await _skipped_events(js)
            assert _skipped_for(skipped, ids["every5"]) == [tid(at(m)) for m in (5, 10, 15)]
            assert {e.data.reason for e in skipped} == {"misfire_coalesced"}
            assert _skipped_for(skipped, ids["daily"]) == _skipped_for(skipped, ids["yearly"]) == []

            # --- B: dispatcher-only downtime, heartbeat kept publishing ------
            await stop_role("dispatcher")
            for m in range(24, 42):  # a real backlog: 10:24 .. 10:41 queued on the durable
                await ticks.publish_tick(build_tick_fired(at(m)))
            info = await js.consumer_info("BLOODBANK_EVENTS", dispatcher_role.DURABLE)
            assert info.num_pending == 18
            start("dispatcher")
            await _until(lambda: len(_lines(logs["every5"])) >= 2, "every5 backlog misfire run")
            await settle("B settle")

            every5 = [l[:2] for l in _lines(logs["every5"])]
            # Never replayed as scheduled fires: one misfire run for the latest.
            assert every5 == [["misfire", tid(at(20))], ["misfire", tid(at(40))]]
            assert [l[:2] for l in _lines(logs["daily"])] == [["scheduled", tid(at(23))]]
            assert _lines(logs["yearly"]) == []
            skipped = await _skipped_for_all(js, ids["every5"])
            assert skipped == [tid(at(m)) for m in (5, 10, 15, 25, 30, 35)]
            async with session_scope(engine) as session:
                assert await get_dispatch_cursor(session, dispatcher_role.DURABLE) == at(41)

            # --- C: downtime beyond the 24h cap ------------------------------
            async with session_scope(engine) as session:
                for key in ("every5", "daily", "yearly"):  # keep the cap case isolated
                    await session.delete(await session.get(Candybar, ids[key]))
            await register(("hourly",))
            cap_tick = datetime(2025, 6, 4, 12, 30, tzinfo=UTC)
            async with session_scope(engine) as session:
                await advance_dispatch_cursor(
                    session, dispatcher_role.DURABLE, cap_tick - timedelta(hours=30)
                )
            await ticks.publish_tick(build_tick_fired(cap_tick))
            await _until(lambda: len(_lines(logs["hourly"])) >= 1, "hourly cap misfire run")
            await settle("C settle")

            assert [l[:2] for l in _lines(logs["hourly"])] == [
                ["misfire", tid(cap_tick.replace(minute=0))]
            ]
            (cap_row,) = await rows("hourly")
            assert cap_row.trigger == "misfire"
            hourly_skipped = await _skipped_for_all(js, ids["hourly"])
            floor = cap_tick - timedelta(hours=24)
            fires = [datetime.fromtimestamp(int(t[5:]) * 60, UTC) for t in hourly_skipped]
            assert len(fires) == 23 and min(fires) >= floor  # nothing older than T-24h
            async with session_scope(engine) as session:
                row = await session.get(Candybar, ids["hourly"])
                assert row.total_invoked == row.total_successful == 1
        finally:
            for role_name in list(running):
                with contextlib.suppress(Exception):
                    await stop_role(role_name)
            async with session_scope(engine) as session:
                for cid in ids.values():
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)  # cascades invocations
                await session.execute(
                    delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
                )
                if original is not None:
                    await advance_dispatch_cursor(session, dispatcher_role.DURABLE, original)
            await engine.dispose()
            await _reset_streams(js)  # leave empty production-named streams behind
            with contextlib.suppress(NotFoundError):
                await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=150))


async def _skipped_for_all(js: JetStreamContext, cid: UUID) -> list[str]:
    return _skipped_for(await _skipped_events(js), cid)
