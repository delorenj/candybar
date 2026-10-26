"""Story 5.4: `GET /schedule/stream`, live updates over SSE (AD-11).

The hub (`candybar.api.stream.ScheduleStream`) is fed production fact
envelopes built by the same builders the heartbeat, dispatcher, executor and
api use. Registry reads are monkeypatched with one in-memory world that both
the hub and `GET /schedule` read, so the central property can be checked
directly: applying the stream's events to a `/schedule` snapshot, with the
documented client patch rule, yields exactly what a refetch returns.
Contract conformance: the trigger list is parsed out of
holocene-view-contract.md's "Live updates" section, and every row, current
row and definition any event carries is checked against the contract shapes.
The live NATS + Postgres round trip is tests/test_schedule_stream_integration.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx2
import pytest

import candybar.api.app as app_module
import candybar.api.stream as stream_module
from candybar.api.app import create_app
from candybar.api.schedule import fire_key
from candybar.api.stream import (
    EVENT_CURRENT_ADVANCED,
    EVENT_DEFINITION_CHANGED,
    EVENT_INVOCATION_COMPLETED,
    EVENT_INVOCATION_STARTED,
    EVENT_READY,
    EVENT_RESET,
    EVENT_SKIP_APPLIED,
    STREAM_TRIGGERS,
    ScheduleStream,
    row_key,
    sse_frame,
)
from candybar.events import (
    build_candybar_completed,
    build_candybar_created,
    build_candybar_failed,
    build_candybar_invoked,
    build_candybar_skipped,
    build_candybar_updated,
    build_tick_fired,
)
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry import SKIP_OCCURRENCE, SKIP_REST_OF_DAY, SkipEntry
from contract_shapes import (
    LIST_KEYS,
    assert_contract_shape,
    assert_schedule_row,
    live_update_patch_clause,
    live_update_triggers,
)
from test_api_schedule import skips_in

DAY = datetime(2026, 10, 26, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, second, tzinfo=UTC)


def tid(fire: datetime) -> str:
    return tick_id_for_minute(int(fire.timestamp()) // 60)


# ---------------------------------------------------------------------------
# One in-memory registry, read by both the hub and GET /schedule.


@dataclass
class Def:
    id: UUID
    name: str
    schedule: str
    noise: bool = False
    created_at: datetime = DAY - timedelta(days=30)
    description: str = ""
    tags: list[str] = field(default_factory=list)
    timezone: str = "UTC"


@dataclass
class World:
    defs: dict[UUID, Def] = field(default_factory=dict)
    skips: list[SkipEntry] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    now: datetime = datetime(2026, 10, 26, 10, 12, 30, tzinfo=UTC)
    db_down: bool = False
    definition_reads: int = 0

    def add(self, name: str, schedule: str, **kw: Any) -> Def:
        d = Def(id=kw.pop("id", uuid4()), name=name, schedule=schedule, **kw)
        self.defs[d.id] = d
        return d

    def skip(self, d: Def, fire: datetime) -> None:
        self.skips.append(SkipEntry(uuid4(), d.id, SKIP_OCCURRENCE, fire, None, None, DAY))

    def skip_day(self, d: Def, day: date) -> None:
        self.skips.append(SkipEntry(uuid4(), d.id, SKIP_REST_OF_DAY, None, day, None, DAY))

    def ran(self, d: Def, fire: datetime, result: str) -> None:
        self.results[fire_key(d.id, fire)] = result

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield object()

    def hub(self, **kw: Any) -> ScheduleStream:
        return ScheduleStream(session_factory=self.session, now=lambda: self.now, **kw)


async def arefetch(world: World, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """GET /schedule over the same world: the real route, a fresh request."""
    app = create_app(session_factory=world.session, sink=None, now=lambda: world.now)
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.get(
            "/schedule", params={"from": start.isoformat(), "to": end.isoformat()}
        )
    assert response.status_code == 200, response.text
    rows = response.json()
    for row in rows:
        assert_schedule_row(row)
    return rows


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    w = World()

    async def list_candybars(session: Any) -> list[Def]:
        if w.db_down:
            raise OSError("connection refused")
        return sorted(w.defs.values(), key=lambda d: (d.created_at, d.id))

    async def get_candybar(session: Any, candybar_id: UUID) -> Def | None:
        if w.db_down:
            raise OSError("connection refused")
        w.definition_reads += 1
        return w.defs.get(candybar_id)

    async def list_skips(session: Any, start: datetime, end: datetime | None = None) -> list[SkipEntry]:
        if w.db_down:
            raise OSError("connection refused")
        return skips_in(w.skips, start, end or datetime(9999, 1, 1, tzinfo=UTC))

    async def fire_results(session: Any, keys: list[str]) -> dict[str, str]:
        if w.db_down:
            raise OSError("connection refused")
        return {k: w.results[k] for k in keys if k in w.results}

    for module in (app_module, stream_module):
        monkeypatch.setattr(module, "list_candybars", list_candybars)
        monkeypatch.setattr(module, "list_skips", list_skips)
        monkeypatch.setattr(module, "fire_results", fire_results)
        if hasattr(module, "get_candybar"):
            monkeypatch.setattr(module, "get_candybar", get_candybar)
    return w


# ---------------------------------------------------------------------------
# An SSE client over the hub's frames, and the documented client patch rule.


def parse_sse(text: str) -> list[dict[str, Any]]:
    """SSE frames as {id, event, data}; comments and retry-only frames are skipped."""
    out: list[dict[str, Any]] = []
    for block in text.split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if not line or line.startswith(":"):
                continue
            name, _, value = line.partition(": ")
            fields[name] = value
        if "id" not in fields and "data" not in fields:
            continue
        out.append(
            {
                "id": fields.get("id"),
                "event": fields.get("event"),
                "data": json.loads(fields["data"]) if "data" in fields else None,
            }
        )
    return out


class Client:
    """One SSE connection: a task drains the hub's frames like a server would.

    `close()` cancels that task, which is what starlette does to the frame
    generator when the client disconnects.
    """

    def __init__(self, hub: ScheduleStream, *, last_event_id: str | None = None, window=None) -> None:
        self.gen = hub.frames(last_event_id=last_event_id, window=window)
        self.raw: list[str] = []
        self.ended = False
        self._unread: list[str] = []
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        async for frame in self.gen:
            self.raw.append(frame)
            self._unread.append(frame)
        self.ended = True

    async def read(self) -> list[dict[str, Any]]:
        """Every frame that arrives until the stream goes quiet, parsed
        (id-only frames included, with event None)."""
        quiet = 0
        while quiet < 5 and not self._task.done():
            seen = len(self.raw)
            await asyncio.sleep(0.01)
            quiet = quiet + 1 if len(self.raw) == seen else 0
        text, self._unread = "".join(self._unread), []
        return parse_sse(text)

    async def close(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


def dispatched(frames: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The frames an EventSource dispatches (an id-only frame only moves its cursor)."""
    return [f for f in frames if f["event"] is not None]


def view_key(row: dict[str, Any]) -> str:
    return f"{row['candybarId']}@{row['fireTime']}"


class View:
    """A Holocene-like consumer: a /schedule snapshot patched by stream events."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {view_key(r): dict(r) for r in rows}
        self.applied: dict[str, int] = {}

    def apply(self, event: dict[str, Any]) -> None:
        kind, data = event["event"], event["data"]
        if kind in (None, EVENT_READY, EVENT_RESET):
            return
        key, seq = data["key"], data["seq"]
        if self.applied.get(key, 0) >= seq:
            return  # idempotent patch keys: an event never applies twice
        self.applied[key] = seq
        if kind == EVENT_CURRENT_ADVANCED:
            current = view_key(data["current"]) if data["current"] else None
            for k, row in self.rows.items():
                if k == current:
                    row["state"] = "current"
                elif row["fireTime"] < data["cut"]:
                    row["state"] = "past"
                else:
                    row["state"] = "upcoming"
        elif kind in (EVENT_INVOCATION_STARTED, EVENT_INVOCATION_COMPLETED, EVENT_SKIP_APPLIED):
            if data["row"] is not None:
                self.rows[key] = dict(data["row"])
        elif kind == EVENT_DEFINITION_CHANGED:
            cid = data["candybarId"]
            if "rows" in data:
                self.rows = {k: r for k, r in self.rows.items() if r["candybarId"] != cid}
                self.rows.update({view_key(r): dict(r) for r in data["rows"]})
            else:
                for row in self.rows.values():
                    if row["candybarId"] == cid:
                        for name in ("name", "schedule", "noise"):
                            row[name] = data["definition"][name]

    def snapshot(self) -> list[dict[str, Any]]:
        return sorted(self.rows.values(), key=lambda r: (r["fireTime"], r["name"]))


def assert_event_contract(event: dict[str, Any]) -> None:
    """Every row-shaped thing an event carries is the contract's shape."""
    data = event["data"]
    if data is None:
        return
    assert data["type"] == event["event"]
    for name in ("row", "current"):
        if data.get(name) is not None:
            assert_schedule_row(data[name])
    for row in data.get("rows", []):
        assert_schedule_row(row)
    if "definition" in data:
        assert_contract_shape(data["definition"], LIST_KEYS)


# Builders for the production facts, with an explicit fact time.


def tick(minute: datetime):
    return build_tick_fired(minute=minute)


def invoked(d: Def, fire: datetime | None, *, trigger: str = "scheduled", invocation: UUID | None = None):
    return build_candybar_invoked(
        candybar_id=d.id, name=d.name, invocation_id=invocation or uuid4(), trigger=trigger,
        tick_id=tid(fire) if fire else None,
    )


def completed(d: Def, fire: datetime | None, *, trigger: str = "scheduled"):
    return build_candybar_completed(
        candybar_id=d.id, name=d.name, invocation_id=uuid4(), trigger=trigger,
        tick_id=tid(fire) if fire else None, exit_code=0, runtime_ms=12,
    )


def failed(d: Def, fire: datetime):
    return build_candybar_failed(
        candybar_id=d.id, name=d.name, invocation_id=uuid4(), trigger="scheduled",
        tick_id=tid(fire), exit_code=1, runtime_ms=12, stderr="boom",
    )


def skipped(d: Def, fire: datetime, reason: str):
    return build_candybar_skipped(candybar_id=d.id, name=d.name, tick_id=tid(fire), reason=reason)


def updated(d: Def, **changes: Any):
    return build_candybar_updated(candybar_id=d.id, name=changes.get("name", d.name), changes=changes)


def created(d: Def):
    return build_candybar_created(
        candybar_id=d.id, name=d.name, description=d.description, tags=list(d.tags),
        schedule=d.schedule, timezone=d.timezone, noise=d.noise,
    )


WINDOW = (at(10), at(10, 30))


# ---------------------------------------------------------------------------
# Contract conformance.


def test_contract_triggers_are_exactly_the_stream_events() -> None:
    # The contract's own list, in its order, maps one to one onto event names.
    assert live_update_triggers() == list(STREAM_TRIGGERS)
    assert len(set(STREAM_TRIGGERS.values())) == 5
    assert "patch in place without a refetch" in live_update_patch_clause()


def test_patched_snapshot_equals_refetch_through_every_trigger(world: World) -> None:
    """The AC end to end: all five triggers, each event checked against the
    contract, and after every step the patched view IS the refetch."""
    alpha = world.add("alpha", "*/5 * * * *", noise=True)
    bravo = world.add("bravo", "*/15 * * * *")
    world.skip(alpha, at(10, 25))  # an operator skip the dispatcher honors at 10:25
    seen: set[str] = set()

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        view = View(await arefetch(world, *WINDOW))
        client = Client(hub, window=WINDOW)
        opening = await client.read()
        assert [f["event"] for f in opening] == [EVENT_READY]
        assert opening[0]["data"]["current"]["fireTime"] == at(10, 15).isoformat()

        async def step(*facts: Any) -> list[dict[str, Any]]:
            for fact in facts:
                await hub.handle(fact)
            frames = dispatched(await client.read())
            for frame in frames:
                assert_event_contract(frame)
                seen.add(frame["event"])
                view.apply(frame)
            assert view.snapshot() == await arefetch(world, *WINDOW)
            return frames

        # The 10:15 tick: current advances from alpha@10:15 to alpha@10:20.
        world.now = at(10, 15, 2)
        [adv] = await step(tick(at(10, 15)))
        assert adv["event"] == EVENT_CURRENT_ADVANCED
        assert adv["data"]["cut"] == at(10, 16).isoformat()
        assert adv["data"]["current"]["fireTime"] == at(10, 20).isoformat()
        assert adv["data"]["current"]["name"] == "alpha"

        # Both 10:15 fires start, then complete (the executor records the row first).
        frames = await step(invoked(alpha, at(10, 15)), invoked(bravo, at(10, 15)))
        assert [f["data"]["key"] for f in frames] == [
            row_key(alpha.id, at(10, 15)), row_key(bravo.id, at(10, 15)),
        ]
        world.ran(alpha, at(10, 15), "success")
        world.ran(bravo, at(10, 15), "fail")
        frames = await step(completed(alpha, at(10, 15)), failed(bravo, at(10, 15)))
        assert [(f["data"]["result"], f["data"]["row"]["lastResult"]) for f in frames] == [
            ("success", "success"), ("fail", "fail"),
        ]

        # 10:20: alpha fires; 10:25: alpha's stored skip is applied, current is alpha@10:30.
        world.now = at(10, 20, 1)
        await step(tick(at(10, 20)), invoked(alpha, at(10, 20)))
        world.ran(alpha, at(10, 20), "success")
        await step(completed(alpha, at(10, 20)))
        world.now = at(10, 25, 1)
        [skip] = await step(tick(at(10, 25)), skipped(alpha, at(10, 25), SKIP_OCCURRENCE))
        assert skip["event"] == EVENT_SKIP_APPLIED  # current did not change: no advance
        assert skip["data"]["row"]["skipped"] is True and skip["data"]["row"]["state"] == "past"
        # A coalesced misfire is a past fire that did not run: skipped stays false.
        [coalesced] = await step(skipped(bravo, at(10), "misfire_coalesced"))
        assert coalesced["data"]["row"]["skipped"] is False

        # A definition change (schedule */10): the window's rows for alpha are replaced.
        alpha.schedule = "*/10 * * * *"
        frames = await step(updated(alpha, schedule="*/10 * * * *"))
        [definition] = [f for f in frames if f["event"] == EVENT_DEFINITION_CHANGED]
        assert definition["data"]["change"] == "updated"
        assert definition["data"]["changes"] == {"schedule": "*/10 * * * *"}
        assert [r["fireTime"] for r in definition["data"]["rows"]] == [
            at(10).isoformat(), at(10, 10).isoformat(), at(10, 20).isoformat(),
        ]
        assert definition["data"]["rows"][-1]["lastResult"] == "success"
        # The current row (alpha@10:30) now has the new schedule string.
        assert EVENT_CURRENT_ADVANCED in [f["event"] for f in frames]

        # A new candybar that fires sooner than everything: current moves to it.
        charlie = world.add("charlie", "27 * * * *", created_at=at(10, 25, 30))
        frames = await step(created(charlie))
        assert [f["event"] for f in frames] == [EVENT_CURRENT_ADVANCED, EVENT_DEFINITION_CHANGED]
        assert frames[0]["data"]["current"]["name"] == "charlie"
        assert frames[1]["data"]["definition"]["createdAt"] == at(10, 25, 30).isoformat()
        await client.close()

    asyncio.run(scenario())
    assert set(STREAM_TRIGGERS.values()) <= seen


def test_matrix_tick_advances_current(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        client = Client(hub)
        await client.read()
        world.now = at(10, 15, 1)
        await hub.handle(tick(at(10, 15)))
        [adv] = dispatched(await client.read())
        assert adv["event"] == EVENT_CURRENT_ADVANCED
        assert adv["data"]["key"] == "current"
        assert adv["data"]["cut"] == at(10, 16).isoformat()
        assert adv["data"]["current"]["candybarId"] == str(alpha.id)
        assert adv["data"]["current"]["fireTime"] == at(10, 20).isoformat()
        assert adv["data"]["current"]["state"] == "current"
        assert "lastResult" not in adv["data"]["current"]

    asyncio.run(scenario())


def test_matrix_duplicate_and_old_ticks_emit_nothing(world: World, monkeypatch) -> None:
    world.add("alpha", "*/5 * * * *")
    reads: list[int] = []
    real = stream_module.list_candybars

    async def counting(session: Any) -> Any:
        reads.append(1)
        return await real(session)

    monkeypatch.setattr(stream_module, "list_candybars", counting)

    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub)
        await client.read()
        world.now = at(10, 15, 1)
        await hub.handle(tick(at(10, 15)))
        first = await client.read()
        assert [f["event"] for f in dispatched(first)] == [EVENT_CURRENT_ADVANCED]
        assert ": keepalive" in client.raw[-2]  # each new tick keeps idle connections open
        await hub.handle(tick(at(10, 15)))  # the same tick_id again (a redelivery)
        await hub.handle(tick(at(10, 14)))  # an older tick (a late replay)
        assert await client.read() == []
        assert len(client.raw) == 3  # opening, keepalive, advance: no second keepalive
        assert len(reads) == 1  # deduped before any registry read

    asyncio.run(scenario())


def test_matrix_scheduled_run_patches_one_row_key(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        client = Client(hub)
        await client.read()
        world.now = at(10, 15, 3)
        await hub.handle(tick(at(10, 15)))
        invocation = uuid4()
        await hub.handle(invoked(alpha, at(10, 15), invocation=invocation))
        world.ran(alpha, at(10, 15), "success")
        await hub.handle(completed(alpha, at(10, 15)))
        frames = dispatched(await client.read())[1:]
        assert [f["event"] for f in frames] == [EVENT_INVOCATION_STARTED, EVENT_INVOCATION_COMPLETED]
        assert {f["data"]["key"] for f in frames} == {row_key(alpha.id, at(10, 15))}
        started, done = (f["data"] for f in frames)
        assert started["invocationId"] == str(invocation) and started["trigger"] == "scheduled"
        assert "lastResult" not in started["row"] and started["row"]["state"] == "past"
        assert done["row"]["lastResult"] == "success" and done["result"] == "success"
        assert started["seq"] < done["seq"]

    asyncio.run(scenario())


def test_matrix_manual_run_has_no_row(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub)
        await client.read()
        invocation = uuid4()
        await hub.handle(invoked(alpha, None, trigger="manual", invocation=invocation))
        await hub.handle(completed(alpha, None, trigger="manual"))
        started, done = dispatched(await client.read())
        assert started["event"] == EVENT_INVOCATION_STARTED
        assert started["data"]["row"] is None
        assert started["data"]["key"] == f"invocation:{invocation}"
        assert started["data"]["trigger"] == "manual"
        assert done["data"]["row"] is None and done["data"]["result"] == "success"

    asyncio.run(scenario())


def test_matrix_operator_skip_and_coalesced_misfire_rows(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")
    world.skip(alpha, at(10, 10))
    world.skip_day(alpha, date(2026, 10, 25))

    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub)
        await client.read()
        await hub.handle(skipped(alpha, at(10, 10), SKIP_OCCURRENCE))
        await hub.handle(skipped(alpha, at(10, 5), "misfire_coalesced"))
        await hub.handle(skipped(alpha, DAY - timedelta(minutes=5), SKIP_REST_OF_DAY))
        rows = [f["data"]["row"] for f in dispatched(await client.read())]
        assert [(r["fireTime"], r["skipped"], r["state"]) for r in rows] == [
            (at(10, 10).isoformat(), True, "past"),
            (at(10, 5).isoformat(), False, "past"),
            ((DAY - timedelta(minutes=5)).isoformat(), True, "past"),
        ]
        assert all("lastResult" not in r for r in rows)

    asyncio.run(scenario())


def test_matrix_schedule_edit_carries_window_rows_and_moves_current(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")
    bravo = world.add("bravo", "*/7 * * * *")  # 10:14 is bravo's next fire

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        assert hub._current_json["name"] == "bravo"
        windowed, bare = Client(hub, window=WINDOW), Client(hub)
        await windowed.read()
        await bare.read()
        # The fact is published BEFORE the row commits (event-first): the hub
        # must not compute from the stale registry row.
        await hub.handle(updated(bravo, schedule="*/20 * * * *"))
        frames = dispatched(await windowed.read())
        assert [f["event"] for f in frames] == [EVENT_CURRENT_ADVANCED, EVENT_DEFINITION_CHANGED]
        assert frames[0]["data"]["current"]["name"] == "alpha"  # alpha@10:15 now comes first
        rows = frames[1]["data"]["rows"]
        assert [r["fireTime"] for r in rows] == [at(10).isoformat(), at(10, 20).isoformat()]
        assert all(r["schedule"] == "*/20 * * * *" for r in rows)
        assert frames[1]["data"]["definition"]["schedule"] == "*/20 * * * *"
        # Without a window the definition still arrives, with no rows to replace.
        bare_frames = dispatched(await bare.read())
        assert "rows" not in bare_frames[1]["data"]
        # Once the row commits, the windowed view equals the refetch.
        bravo.schedule = "*/20 * * * *"
        refetch = await arefetch(world, *WINDOW)
        assert [r for r in refetch if r["name"] == "bravo"] == rows
        assert alpha.id != bravo.id

    asyncio.run(scenario())


def test_rename_and_noise_patch_without_a_window_equal_refetch(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")
    world.add("bravo", "*/15 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        view = View(await arefetch(world, *WINDOW))
        client = Client(hub)
        await client.read()
        alpha.name, alpha.noise = "alpha-two", True
        await hub.handle(updated(alpha, name="alpha-two", noise=True, entrypoint="/x/run.sh"))
        frames = dispatched(await client.read())
        [definition] = [f for f in frames if f["event"] == EVENT_DEFINITION_CHANGED]
        # The entrypoint is executor-internal: never echoed on the stream.
        assert definition["data"]["changes"] == {"name": "alpha-two", "noise": True}
        for frame in frames:
            assert_event_contract(frame)
            view.apply(frame)
        assert view.snapshot() == await arefetch(world, *WINDOW)

    asyncio.run(scenario())


def test_created_before_commit_builds_the_definition_from_the_fact(world: World) -> None:
    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub, window=WINDOW)
        await client.read()
        ghost = Def(id=uuid4(), name="delta", schedule="*/30 * * * *", tags=["ops"])
        fact = created(ghost).model_copy(update={"time": at(9, 59, 30)})
        await hub.handle(fact)  # the registry row has not committed yet
        frames = dispatched(await client.read())
        [definition] = [f for f in frames if f["event"] == EVENT_DEFINITION_CHANGED]
        assert definition["data"]["definition"] == {
            "id": str(ghost.id), "name": "delta", "description": "", "tags": ["ops"],
            "schedule": "*/30 * * * *", "timezone": "UTC", "noise": False, "createdAt": at(9, 59, 30).isoformat(),
        }
        assert [r["fireTime"] for r in definition["data"]["rows"]] == [
            at(10).isoformat(),
        ]
        assert frames[0]["data"]["current"]["name"] == "delta"  # the only candybar: delta@10:30

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Window scoping.


def test_row_events_outside_the_window_only_move_the_cursor(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub, window=(at(10, 10), at(10, 30)))
        await client.read()
        await hub.handle(invoked(alpha, at(10, 5)))  # outside
        await hub.handle(invoked(alpha, at(10, 10)))  # inside
        frames = await client.read()
        assert [f["event"] for f in frames] == [None, EVENT_INVOCATION_STARTED]
        assert frames[0]["data"] is None and frames[0]["id"] == f"{hub.boot}-1"
        assert frames[1]["id"] == f"{hub.boot}-2"

    asyncio.run(scenario())


def test_a_run_the_current_schedule_never_fires_has_no_row(world: World) -> None:
    alpha = world.add("alpha", "*/10 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub)
        await client.read()
        # Dispatched at 10:05 under an older */5 schedule: a refetch has no such row.
        await hub.handle(invoked(alpha, at(10, 5)))
        [frame] = dispatched(await client.read())
        assert frame["data"]["row"] is None
        assert frame["data"]["key"].startswith("invocation:")

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Last-Event-ID: resume without duplicates; unresumable ids reset.


def test_matrix_resume_replays_exactly_the_missed_events(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        view = View(await arefetch(world, *WINDOW))
        first = Client(hub, window=WINDOW)
        await first.read()
        world.now = at(10, 15, 1)
        await hub.handle(tick(at(10, 15)))
        await hub.handle(invoked(alpha, at(10, 15)))
        got = dispatched(await first.read())
        for frame in got:
            view.apply(frame)
        last = got[-1]["id"]
        await first.close()  # the connection drops
        assert hub.subscriber_count == 0

        world.ran(alpha, at(10, 15), "success")
        missed = [completed(alpha, at(10, 15)), tick(at(10, 20))]
        world.now = at(10, 20, 1)
        for fact in missed:
            await hub.handle(fact)

        again = Client(hub, last_event_id=last, window=WINDOW)
        resumed = await again.read()
        # Exactly the missed events, in order: no ready, no reset, no duplicate.
        assert [f["event"] for f in resumed] == [EVENT_INVOCATION_COMPLETED, EVENT_CURRENT_ADVANCED]
        assert [f["id"] for f in resumed] == [f"{hub.boot}-3", f"{hub.boot}-4"]
        for frame in resumed:
            view.apply(frame)
        assert view.snapshot() == await arefetch(world, *WINDOW)
        # Re-applying everything (an over-eager client) changes nothing.
        for frame in got + resumed:
            view.apply(frame)
        assert view.snapshot() == await arefetch(world, *WINDOW)
        # Live events keep flowing on the resumed connection.
        await hub.handle(invoked(alpha, at(10, 20)))
        assert [f["id"] for f in await again.read()] == [f"{hub.boot}-5"]

    asyncio.run(scenario())


def test_ready_carries_the_resume_point(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.handle(invoked(alpha, at(10, 5)))
        client = Client(hub)
        [ready] = await client.read()
        assert ready["event"] == EVENT_READY and ready["id"] == f"{hub.boot}-1"
        assert client.raw[0].startswith("retry: ")
        # Not a patch, but the same envelope fields as one.
        assert ready["data"]["key"] == "ready" and ready["data"]["seq"] == 1
        assert ready["data"]["at"] == world.now.isoformat()
        await client.close()
        await hub.handle(invoked(alpha, at(10, 10)))
        again = Client(hub, last_event_id=ready["id"])
        assert [f["id"] for f in await again.read()] == [f"{hub.boot}-2"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "make_id",
    [
        lambda hub: "0123456789ab-1",  # another boot (the api restarted)
        lambda hub: f"{hub.boot}-99",  # a future id
        lambda hub: f"{hub.boot}-1",  # evicted from the ring buffer
        lambda hub: "garbage",
        lambda hub: f"{hub.boot}-x",
        lambda hub: "",
        lambda hub: f"{hub.boot}-\u00b2",  # str.isdigit() is True, int() refuses it
    ],
    ids=["other-boot", "future", "evicted", "garbage", "non-numeric", "empty", "unicode-digit"],
)
def test_matrix_unresumable_ids_reset(world: World, make_id) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub(buffer_size=3)
        for minute in range(5):
            await hub.handle(invoked(alpha, at(10, minute * 5)))  # seqs 1..5; 1 and 2 evicted
        client = Client(hub, last_event_id=make_id(hub))
        [reset] = await client.read()
        assert reset["event"] == EVENT_RESET
        assert reset["data"]["reason"] == "unresumable"
        assert reset["id"] == f"{hub.boot}-5"  # the next reconnect resumes from here
        assert {"type", "seq", "key", "at", "cut", "current"} <= set(reset["data"])
        await hub.handle(invoked(alpha, at(10, 25)))
        assert [f["id"] for f in await client.read()] == [f"{hub.boot}-6"]

    asyncio.run(scenario())


def test_resume_from_the_buffer_floor_replays_the_whole_buffer(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub(buffer_size=3)
        for minute in range(5):
            await hub.handle(invoked(alpha, at(10, minute * 5)))
        client = Client(hub, last_event_id=f"{hub.boot}-2")  # oldest buffered is 3
        assert [f["id"] for f in await client.read()] == [f"{hub.boot}-{n}" for n in (3, 4, 5)]

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Lifecycle: overflow, close, failures.


def test_a_subscriber_that_stops_reading_is_closed_and_can_resume(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub(queue_size=2)
        slow = Client(hub)
        [ready] = await slow.read()
        for minute in range(3):
            await hub.handle(invoked(alpha, at(10, minute * 5)))
        assert hub.subscriber_count == 0
        assert await slow.read() == [] and slow.ended
        again = Client(hub, last_event_id=ready["id"])
        assert [f["id"] for f in await again.read()] == [f"{hub.boot}-{n}" for n in (1, 2, 3)]

    asyncio.run(scenario())


def test_close_ends_every_stream(world: World) -> None:
    world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        clients = [Client(hub), Client(hub, window=WINDOW)]
        for client in clients:
            await client.read()
        assert hub.subscriber_count == 2
        hub.close()
        for client in clients:
            assert await client.read() == [] and client.ended
        late = Client(hub)
        assert await late.read() == [] and late.ended
        await hub.handle(tick(at(10, 15)))  # ignored once closed
        assert hub.last_id == f"{hub.boot}-0"

    asyncio.run(scenario())


def test_matrix_db_down_resets_once_per_failure_streak(world: World) -> None:
    world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()
        client = Client(hub)
        await client.read()
        world.db_down = True
        world.now = at(10, 15, 1)
        await hub.handle(tick(at(10, 15)))
        world.now = at(10, 16, 1)
        await hub.handle(tick(at(10, 16)))
        [reset] = dispatched(await client.read())
        assert reset["event"] == EVENT_RESET and reset["data"]["reason"] == "update_failed"
        assert reset["data"]["key"] == "reset" and reset["data"]["seq"] == 1
        assert reset["id"] == f"{hub.boot}-1"  # buffered: a resume replays it
        world.db_down = False
        world.now = at(10, 17, 1)
        await hub.handle(tick(at(10, 17)))  # recovered: alpha@10:20 is current
        [adv] = dispatched(await client.read())
        assert adv["event"] == EVENT_CURRENT_ADVANCED
        world.db_down = True
        world.now = at(10, 18, 1)
        await hub.handle(tick(at(10, 18)))  # a new streak resets again
        assert [f["event"] for f in dispatched(await client.read())] == [EVENT_RESET]

    asyncio.run(scenario())


def test_start_survives_a_down_registry(world: World) -> None:
    world.db_down = True

    async def scenario() -> None:
        hub = world.hub()
        await hub.start()  # logged, not raised
        [ready] = await Client(hub).read()
        assert ready["data"]["current"] is None

    asyncio.run(scenario())


def test_rows_render_failure_resets_only_that_client(world: World, monkeypatch) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        windowed, bare = Client(hub, window=WINDOW), Client(hub)
        await windowed.read()
        await bare.read()

        async def broken(*args: Any, **kwargs: Any) -> Any:
            raise OSError("connection refused")

        monkeypatch.setattr(stream_module, "fire_results", broken)
        await hub.handle(updated(alpha, noise=True))
        windowed_frames = dispatched(await windowed.read())
        assert windowed_frames[-1]["event"] == EVENT_RESET
        assert windowed_frames[-1]["data"]["reason"] == "render_failed"
        assert dispatched(await bare.read())[-1]["event"] == EVENT_DEFINITION_CHANGED

    asyncio.run(scenario())


def test_non_stream_facts_and_unknown_types_are_ignored(world: World) -> None:
    async def scenario() -> None:
        hub = world.hub()
        client = Client(hub)
        await client.read()
        from candybar.events import build_candybar_invoke

        command = build_candybar_invoke(
            candybar_id=uuid4(), name="alpha", trigger="manual", command_id=uuid4()
        )
        await hub.handle(command)
        assert await client.read() == [] and hub.last_id == f"{hub.boot}-0"

    asyncio.run(scenario())


def test_sse_frame_is_one_line_per_field() -> None:
    frame = sse_frame("b-1", "ready", {"text": "two\nlines"})
    assert frame == 'id: b-1\nevent: ready\ndata: {"text":"two\\nlines"}\n\n'
    assert sse_frame("b-2", None, None) == "id: b-2\n\n"


# ---------------------------------------------------------------------------
# The route.


def test_route_streams_event_stream_frames(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        app = create_app(session_factory=world.session, sink=None, now=lambda: world.now, stream=hub)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            request = asyncio.create_task(
                client.get(
                    "/schedule/stream",
                    params={"from": "2026-10-26T10:00:00Z", "to": "2026-10-26T10:30:00Z"},
                )
            )
            for _ in range(100):
                if hub.subscriber_count:
                    break
                await asyncio.sleep(0.01)
            assert hub.subscriber_count == 1
            await hub.handle(invoked(alpha, at(10, 5)))
            await hub.handle(invoked(alpha, at(9, 55)))  # outside the window
            hub.close()  # ASGITransport buffers: end the stream to read it
            response = await request
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["cache-control"] == "no-cache"
        assert response.headers["x-accel-buffering"] == "no"
        frames = parse_sse(response.text)
        assert [f["event"] for f in frames] == [EVENT_READY, EVENT_INVOCATION_STARTED, None]
        assert frames[1]["data"]["row"]["fireTime"] == at(10, 5).isoformat()

    asyncio.run(scenario())


def test_route_resumes_from_the_last_event_id_header(world: World) -> None:
    alpha = world.add("alpha", "*/5 * * * *")

    async def scenario() -> None:
        hub = world.hub()
        await hub.handle(invoked(alpha, at(10, 5)))
        await hub.handle(invoked(alpha, at(10, 10)))
        app = create_app(session_factory=world.session, sink=None, now=lambda: world.now, stream=hub)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            request = asyncio.create_task(
                client.get("/schedule/stream", headers={"Last-Event-ID": f"{hub.boot}-1"})
            )
            for _ in range(100):
                if hub.subscriber_count:
                    break
                await asyncio.sleep(0.01)
            hub.close()
            response = await request
        assert [f["id"] for f in parse_sse(response.text)] == [f"{hub.boot}-2"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "params",
    [
        {"from": "2026-10-26T10:00:00Z"},
        {"to": "2026-10-26T10:00:00Z"},
        {"from": "2026-10-26T10:30:00Z", "to": "2026-10-26T10:00:00Z"},
        {"from": "2026-10-26T10:00:00", "to": "2026-10-26T10:30:00"},
        {"from": "2026-10-01T00:00:00Z", "to": "2026-11-15T00:00:00Z"},
    ],
    ids=["from-only", "to-only", "reversed", "naive", "over-31-days"],
)
def test_route_refuses_a_bad_window_as_problem_json(world: World, params) -> None:
    async def scenario() -> httpx2.Response:
        hub = world.hub()
        app = create_app(session_factory=world.session, sink=None, now=lambda: world.now, stream=hub)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            return await client.get("/schedule/stream", params=params)

    response = asyncio.run(scenario())
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["status"] == 422


def test_an_app_built_without_a_hub_still_serves_the_stream(world: World) -> None:
    """create_app's default hub (tests, tools): a ready frame and a clean end."""

    async def scenario() -> httpx2.Response:
        app = create_app(session_factory=world.session, sink=None, now=lambda: world.now)
        hub = app.state.stream
        assert isinstance(hub, ScheduleStream)
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as client:
            request = asyncio.create_task(client.get("/schedule/stream"))
            for _ in range(100):
                if hub.subscriber_count:
                    break
                await asyncio.sleep(0.01)
            hub.close()
            return await request

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert [f["event"] for f in parse_sse(response.text)] == [EVENT_READY]
