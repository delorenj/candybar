"""Story 5.6 — the skip operations API, proven live (contract "Skip operations").

Through the REAL routes (the production `create_app` over the real registry
and a real JetStream sink), the REAL dispatcher + executor roles, and a REAL
stream subscriber:

* `POST /candybars/{id}/skip` `{fireTime}` stores the occurrence skip, and
  the dispatcher suppresses exactly that fire (no command, no `invocations`
  row) while the `/schedule` row reads `skipped: true`;
* `POST /candybars/{id}/skip-rest-of-day` `{date}` suppresses the whole UTC
  date, and the next day runs normally;
* each write's own `skipped` fact reaches the stream IMMEDIATELY as
  `skip.applied` — before the fire's tick — and the 404s (unknown id, a
  fireTime the schedule never produces, a fireless date) store and publish
  nothing.

THROWAWAY BUS ONLY (the fault-suite guard `_refuse_live_bus`): the dispatcher
role test resets the production-named streams; the cursor row is restored at
teardown. Env-gated: CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1,
CANDYBAR_TEST_FAULT=1 (throwaway NATS_URL + DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import nats
import pytest
from sqlalchemy import delete, select, update

from candybar.api.app import create_app
from candybar.api.stream import ScheduleStream
from candybar.dispatcher import role as dispatcher_role
from candybar.dispatcher.schedule import fires_between, group_skips, next_fire
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.vocabulary import tick_id_for_minute
from candybar.executor import role as executor_role
from candybar.heartbeat import NatsTickSink
from candybar.registry import (
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    CandybarManifest,
    advance_dispatch_cursor,
    create_registry_engine,
    get_dispatch_cursor,
    insert_candybar,
    list_skips,
    occurrence_skip_id,
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

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1"
    or os.environ.get("CANDYBAR_TEST_PG") != "1"
    or os.environ.get("CANDYBAR_TEST_FAULT") != "1",
    reason="requires CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1, CANDYBAR_TEST_FAULT=1 "
    "(throwaway NATS_URL + DATABASE_URL)",
)

BASE = datetime(2025, 7, 7, 10, 0, tzinfo=UTC)  # a Monday
REGISTERED = datetime(2025, 1, 1, tzinfo=UTC)
# The first fire of BASE's UTC date: midnight (the */5 and hourly schedules
# both fire at 00:00, and REGISTERED is months earlier).
DAY_FIRST_FIRE = datetime(2025, 7, 7, 0, 0, tzinfo=UTC)
# The route's own "now": the wall clock is irrelevant to the write path (the
# 404 rule is about what the schedule produces, not about past/future), so
# the app reads the scenario's base date as "now".
NOW = BASE


def at(minutes: int) -> datetime:
    return BASE + timedelta(minutes=minutes)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def _manifest(name: str, entrypoint: str = "/bin/true") -> CandybarManifest:
    return CandybarManifest.model_validate(
        {"name": name, "schedule": "*/5 * * * *", "entrypoint": entrypoint}
    )


class BusSink:
    """The production envelope and dedupe header, straight onto the bus."""

    def __init__(self, js) -> None:
        self._js = js

    async def publish(self, event) -> None:
        await self._js.publish(
            event.subject,
            event.model_dump_json().encode(),
            headers={"Nats-Msg-Id": str(event.id)},
        )


def test_skip_operations_api_suppresses_and_streams_live(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")
    monkeypatch.setattr(dispatcher_role, "ACK_WAIT_SECONDS", DISPATCHER_ACK_WAIT)
    monkeypatch.setattr(executor_role, "ACK_WAIT_SECONDS", EXECUTOR_ACK_WAIT)

    suffix = uuid4().hex[:8]
    keys = ("skipped", "day_skipped", "neighbour")
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
            await asyncio.sleep(1.5)

        def runs(key: str) -> list[list[str]]:
            # Commands execute concurrently: order the log by fire minute.
            return sorted((line[:2] for line in _lines(logs[key])), key=lambda r: int(r[1][5:]))

        sink = BusSink(js)
        api = create_app(
            session_factory=lambda: session_scope(engine),
            sink=sink,
            now=lambda: NOW,
        )

        async with session_scope(engine) as session:
            original = await get_dispatch_cursor(session, dispatcher_role.DURABLE)
            await session.execute(
                delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
            )
            await advance_dispatch_cursor(session, dispatcher_role.DURABLE, at(4))

        # A stream subscriber (an ephemeral ordered reader like the api role's).
        stream_facts: list = []
        feeder: asyncio.Task[None] | None = None

        async def feed() -> None:
            sub = await js.subscribe(
                "bloodbank.evt.cron.>",
                stream="BLOODBANK_EVENTS",
                ordered_consumer=True,
            )
            try:
                while True:
                    msg = await sub.next_msg(timeout=120)
                    stream_facts.append(msg)
            except (TimeoutError, nats.errors.TimeoutError):
                return

        try:
            async with session_scope(engine) as session:
                for key in keys:
                    script = _make_script(tmp_path, f"{key}.sh", ENV_LOG_SCRIPT.format(log=logs[key]))
                    await insert_candybar(
                        session,
                        _manifest(f"itest-skipapi-{key.replace('_', '-')}-{suffix}", script),
                        candybar_id=ids[key],
                    )
                await session.execute(
                    update(Candybar)
                    .where(Candybar.id.in_(ids.values()))
                    .values(created_at=REGISTERED)
                )

            feeder = asyncio.create_task(feed())
            transport = httpx2.ASGITransport(app=api)
            async with httpx2.AsyncClient(
                transport=transport, base_url="http://candybar"
            ) as client:
                # --- The write surface: 404s and success -------------------
                missing = await client.post(
                    f"/candybars/{uuid4()}/skip", json={"fireTime": tid_iso(at(10))}
                )
                assert missing.status_code == 404
                assert missing.headers["content-type"] == "application/problem+json"

                no_fire = await client.post(
                    f"/candybars/{ids['skipped']}/skip", json={"fireTime": tid_iso(at(11))}
                )
                assert no_fire.status_code == 404  # */5 never fires at :11

                # A fireless day: 404, nothing stored. A weekdays-only
                # schedule never fires on Sunday 2025-07-06.
                async with session_scope(engine) as session:
                    row = await session.get(Candybar, ids["neighbour"])
                    row.schedule = "*/5 * * * 1-5"
                no_day = await client.post(
                    f"/candybars/{ids['neighbour']}/skip-rest-of-day",
                    json={"date": "2025-07-06"},
                )
                assert no_day.status_code == 404
                async with session_scope(engine) as session:
                    row = await session.get(Candybar, ids["neighbour"])
                    row.schedule = "*/5 * * * *"

                # The operator's overrides, through the API:
                # - skip the 10:10 fire of ONE candybar;
                # - skip the whole UTC day for ANOTHER.
                resp = await client.post(
                    f"/candybars/{ids['skipped']}/skip", json={"fireTime": tid_iso(at(10))}
                )
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"skipped": True, "fireTime": tid_iso(at(10))}

                resp = await client.post(
                    f"/candybars/{ids['day_skipped']}/skip-rest-of-day",
                    json={"date": BASE.date().isoformat()},
                )
                assert resp.status_code == 200, resp.text
                assert resp.json() == {"skipped": True, "date": BASE.date().isoformat()}

                # Idempotent repeat: 200, no new row.
                resp = await client.post(
                    f"/candybars/{ids['skipped']}/skip", json={"fireTime": tid_iso(at(10))}
                )
                assert resp.status_code == 200

                # The registry rows are exactly the two new skips.
                async with session_scope(engine) as session:
                    stored = await list_skips(session, at(0), at(24 * 60))
                mine = {(s.candybar_id, s.kind, s.fire_time, s.day) for s in stored}
                assert mine == {
                    (ids["day_skipped"], SKIP_REST_OF_DAY, None, BASE.date()),
                    (ids["skipped"], SKIP_OCCURRENCE, at(10), None),
                }

                # /schedule reflects both BEFORE any tick: the skipped rows
                # read skipped:true, recomputed upcoming times omit them.
                window = await client.get(
                    "/schedule",
                    params={"from": tid_iso(at(5)), "to": tid_iso(at(20))},
                )
                assert window.status_code == 200
                schedule_rows = window.json()
                by_key = {
                    (r["candybarId"], r["fireTime"]): r for r in schedule_rows
                }
                assert by_key[(str(ids["skipped"]), tid_iso(at(10)))]["skipped"] is True
                assert by_key[(str(ids["skipped"]), tid_iso(at(5)))]["skipped"] is False
                for m in (5, 10, 15):
                    assert by_key[(str(ids["day_skipped"]), tid_iso(at(m)))]["skipped"] is True
                    assert by_key[(str(ids["neighbour"]), tid_iso(at(m)))]["skipped"] is False

            # --- The stream saw both writes immediately -------------------
            # Exactly 2 skipped facts on the bus so far (the routes'; the
            # dispatcher has not started), and both ARE the routes'.
            await _until(lambda: len(stream_facts) >= 2, "stream saw the route facts")
            assert len(stream_facts) == 2
            route_facts = []
            for msg in stream_facts:
                fact = parse_envelope(json.loads(msg.data))
                assert fact.type == "bloodbank.cron.candybar.skipped"
                route_facts.append(fact)
            by_id = {f.data.candybar_id: f for f in route_facts}
            occ = by_id[ids["skipped"]]
            assert occ.data.reason == "occurrence" and occ.data.tick_id == tid(at(10))
            assert occ.id == occurrence_skip_id(ids["skipped"], at(10))
            day = by_id[ids["day_skipped"]]
            assert day.data.reason == "rest_of_day" and day.data.tick_id == tid(DAY_FIRST_FIRE)
            assert day.id == rest_of_day_skip_id(ids["day_skipped"], BASE.date())

            # The stream hub (the real one, fed the route's fact) patches the
            # row immediately: covered by the skip kind, no registry row read.
            hub = ScheduleStream(session_factory=lambda: session_scope(engine), now=lambda: NOW)
            await hub.start()
            events: list[dict] = []

            async def collect() -> None:
                async for frame in hub.frames():
                    if "event: skip.applied" in frame:
                        events.append(json.loads(frame.split("data: ", 1)[1]))
                        if len(events) == 2:
                            return

            collector = asyncio.ensure_future(collect())
            await asyncio.sleep(0)
            await hub.handle(occ)
            await hub.handle(day)
            await asyncio.wait_for(collector, timeout=10)
            hub.close()
            patched = {e["candybarId"]: e for e in events}
            assert patched[str(ids["skipped"])]["row"]["skipped"] is True
            assert patched[str(ids["skipped"])]["row"]["fireTime"] == tid_iso(at(10))
            assert patched[str(ids["day_skipped"])]["row"]["skipped"] is True
            assert patched[str(ids["day_skipped"])]["row"]["fireTime"] == tid_iso(DAY_FIRST_FIRE)

            # --- The dispatcher honors both skips -------------------------
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

            for minute in (5, 10, 15):
                await ticks.publish_tick(build_tick_fired(at(minute)))
                await _until(lambda m=minute: cursor_at(at(m)), f"tick 10:{minute:02d}")
            await _until(lambda: len(_lines(logs["skipped"])) >= 2, "skipped runs")
            await _until(lambda: len(_lines(logs["neighbour"])) >= 3, "neighbour runs")
            await settle("dispatch settle")

            # The occurrence skip suppressed exactly 10:10.
            assert runs("skipped") == [["scheduled", tid(at(5))], ["scheduled", tid(at(15))]]
            assert runs("neighbour") == [["scheduled", tid(at(m))] for m in (5, 10, 15)]
            # The day skip suppressed every fire so far.
            assert _lines(logs["day_skipped"]) == []
            assert await rows("day_skipped") == []
            assert sorted(r.tick_id for r in await rows("skipped")) == sorted(
                [tid(at(5)), tid(at(15))]
            )
            facts = await _skipped_events(js)
            dispatch_facts = sorted(
                (e.data.candybar_id, e.data.tick_id, e.data.reason)
                for e in facts
                # The route's own facts are among these; assert the full set.
            )
            assert (ids["skipped"], tid(at(10)), "occurrence") in dispatch_facts
            for m in (5, 10, 15):
                assert (ids["day_skipped"], tid(at(m)), "rest_of_day") in dispatch_facts

            # Upcoming-fire computation off the same registry rows agrees.
            async with session_scope(engine) as session:
                overrides = group_skips(await list_skips(session, at(5), at(30)))
            mine = overrides[ids["skipped"]]
            assert next_fire("*/5 * * * *", at(6), skips=mine) == at(15)
            assert list(fires_between("*/5 * * * *", at(5), at(20), skips=mine)) == [at(5), at(15)]
            day_mine = overrides[ids["day_skipped"]]
            # The whole UTC day is covered; the next fire is next midnight's.
            assert next_fire("*/5 * * * *", at(5), skips=day_mine) == DAY_FIRST_FIRE + timedelta(
                days=1
            )
        finally:
            for role_name in list(running):
                await stop_role(role_name)
            if feeder is not None:
                feeder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feeder
            async with session_scope(engine) as session:
                for cid in ids.values():
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)
                await session.execute(
                    delete(Skip).where(Skip.candybar_id.in_(ids.values()))
                )
                await session.execute(
                    delete(DispatchCursor).where(DispatchCursor.name == dispatcher_role.DURABLE)
                )
                if original is not None:
                    await advance_dispatch_cursor(session, dispatcher_role.DURABLE, original)
            await engine.dispose()
            await nc.drain()

    asyncio.run(scenario())


def tid_iso(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat()
