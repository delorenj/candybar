"""Story 2.4 — skip the rest of a day, proven live (AD-10, AD-13).

Two layers:

* the registry write path (`add_rest_of_day_skip` / `list_skips`) against a
  real Postgres: idempotent storage with the first note kept, unknown
  candybar, and the day-intersecting window read beside occurrence rows;
* the PRODUCTION `run_dispatcher` + `run_executor` roles over a real NATS
  JetStream with real child processes: a stored day skip suppresses every
  remaining fire of its UTC date — one `skipped` fact (`reason=rest_of_day`)
  per suppressed fire, published on the tick it would have fired — the next
  UTC day dispatches normally, and a dispatcher gap spanning a fully skipped
  day runs the following day's latest missed fire as `misfire`.

THROWAWAY BUS ONLY for the role test (same guard as the Story 2.1 fault
suite: it resets the production-named streams); the cursor row is restored
at teardown. Env-gated: CANDYBAR_TEST_PG=1 for the store test, plus
CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_FAULT=1 for the role test.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import nats
import pytest
from nats.js.errors import NotFoundError
from sqlalchemy import delete, func, select
from sqlalchemy import update as sql_update

from candybar.dispatcher import role as dispatcher_role
from candybar.events import build_tick_fired
from candybar.events.vocabulary import tick_id_for_minute
from candybar.executor import role as executor_role
from candybar.heartbeat import NatsTickSink
from candybar.registry import (
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    CandybarManifest,
    UnknownCandybarError,
    add_occurrence_skip,
    add_rest_of_day_skip,
    advance_dispatch_cursor,
    create_registry_engine,
    get_dispatch_cursor,
    insert_candybar,
    list_skips,
    rest_of_day_skip_id,
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

# Historical UTC dates: the dispatcher keys off consumed ticks (AD-1). The
# role test crosses a real UTC midnight: SKIP_DAY's 23:40/23:45 fires are
# suppressed and the next day's 00:00/00:05 run.
BASE = datetime(2025, 7, 7, 23, 40, tzinfo=UTC)  # SKIP_DAY 23:40
SKIP_DAY = date(2025, 7, 7)
NEXT_DAY = date(2025, 7, 8)
REGISTERED = datetime(2025, 1, 1, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def _manifest(name: str, schedule: str = "*/5 * * * *", entrypoint: str = "/bin/true") -> CandybarManifest:
    return CandybarManifest.model_validate(
        {"name": name, "schedule": schedule, "entrypoint": entrypoint}
    )


@needs_pg
def test_rest_of_day_skip_store_is_idempotent_and_window_read() -> None:
    _ensure_schema()

    async def scenario() -> None:
        engine = create_registry_engine()
        cid = uuid4()
        try:
            async with session_scope(engine) as session:
                await insert_candybar(
                    session, _manifest(f"itest-dayskip-store-{cid.hex[:8]}"), candybar_id=cid
                )
            async with session_scope(engine) as session:
                first, created = await add_rest_of_day_skip(
                    session, cid, SKIP_DAY, reason="noisy during the maintenance window"
                )
            assert created is True
            assert first.id == rest_of_day_skip_id(cid, SKIP_DAY)
            assert (first.kind, first.fire_time, first.day) == (SKIP_REST_OF_DAY, None, SKIP_DAY)
            assert first.reason == "noisy during the maintenance window"

            # Storing the same skip again stores nothing new and says so; the
            # first row (and its note) is kept.
            async with session_scope(engine) as session:
                again, created_again = await add_rest_of_day_skip(
                    session, cid, SKIP_DAY, reason="dup"
                )
            assert created_again is False
            assert again == first
            async with session_scope(engine) as session:
                count = await session.scalar(
                    select(func.count()).select_from(Skip).where(Skip.candybar_id == cid)
                )
            assert count == 1

            async with session_scope(engine) as session:
                await add_rest_of_day_skip(session, cid, NEXT_DAY)
                await add_occurrence_skip(session, cid, at(0))
            # Day-intersecting window read, beside occurrence rows.
            d1_morning = datetime(2025, 7, 7, 10, 0, tzinfo=UTC)
            d1_late = datetime(2025, 7, 7, 23, 0, tzinfo=UTC)
            d2_start = datetime(2025, 7, 8, 0, 0, tzinfo=UTC)
            d3_start = datetime(2025, 7, 9, 0, 0, tzinfo=UTC)
            d4_start = datetime(2025, 7, 10, 0, 0, tzinfo=UTC)
            async with session_scope(engine) as session:
                # Occurrence in the window + SKIP_DAY intersects, NEXT_DAY not.
                mixed = [s for s in await list_skips(session, d1_late, d2_start)]
                both_days = [s for s in await list_skips(session, d1_late, d3_start)]
                none = [s for s in await list_skips(session, d4_start, d4_start + timedelta(hours=1))]
                # [NEXT_DAY 00:00, day after 00:00): only NEXT_DAY intersects
                # (the window end is exclusive down to the minute).
                boundary = [s for s in await list_skips(session, d2_start, d3_start)]
                # A window in the middle of SKIP_DAY still intersects it.
                day1_only = [s for s in await list_skips(session, d1_morning, d1_morning + timedelta(hours=1))]
            mine = [s for s in mixed if s.candybar_id == cid]
            # Occurrence row first (fire_time set), the day row last.
            assert [(s.kind, s.fire_time, s.day) for s in mine] == [
                (SKIP_OCCURRENCE, at(0), None),
                (SKIP_REST_OF_DAY, None, SKIP_DAY),
            ]
            both = [s for s in both_days if s.candybar_id == cid and s.kind == SKIP_REST_OF_DAY]
            assert [s.day for s in both] == [SKIP_DAY, NEXT_DAY]
            assert [s for s in none if s.candybar_id == cid] == []
            assert [s.day for s in boundary if s.candybar_id == cid and s.day] == [NEXT_DAY]
            assert [s.day for s in day1_only if s.candybar_id == cid and s.day] == [SKIP_DAY]

            # Unknown candybar: nothing stored, typed error (no sqlalchemy leak).
            ghost = uuid4()
            with pytest.raises(UnknownCandybarError):
                async with session_scope(engine) as session:
                    await add_rest_of_day_skip(session, ghost, SKIP_DAY)
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
def test_stored_day_skip_suppresses_until_utc_midnight_live(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)
    monkeypatch.setattr(executor_role, "ACK_WAIT_SECONDS", EXECUTOR_ACK_WAIT)

    suffix = uuid4().hex[:8]
    keys = ("skipped", "neighbour", "hourly")
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
            # One minute before the first tick (SKIP_DAY 23:39).
            await advance_dispatch_cursor(session, dispatcher_role.DURABLE, at(-1))

        try:
            async with session_scope(engine) as session:
                schedules = {"skipped": "*/5 * * * *", "neighbour": "*/5 * * * *", "hourly": "0 * * * *"}
                for key in keys:
                    script = _make_script(tmp_path, f"{key}.sh", ENV_LOG_SCRIPT.format(log=logs[key]))
                    await insert_candybar(
                        session,
                        _manifest(f"itest-dayskip-{key}-{suffix}", schedules[key], script),
                        candybar_id=ids[key],
                    )
                await session.execute(
                    sql_update(Candybar).where(Candybar.id.in_(ids.values())).values(created_at=REGISTERED)
                )
            # The operator's override: quiet ONE candybar for the rest of
            # SKIP_DAY (23:40 and 23:45 remain; midnight ends it).
            async with session_scope(engine) as session:
                _, created = await add_rest_of_day_skip(session, ids["skipped"], SKIP_DAY)
            assert created

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

            # --- A: across the UTC midnight: 23:40, 23:45 (SKIP_DAY,         -
            # suppressed), 00:00, 00:05 (NEXT_DAY, dispatched). One at a time:
            # a tick with newer ticks queued behind it is a backlog tick. -----
            # The 23:45 -> 00:00 tick is a 15-minute gap: the skipped
            # candybar's remaining SKIP_DAY fires (23:50, 23:55) are recorded
            # rest_of_day by coalescing, and the neighbour coalesces them to a
            # 23:55 misfire (Story 2.2 behavior for the unskipped candybar).
            for offset in (0, 5, 20, 25):  # 23:40, 23:45, 00:00, 00:05
                await ticks.publish_tick(build_tick_fired(at(offset)))
                await _until(lambda o=offset: cursor_at(at(o)), f"tick {at(offset)} dispatched")
            await _until(lambda: len(_lines(logs["skipped"])) >= 2, "skipped candybar runs")
            await _until(lambda: len(_lines(logs["neighbour"])) >= 5, "neighbour runs")
            await _until(lambda: len(_lines(logs["hourly"])) >= 1, "hourly runs at midnight")
            await settle("A settle")

            assert runs("skipped") == [["scheduled", tid(at(20))], ["scheduled", tid(at(25))]]
            assert runs("neighbour") == [
                ["scheduled", tid(at(0))],
                ["scheduled", tid(at(5))],
                ["misfire", tid(at(15))],  # coalesced catch-up for the 15m gap
                ["scheduled", tid(at(20))],
                ["scheduled", tid(at(25))],
            ]
            assert runs("hourly") == [["scheduled", tid(at(20))]]
            assert sorted(r.tick_id for r in await rows("skipped")) == sorted(
                [tid(at(20)), tid(at(25))]
            )
            # Every suppressed SKIP_DAY fire was recorded rest_of_day: 23:40
            # and 23:45 on their own ticks, 23:50 and 23:55 when the 00:00
            # tick closed the gap. Nothing was recorded for NEXT_DAY.
            facts = await _skipped_events(js)
            assert [
                (e.data.tick_id, e.data.reason)
                for e in facts if e.data.candybar_id == ids["skipped"]
            ] == [(tid(at(m)), "rest_of_day") for m in (0, 5, 10, 15)]
            assert [
                (e.data.tick_id, e.data.reason)
                for e in facts if e.data.candybar_id == ids["neighbour"]
            ] == [(tid(at(10)), "misfire_coalesced")]
            assert [e for e in facts if e.data.candybar_id == ids["hourly"]] == []
            async with session_scope(engine) as session:
                row = await session.get(Candybar, ids["skipped"])
                assert (row.total_invoked, row.total_successful) == (2, 2)

            # --- B: a gap spanning a fully skipped day ------------------------
            # The hourly candybar is quiet for NEXT_DAY; the daemon is then
            # down until 00:30 the day after. The 23 skipped-day fires are
            # recorded rest_of_day; the following day's 00:00 fire runs as
            # misfire. The */5 candybars leave the inventory first so their
            # coalesced backlog does not flood the bus (the hourly candybar
            # is the one under test here).
            async with session_scope(engine) as session:
                await add_rest_of_day_skip(session, ids["hourly"], NEXT_DAY)
                for key in ("skipped", "neighbour"):
                    await session.delete(await session.get(Candybar, ids[key]))
            gap_tick = at(20) + timedelta(days=1, minutes=30)  # Jul 9 00:30
            await ticks.publish_tick(build_tick_fired(gap_tick))
            await _until(lambda: len(_lines(logs["hourly"])) >= 2, "hourly misfire")
            await settle("B settle")

            # The following day's latest missed fire (Jul 9 00:00) ran as the
            # catch-up; nothing from the skipped day ran.
            assert runs("hourly")[1:] == [["misfire", tid(at(20) + timedelta(days=1))]]
            facts = await _skipped_events(js)
            hourly_facts = [e for e in facts if e.data.candybar_id == ids["hourly"]]
            # 01:00..23:00 on NEXT_DAY, oldest first, each rest_of_day.
            assert [(e.data.tick_id, e.data.reason) for e in hourly_facts] == [
                (tid(at(20 + 60 * h)), "rest_of_day") for h in range(1, 24)
            ]
            # No invocation row exists for any suppressed hourly fire.
            hourly_rows = await rows("hourly")
            assert sorted(r.tick_id for r in hourly_rows) == sorted(
                [tid(at(20)), tid(at(20) + timedelta(days=1))]
            )
            async with session_scope(engine) as session:
                row = await session.get(Candybar, ids["hourly"])
                assert (row.total_invoked, row.total_successful) == (2, 2)
                assert await get_dispatch_cursor(session, dispatcher_role.DURABLE) == gap_tick
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

    asyncio.run(asyncio.wait_for(scenario(), timeout=180))
