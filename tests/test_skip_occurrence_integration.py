"""Story 2.3 — skip a single occurrence, proven live (AD-10, AD-13).

Two layers:

* the registry write path (`add_occurrence_skip` / `list_skips`) against a
  real Postgres: idempotent storage, unknown candybar, UTC window read;
* the PRODUCTION `run_dispatcher` + `run_executor` roles over a real NATS
  JetStream with real child processes: a stored skip suppresses exactly that
  fire (no command, no `invocations` row, one `skipped` fact with
  `reason=occurrence`), the next fire runs normally, upcoming-fire
  computation off the same registry rows omits the fire, and a gap whose
  latest missed fire is skipped runs the previous fire as `misfire`.

THROWAWAY BUS ONLY for the role test (same guard as the Story 2.1 fault
suite: it resets the production-named streams); the cursor row is restored
at teardown. Env-gated: CANDYBAR_TEST_PG=1 for the store test, plus
CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_FAULT=1 for the role test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import nats
import pytest
from nats.js.errors import NotFoundError
from sqlalchemy import delete, func, select, update

from candybar.dispatcher import role as dispatcher_role
from candybar.dispatcher.schedule import fires_between, group_skips, next_fire
from candybar.events import build_tick_fired
from candybar.events.vocabulary import tick_id_for_minute
from candybar.executor import role as executor_role
from candybar.heartbeat import NatsTickSink
from candybar.registry import (
    SKIP_OCCURRENCE,
    CandybarManifest,
    UnknownCandybarError,
    add_occurrence_skip,
    advance_dispatch_cursor,
    create_registry_engine,
    get_dispatch_cursor,
    insert_candybar,
    list_skips,
    occurrence_skip_id,
    session_scope,
)
from candybar.registry.models import Candybar, DispatchCursor, Invocation, Skip
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
from test_misfire_integration import _skipped_events

needs_pg = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_PG=1 (throwaway DATABASE_URL)",
)
needs_bus = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1"
    or os.environ.get("CANDYBAR_TEST_PG") != "1"
    or os.environ.get("CANDYBAR_TEST_FAULT") != "1",
    reason="requires CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1, CANDYBAR_TEST_FAULT=1 "
    "(throwaway NATS_URL + DATABASE_URL)",
)

# Historical UTC minutes: the dispatcher keys off consumed ticks (AD-1).
BASE = datetime(2025, 7, 7, 10, 0, tzinfo=UTC)
REGISTERED = datetime(2025, 1, 1, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def _manifest(name: str, entrypoint: str = "/bin/true") -> CandybarManifest:
    return CandybarManifest.model_validate(
        {"name": name, "schedule": "*/5 * * * *", "entrypoint": entrypoint}
    )


@needs_pg
def test_occurrence_skip_store_is_idempotent_and_window_read() -> None:
    _ensure_schema()

    async def scenario() -> None:
        engine = create_registry_engine()
        cid = uuid4()
        try:
            async with session_scope(engine) as session:
                await insert_candybar(
                    session, _manifest(f"itest-skip-store-{cid.hex[:8]}"), candybar_id=cid
                )
            async with session_scope(engine) as session:
                first, created = await add_occurrence_skip(
                    session, cid, at(10), reason="known-bad upstream window"
                )
            assert created is True
            assert first.id == occurrence_skip_id(cid, at(10))
            assert (first.kind, first.fire_time, first.day) == (SKIP_OCCURRENCE, at(10), None)
            assert first.reason == "known-bad upstream window"

            # Storing the same skip again stores nothing new and says so.
            async with session_scope(engine) as session:
                again, created_again = await add_occurrence_skip(session, cid, at(10), reason="dup")
            assert created_again is False
            assert again == first  # the first row (and its note) is kept
            async with session_scope(engine) as session:
                count = await session.scalar(
                    select(func.count()).select_from(Skip).where(Skip.candybar_id == cid)
                )
            assert count == 1

            async with session_scope(engine) as session:
                await add_occurrence_skip(session, cid, at(20))
            # Half-open UTC window, oldest first; fire_time comes back aware UTC.
            async with session_scope(engine) as session:
                window = [s for s in await list_skips(session, at(10), at(20)) if s.candybar_id == cid]
                wide = [s for s in await list_skips(session, at(0), at(30)) if s.candybar_id == cid]
            assert [s.fire_time for s in window] == [at(10)]
            assert [s.fire_time for s in wide] == [at(10), at(20)]
            assert all(s.fire_time.tzinfo is not None for s in wide)

            # Unknown candybar: nothing stored, typed error (no sqlalchemy leak).
            ghost = uuid4()
            with pytest.raises(UnknownCandybarError):
                async with session_scope(engine) as session:
                    await add_occurrence_skip(session, ghost, at(10))
            async with session_scope(engine) as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(Skip).where(Skip.candybar_id == ghost)
                    )
                ) == 0

            # Skips are override state owned by the candybar: deleting it cascades.
            async with session_scope(engine) as session:
                await session.delete(await session.get(Candybar, cid))
            async with session_scope(engine) as session:
                assert (
                    await session.scalar(
                        select(func.count()).select_from(Skip).where(Skip.candybar_id == cid)
                    )
                ) == 0
        finally:
            async with session_scope(engine) as session:
                row = await session.get(Candybar, cid)
                if row is not None:
                    await session.delete(row)
            await engine.dispose()

    asyncio.run(scenario())


@needs_bus
def test_stored_skip_suppresses_exactly_that_fire_live(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)
    monkeypatch.setattr(executor_role, "ACK_WAIT_SECONDS", EXECUTOR_ACK_WAIT)

    suffix = uuid4().hex[:8]
    keys = ("skipped", "neighbour")  # same */5 schedule; only one carries the skip
    ids = {k: uuid4() for k in keys}
    logs = {k: tmp_path / f"{k}.log" for k in keys}

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

        def runs(key: str) -> list[list[str]]:
            # Commands execute concurrently: order the log by fire minute.
            return sorted((line[:2] for line in _lines(logs[key])), key=lambda r: int(r[1][5:]))

        async with session_scope(engine) as session:
            original = await get_dispatch_cursor(session, dispatcher_role.DURABLE)
            await session.execute(
                delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
            )
            await advance_dispatch_cursor(session, dispatcher_role.DURABLE, at(4))

        try:
            async with session_scope(engine) as session:
                for key in keys:
                    script = _make_script(tmp_path, f"{key}.sh", ENV_LOG_SCRIPT.format(log=logs[key]))
                    await insert_candybar(
                        session, _manifest(f"itest-skip-{key}-{suffix}", script), candybar_id=ids[key]
                    )
                await session.execute(
                    update(Candybar).where(Candybar.id.in_(ids.values())).values(created_at=REGISTERED)
                )
            # The operator's override: skip the 10:10 fire of ONE candybar.
            async with session_scope(engine) as session:
                _, created = await add_occurrence_skip(session, ids["skipped"], at(10))
            assert created

            # Upcoming-fire computation off the same registry rows omits 10:10.
            async with session_scope(engine) as session:
                overrides = group_skips(await list_skips(session, at(5), at(30)))
            mine = overrides[ids["skipped"]]
            assert next_fire("*/5 * * * *", at(6), skips=mine) == at(15)
            assert list(fires_between("*/5 * * * *", at(5), at(20), skips=mine)) == [at(5), at(15)]
            assert ids["neighbour"] not in overrides

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

            async def cursor_at(minute: datetime) -> bool:
                async with session_scope(engine) as session:
                    return await get_dispatch_cursor(session, dispatcher_role.DURABLE) == minute

            # --- A: ticks 10:05, 10:10, 10:15, each dispatched in turn -------
            # One at a time: a tick with newer ticks queued behind it is a
            # backlog tick (Story 2.2) and would defer to the newest one.
            for minute in (5, 10, 15):
                await ticks.publish_tick(build_tick_fired(at(minute)))
                await _until(lambda m=minute: cursor_at(at(m)), f"tick 10:{minute:02d} dispatched")
            await _until(lambda: len(_lines(logs["skipped"])) >= 2, "skipped candybar runs")
            await _until(lambda: len(_lines(logs["neighbour"])) >= 3, "neighbour runs")
            await settle("A settle")

            assert runs("skipped") == [["scheduled", tid(at(5))], ["scheduled", tid(at(15))]]
            assert runs("neighbour") == [["scheduled", tid(at(m))] for m in (5, 10, 15)]
            assert sorted(r.tick_id for r in await rows("skipped")) == sorted(
                [tid(at(5)), tid(at(15))]
            )
            assert all(r.idempotency_key != f"candybar:{ids['skipped']}:tick:{tid(at(10))}"
                       for r in await rows("skipped"))
            facts = await _skipped_events(js)
            assert [(e.data.candybar_id, e.data.tick_id, e.data.reason) for e in facts] == [
                (ids["skipped"], tid(at(10)), "occurrence")
            ]
            async with session_scope(engine) as session:
                row = await session.get(Candybar, ids["skipped"])
                assert (row.total_invoked, row.total_successful) == (2, 2)

            # --- B: a gap whose latest missed fire is skipped ---------------
            # Daemon down after 10:15; the operator skipped 10:25; first tick 10:28.
            async with session_scope(engine) as session:
                await add_occurrence_skip(session, ids["skipped"], at(25))
            await ticks.publish_tick(build_tick_fired(at(28)))
            await _until(lambda: len(_lines(logs["skipped"])) >= 3, "skipped candybar misfire")
            await _until(lambda: len(_lines(logs["neighbour"])) >= 4, "neighbour misfire")
            await settle("B settle")

            assert runs("skipped")[2:] == [["misfire", tid(at(20))]]
            assert runs("neighbour")[3:] == [["misfire", tid(at(25))]]
            facts = await _skipped_events(js)
            mine_facts = sorted(
                (e.data.tick_id, e.data.reason)
                for e in facts if e.data.candybar_id == ids["skipped"]
            )
            assert mine_facts == sorted(
                [(tid(at(10)), "occurrence"), (tid(at(25)), "occurrence")]
            )
            assert [
                (e.data.tick_id, e.data.reason)
                for e in facts if e.data.candybar_id == ids["neighbour"]
            ] == [(tid(at(20)), "misfire_coalesced")]
            async with session_scope(engine) as session:
                assert await get_dispatch_cursor(session, dispatcher_role.DURABLE) == at(28)
        finally:
            for role_name in list(running):
                with contextlib.suppress(Exception):
                    await stop_role(role_name)
            async with session_scope(engine) as session:
                for cid in ids.values():
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)  # cascades invocations + skips
                await session.execute(
                    delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
                )
                if original is not None:
                    await advance_dispatch_cursor(session, dispatcher_role.DURABLE, original)
            await engine.dispose()
            await _reset_streams(js)
            with contextlib.suppress(NotFoundError):
                await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))
