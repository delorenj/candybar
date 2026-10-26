"""Story 5.4 — `GET /schedule/stream` live, end to end (env-gated: CANDYBAR_TEST_NATS=1
and CANDYBAR_TEST_PG=1, with a throwaway DATABASE_URL).

The real api app is served by a real uvicorn on a loopback port, read by
real HTTP SSE clients. Its hub is fed by the production feed (`subscribe_feed`:
an ordered, ephemeral JetStream consumer) attached to an ISOLATED test stream,
never BLOODBANK_EVENTS. The real register and PATCH routes publish their
created/updated facts onto that stream; ticks and invocation/skip facts are the
production envelopes from the same builders the heartbeat, dispatcher and
executor use, published the same way (Nats-Msg-Id = event id). Postgres holds
the real registry rows, invocations and skips.

Proven here: the five contract triggers arrive over HTTP; applying them to a
`/schedule` snapshot equals a refetch on real SQL; a client that drops and
reconnects with `Last-Event-ID` receives exactly the events it missed (the
same ids a connected client saw, once each); and `run_api` with an open
stream stops well inside docker's 10s grace.

THROWAWAY DATABASE ONLY: `migrated_db` downgrades the schema to base afterwards.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
import nats
import pytest
import uvicorn
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError
from prometheus_client import CollectorRegistry
from sqlalchemy import update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from candybar.api.app import create_app
from candybar.api.role import GRACEFUL_SHUTDOWN_SECONDS, run_api, subscribe_feed
from candybar.api.schedule import fire_key
from candybar.api.stream import (
    EVENT_CURRENT_ADVANCED,
    EVENT_DEFINITION_CHANGED,
    EVENT_READY,
    STREAM_TRIGGERS,
    ScheduleStream,
)
from candybar.events import (
    build_candybar_completed,
    build_candybar_failed,
    build_candybar_invoked,
    build_candybar_skipped,
    build_tick_fired,
)
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry import (
    SKIP_OCCURRENCE,
    TerminalInvocation,
    add_occurrence_skip,
    record_terminal_invocation,
    session_scope,
    uuid7,
)
from candybar.registry.models import Candybar
from contract_shapes import LIST_KEYS, assert_contract_shape, assert_schedule_row
from test_registry_integration import migrated_db  # noqa: F401  (fixture)
from test_schedule_stream import View, parse_sse

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)

NATS_URL = os.environ.get("NATS_URL", "nats://localhost:4222")
STOP_BUDGET_SECONDS = 8.0  # docker's 10s grace, with margin


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 7, 7, hour, minute, second, tzinfo=UTC)


def tid(fire: datetime) -> str:
    return tick_id_for_minute(int(fire.timestamp()) // 60)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveClient:
    """A real HTTP SSE connection, drained by a task (cancel = the client drops)."""

    def __init__(self, base: str, *, params: dict[str, str] | None = None, last_event_id=None) -> None:
        self.base, self.params, self.last_event_id = base, params, last_event_id
        self.text = ""
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        headers = {"Last-Event-ID": self.last_event_id} if self.last_event_id else {}
        async with (
            httpx2.AsyncClient(base_url=self.base, timeout=None) as http,
            http.stream("GET", "/schedule/stream", params=self.params, headers=headers) as response,
        ):
            self.status, self.headers = response.status_code, dict(response.headers)
            async for chunk in response.aiter_text():
                self.text += chunk

    def frames(self) -> list[dict[str, Any]]:
        return [f for f in parse_sse(self.text) if f["id"] is not None]

    def events(self) -> list[dict[str, Any]]:
        return [f for f in self.frames() if f["event"] is not None]

    async def wait_for(self, count: int, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Until `count` dispatched events (opening included) have arrived."""
        deadline = time.monotonic() + timeout
        while len(self.events()) < count:
            assert time.monotonic() < deadline, f"stream stalled at {self.events()}"
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.15)  # anything extra would show up now
        return self.events()

    async def until(self, done: Any, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while not (self.events() and done(self.events())):
            assert time.monotonic() < deadline, f"stream stalled at {self.events()}"
            await asyncio.sleep(0.02)
        return self.events()

    async def drop(self) -> None:
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task

    @property
    def ended(self) -> bool:
        return self._task.done()


def _entrypoint(tmp_path: Path) -> str:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def _ran(candybar_id: UUID, fire: datetime, result: str) -> TerminalInvocation:
    return TerminalInvocation(
        invocation_id=uuid7(), candybar_id=candybar_id, trigger="scheduled",
        tick_id=tid(fire), idempotency_key=fire_key(candybar_id, fire),
        result=result, exit_code=0 if result == "success" else 1, runtime_ms=5,
        error_message=None, stderr_tail=None, token_spend=0, cost_usd=None,
        started_at=fire + timedelta(seconds=1), finished_at=fire + timedelta(seconds=2),
    )


def test_live_stream_patches_equal_refetch_and_resume_exactly(migrated_db: str, tmp_path: Path) -> None:  # noqa: F811
    window = {"from": at(10).isoformat(), "to": at(10, 30).isoformat()}

    async def scenario() -> None:
        nc = await nats.connect(NATS_URL)
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        stream_name = f"CANDYBAR_TEST_SSE_{suffix.upper()}"
        subject = f"candybar.test.{suffix}.facts"
        await js.add_stream(
            StreamConfig(name=stream_name, subjects=[subject], retention=RetentionPolicy.LIMITS)
        )

        class RerouteSink:
            """The production envelope and dedupe header, on the isolated stream."""

            async def publish(self, event: Any) -> None:
                await js.publish(
                    subject, event.model_dump_json().encode(), headers={"Nats-Msg-Id": str(event.id)}
                )

        sink = RerouteSink()
        # The api session runs in a non-UTC zone, as on the live server.
        engine = create_async_engine(
            migrated_db, poolclass=NullPool, connect_args={"options": "-c timezone=America/New_York"}
        )
        clock = [at(10, 12, 30)]
        hub = ScheduleStream(session_factory=lambda: session_scope(engine), now=lambda: clock[0])
        app = create_app(
            session_factory=lambda: session_scope(engine),
            sink=sink,
            now=lambda: clock[0],
            stream=hub,
            metrics_registry=CollectorRegistry(),
        )
        feed = await subscribe_feed(nc, hub, stream=stream_name, filter_subject=subject)
        port = free_port()
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", timeout_graceful_shutdown=3)
        )
        serve = asyncio.create_task(server.serve())
        base = f"http://127.0.0.1:{port}"
        try:
            while not server.started:
                await asyncio.sleep(0.02)
            watcher = LiveClient(base)  # no window: sees every event, start to finish
            [ready] = await watcher.wait_for(1)
            assert ready["event"] == EVENT_READY
            assert watcher.status == 200
            assert watcher.headers["content-type"].startswith("text/event-stream")

            # Register through the real API: created facts cross the bus to the hub.
            async with httpx2.AsyncClient(base_url=base) as http:
                ids: dict[str, UUID] = {}
                for name, schedule, noise in (("alpha", "*/5 * * * *", True), ("bravo", "*/15 * * * *", False)):
                    response = await http.post(
                        "/candybars",
                        json={"name": name, "schedule": schedule, "entrypoint": _entrypoint(tmp_path), "noise": noise},
                    )
                    assert response.status_code == 201, response.text
                    ids[name] = UUID(response.json()["id"])
            await watcher.until(
                lambda evs: sum(e["event"] == EVENT_DEFINITION_CHANGED for e in evs) == 2
            )
            definitions = [e for e in watcher.events() if e["event"] == EVENT_DEFINITION_CHANGED]
            assert [d["data"]["definition"]["name"] for d in definitions] == ["alpha", "bravo"]
            for d in definitions:
                assert d["data"]["change"] == "created"
                assert_contract_shape(d["data"]["definition"], LIST_KEYS)

            # Test setup outside the stream's scope: backdate the registrations
            # before the fixed clock, and store alpha's 10:25 skip (5.6 owns a
            # skip write API and its stream event). Then the view's snapshot.
            async with session_scope(engine) as session:
                await session.execute(update(Candybar).values(created_at=at(0) - timedelta(days=30)))
            async with session_scope(engine) as session:
                await add_occurrence_skip(session, ids["alpha"], at(10, 25))

            async def refetch() -> list[dict[str, Any]]:
                async with httpx2.AsyncClient(base_url=base) as http:
                    response = await http.get("/schedule", params=window)
                assert response.status_code == 200, response.text
                return response.json()

            view = View(await refetch())
            client = LiveClient(base, params=window)
            await client.wait_for(1)
            applied = 1
            seen: set[str] = set()

            async def step(*facts: Any, expect: int) -> list[dict[str, Any]]:
                nonlocal applied
                for fact in facts:
                    await sink.publish(fact)
                events = await client.wait_for(applied + expect)
                new = events[applied:]
                assert len(new) == expect, new
                applied = len(events)
                for frame in new:
                    data = frame["data"]
                    for name in ("row", "current"):
                        if data.get(name) is not None:
                            assert_schedule_row(data[name])
                    for row in data.get("rows", []):
                        assert_schedule_row(row)
                    seen.add(frame["event"])
                    view.apply(frame)
                assert view.snapshot() == await refetch()
                return new

            def invoked(name: str, fire: datetime):
                return build_candybar_invoked(
                    candybar_id=ids[name], name=name, invocation_id=uuid4(), trigger="scheduled",
                    tick_id=tid(fire),
                )

            clock[0] = at(10, 15, 2)
            [advanced] = await step(build_tick_fired(minute=at(10, 15)), expect=1)
            assert advanced["event"] == EVENT_CURRENT_ADVANCED
            assert advanced["data"]["current"]["fireTime"] == at(10, 20).isoformat()
            await step(invoked("alpha", at(10, 15)), invoked("bravo", at(10, 15)), expect=2)

            # The connection drops here; four facts flow while it is away.
            resume_from = client.frames()[-1]["id"]
            dropped_at = int(resume_from.rpartition("-")[2])
            await client.drop()
            async with session_scope(engine) as session:
                await record_terminal_invocation(session, _ran(ids["alpha"], at(10, 15), "success"))
            async with session_scope(engine) as session:
                await record_terminal_invocation(session, _ran(ids["bravo"], at(10, 15), "fail"))
            await sink.publish(
                build_candybar_completed(
                    candybar_id=ids["alpha"], name="alpha", invocation_id=uuid4(), trigger="scheduled",
                    tick_id=tid(at(10, 15)), exit_code=0, runtime_ms=5,
                )
            )
            await sink.publish(
                build_candybar_failed(
                    candybar_id=ids["bravo"], name="bravo", invocation_id=uuid4(), trigger="scheduled",
                    tick_id=tid(at(10, 15)), exit_code=1, runtime_ms=5, stderr="boom",
                )
            )
            clock[0] = at(10, 25, 1)
            await sink.publish(build_tick_fired(minute=at(10, 25)))  # alpha@10:30 is next
            await sink.publish(
                build_candybar_skipped(
                    candybar_id=ids["alpha"], name="alpha", tick_id=tid(at(10, 25)), reason=SKIP_OCCURRENCE
                )
            )
            await watcher.until(lambda evs: evs[-1]["data"]["seq"] == dropped_at + 4)

            # Reconnect with Last-Event-ID: exactly what the connected watcher
            # saw after the drop, the same ids, each once, no ready or reset.
            client = LiveClient(base, params=window, last_event_id=resume_from)
            applied = 0
            resumed = await step(expect=4)
            expected = [e["id"] for e in watcher.events() if e["data"]["seq"] > dropped_at]
            assert [f["id"] for f in resumed] == expected
            assert [f["event"] for f in resumed] == [
                "invocation.completed", "invocation.completed", "current.advanced", "skip.applied",
            ]
            assert resumed[-1]["data"]["row"]["skipped"] is True

            # A definition change through the real PATCH route: alpha's rows replaced.
            async with httpx2.AsyncClient(base_url=base) as http:
                response = await http.patch(f"/candybars/{ids['alpha']}", json={"schedule": "*/10 * * * *"})
                assert response.status_code == 200, response.text
            changed = await step(expect=2)
            assert [f["event"] for f in changed] == [EVENT_CURRENT_ADVANCED, EVENT_DEFINITION_CHANGED]
            assert [r["fireTime"] for r in changed[1]["data"]["rows"]] == [
                at(10).isoformat(), at(10, 10).isoformat(), at(10, 20).isoformat(),
            ]
            assert set(STREAM_TRIGGERS.values()) <= seen
            # The watcher never dropped: its seqs are contiguous, nothing skipped or repeated.
            await watcher.until(lambda evs: evs[-1]["data"]["seq"] == hub._seq)
            seqs = [e["data"]["seq"] for e in watcher.events()[1:]]
            assert seqs == list(range(1, len(seqs) + 1))
        finally:
            hub.close()
            server.should_exit = True
            await serve
            await feed.unsubscribe()
            with contextlib.suppress(NotFoundError):
                await js.delete_stream(stream_name)
            await engine.dispose()
            await nc.close()

    asyncio.run(scenario())


async def _ensure_event_stream() -> None:
    """A bare CI nats-server has no BLOODBANK_EVENTS; on the live bus it exists and is left alone."""
    nc = await nats.connect(NATS_URL)
    try:
        js = nc.jetstream()
        try:
            await js.stream_info("BLOODBANK_EVENTS")
        except NotFoundError:
            await js.add_stream(
                StreamConfig(name="BLOODBANK_EVENTS", subjects=["bloodbank.evt.>"], retention=RetentionPolicy.LIMITS)
            )
    finally:
        await nc.close()


def test_run_api_with_an_open_stream_stops_inside_the_grace(migrated_db: str, monkeypatch) -> None:  # noqa: F811
    """The production role, with its production feed attached (read-only: an
    ephemeral ordered consumer) and an SSE client connected, stops fast."""
    port = free_port()
    monkeypatch.setenv("CANDYBAR_API_PORT", str(port))
    monkeypatch.setenv("NATS_URL", NATS_URL)
    monkeypatch.setenv("DATABASE_URL", migrated_db)

    async def scenario() -> float:
        await _ensure_event_stream()
        stop = asyncio.Event()
        role = asyncio.create_task(run_api(stop))
        base = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while True:
            assert time.monotonic() < deadline, "api role never served"
            try:
                async with httpx2.AsyncClient(base_url=base) as http:
                    if (await http.get("/healthz")).status_code == 200:
                        break
            except httpx2.TransportError:
                pass
            await asyncio.sleep(0.05)
        client = LiveClient(base)
        [ready] = await client.wait_for(1)
        assert ready["event"] == EVENT_READY
        started = time.monotonic()
        stop.set()
        await asyncio.wait_for(role, timeout=STOP_BUDGET_SECONDS + 2)
        elapsed = time.monotonic() - started
        for _ in range(100):
            if client.ended:
                break
            await asyncio.sleep(0.02)
        assert client.ended, "the open SSE response must end when the role stops"
        return elapsed

    elapsed = asyncio.run(scenario())
    assert elapsed < STOP_BUDGET_SECONDS, f"run_api took {elapsed:.2f}s to stop"
    # The stream ended because the hub closed it, not because uvicorn's
    # graceful-shutdown backstop cancelled it.
    assert elapsed < GRACEFUL_SHUTDOWN_SECONDS, f"run_api took {elapsed:.2f}s: the backstop fired"
