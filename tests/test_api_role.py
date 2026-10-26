"""Unit tests for the api daemon role wiring (run_api).

The uvicorn server and NATS connect are stubbed; what is under test is the
role's own wiring: engine creation, create_app kwargs, serve-until-stop,
dispose/drain on shutdown, and the stop-before-connect early exit.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from nats.js.api import DeliverPolicy

import candybar.api.role as role_mod
from candybar.api.stream import ScheduleStream
from candybar.bus import NatsEventSink
from candybar.daemon import ROLES, resolve_roles


def test_api_role_registered_and_resolvable() -> None:
    assert ROLES["api"] is role_mod.run_api
    assert resolve_roles("api") == ["api"]
    assert resolve_roles("all") == ["api", "dispatcher", "executor", "heartbeat"]


def test_run_api_stopped_before_connect_serves_nothing(monkeypatch: Any) -> None:
    async def fake_connect_nats(*args: Any, **kwargs: Any) -> None:
        return None

    def forbidden_create_app(**kwargs: Any) -> Any:
        raise AssertionError("create_app must not run when the bus connect was stopped")

    monkeypatch.setattr(role_mod, "connect_nats", fake_connect_nats)
    monkeypatch.setattr(role_mod, "create_app", forbidden_create_app)

    asyncio.run(role_mod.run_api(asyncio.Event()))


class _FakeFeed:
    def __init__(self) -> None:
        self.unsubscribed = False

    async def unsubscribe(self) -> None:
        self.unsubscribed = True


class _FakeJS:
    def __init__(self, nc: _FakeNC) -> None:
        self.nc = nc

    async def subscribe(self, subject: str, **kwargs: Any) -> _FakeFeed:
        if self.nc.feed_error is not None:
            raise self.nc.feed_error
        self.nc.subscriptions.append((subject, kwargs))
        return self.nc.feed


class _FakeNC:
    def __init__(self, feed_error: Exception | None = None) -> None:
        self.drained = False
        self.feed = _FakeFeed()
        self.feed_error = feed_error
        self.subscriptions: list[tuple[str, dict[str, Any]]] = []

    def jetstream(self) -> _FakeJS:
        return _FakeJS(self)

    async def drain(self) -> None:
        self.drained = True


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


class _FakeServer:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.should_exit = False
        self.served = False

    async def serve(self) -> None:
        self.served = True
        while not self.should_exit:
            await asyncio.sleep(0.01)


def test_run_api_wires_engine_app_server_and_drains(monkeypatch: Any) -> None:
    nc = _FakeNC()
    engine = _FakeEngine()
    captured: dict[str, Any] = {}

    async def fake_connect_nats(url: str, **kwargs: Any) -> Any:
        captured["url"] = url
        captured["connect_name"] = kwargs["name"]
        return nc

    def fake_create_app(*, session_factory: Any, sink: Any, stream: Any) -> str:
        captured["session_factory"] = session_factory
        captured["sink"] = sink
        captured["stream"] = stream
        return "the-app"

    servers: list[_FakeServer] = []

    monkeypatch.setattr(role_mod, "connect_nats", fake_connect_nats)
    monkeypatch.setattr(role_mod, "create_registry_engine", lambda: engine)
    monkeypatch.setattr(role_mod, "create_app", fake_create_app)
    monkeypatch.setattr(
        role_mod.uvicorn, "Server", lambda config: servers.append(_FakeServer(config)) or servers[0]
    )
    monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:1")

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(role_mod.run_api(stop))
        await asyncio.sleep(0.1)  # let the server start serving
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())

    assert captured["connect_name"] == "candybar-api"
    assert captured["url"] == "nats://127.0.0.1:1"
    assert isinstance(captured["sink"], NatsEventSink)
    assert callable(captured["session_factory"])
    server = servers[0]
    assert server.config.app == "the-app"
    assert server.served and server.should_exit
    assert engine.disposed and nc.drained
    # Story 5.4: the hub is fed by ONE ordered, ephemeral consumer on the cron
    # facts (never a durable that could compete with the dispatcher's), and
    # every stream is closed on stop so uvicorn can finish.
    hub = captured["stream"]
    assert isinstance(hub, ScheduleStream)
    [(subject, kwargs)] = nc.subscriptions
    assert subject == role_mod.STREAM_FILTER == "bloodbank.evt.cron.>"
    assert kwargs["stream"] == role_mod.EVENT_STREAM == "BLOODBANK_EVENTS"
    assert kwargs["ordered_consumer"] is True
    assert kwargs["deliver_policy"] == DeliverPolicy.NEW
    assert "durable" not in kwargs and callable(kwargs["cb"])
    assert nc.feed.unsubscribed
    assert hub._closed


def test_run_api_serves_rest_when_the_live_feed_cannot_attach(monkeypatch: Any) -> None:
    """A missing event stream must not take REST down: log it and keep serving."""
    from nats.js.errors import NotFoundError

    nc = _FakeNC(feed_error=NotFoundError())
    engine = _FakeEngine()
    servers: list[_FakeServer] = []

    async def fake_connect_nats(url: str, **kwargs: Any) -> Any:
        return nc

    monkeypatch.setattr(role_mod, "connect_nats", fake_connect_nats)
    monkeypatch.setattr(role_mod, "create_registry_engine", lambda: engine)
    monkeypatch.setattr(role_mod, "create_app", lambda **kwargs: "the-app")
    monkeypatch.setattr(
        role_mod.uvicorn, "Server", lambda config: servers.append(_FakeServer(config)) or servers[0]
    )

    async def drive() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(role_mod.run_api(stop))
        await asyncio.sleep(0.1)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(drive())
    assert servers[0].served and nc.drained and engine.disposed


def test_feed_hands_cron_facts_to_the_hub_and_drops_the_rest() -> None:
    from candybar.events import build_tick_fired

    handled: list[Any] = []

    class Hub:
        async def handle(self, fact: Any) -> None:
            handled.append(fact)

    nc = _FakeNC()

    class Msg:
        def __init__(self, data: bytes) -> None:
            self.data = data
            self.subject = "bloodbank.evt.cron.tick.fired"

    async def drive() -> None:
        await role_mod.subscribe_feed(nc, Hub())  # type: ignore[arg-type]
        cb = nc.subscriptions[0][1]["cb"]
        tick = build_tick_fired(minute=datetime(2026, 10, 26, 10, 15, tzinfo=UTC))
        await cb(Msg(tick.model_dump_json().encode()))
        await cb(Msg(b"not json"))
        await cb(Msg(b'{"type": "bloodbank.other.thing.happened"}'))
        await cb(Msg(b"[1, 2]"))

    asyncio.run(drive())
    assert [f.data.tick_id for f in handled] == ["tick:29883495"]
