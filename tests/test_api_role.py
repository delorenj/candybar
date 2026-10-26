"""Unit tests for the api daemon role wiring (run_api).

The uvicorn server and NATS connect are stubbed; what is under test is the
role's own wiring: engine creation, create_app kwargs, serve-until-stop,
dispose/drain on shutdown, and the stop-before-connect early exit.
"""

from __future__ import annotations

import asyncio
from typing import Any

import candybar.api.role as role_mod
from candybar.bus import NatsEventSink
from candybar.daemon import ROLES, resolve_roles


def test_api_role_registered_and_resolvable() -> None:
    assert ROLES["api"] is role_mod.run_api
    assert resolve_roles("api") == ["api"]
    assert resolve_roles("all") == ["api", "heartbeat"]


def test_run_api_stopped_before_connect_serves_nothing(monkeypatch: Any) -> None:
    async def fake_connect_nats(*args: Any, **kwargs: Any) -> None:
        return None

    def forbidden_create_app(**kwargs: Any) -> Any:
        raise AssertionError("create_app must not run when the bus connect was stopped")

    monkeypatch.setattr(role_mod, "connect_nats", fake_connect_nats)
    monkeypatch.setattr(role_mod, "create_app", forbidden_create_app)

    asyncio.run(role_mod.run_api(asyncio.Event()))


class _FakeJS:
    pass


class _FakeNC:
    def __init__(self) -> None:
        self.drained = False

    def jetstream(self) -> _FakeJS:
        return _FakeJS()

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

    def fake_create_app(*, session_factory: Any, sink: Any) -> str:
        captured["session_factory"] = session_factory
        captured["sink"] = sink
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
