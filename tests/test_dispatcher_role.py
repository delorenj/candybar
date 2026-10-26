"""Dispatcher role wiring: durable consumer, registry inventory, sink and shutdown."""

from __future__ import annotations

import asyncio
from typing import Any

from candybar.daemon import ROLES, resolve_roles
from candybar.dispatcher import role


def test_dispatcher_role_registered():
    assert ROLES["dispatcher"] is role.run_dispatcher
    assert resolve_roles("dispatcher") == ["dispatcher"]


def test_dispatcher_stops_before_connect_without_engine(monkeypatch: Any):
    async def connect(*_args: Any, **_kwargs: Any):
        return None

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "create_registry_engine", lambda: 1 / 0)
    asyncio.run(role.run_dispatcher(asyncio.Event()))


def test_role_wires_durable_inventory_and_sink(monkeypatch: Any):
    captured: dict[str, Any] = {}
    stop = asyncio.Event()

    class FakeEngine:
        async def dispose(self):
            captured["disposed"] = True

    class FakeNC:
        def jetstream(self):
            return FakeJS()

        async def drain(self):
            captured["drained"] = True

    class FakeJS:
        async def pull_subscribe(self, subject: str, **kwargs: Any):
            captured["subject"] = subject
            captured.update(kwargs)
            return FakeSub()

    class FakeSub:
        async def fetch(self, *_args: Any, **_kwargs: Any):
            stop.set()
            return [object()]

    async def connect(url: str, **kwargs: Any):
        captured["connect_url"] = url
        captured["connect_name"] = kwargs["name"]
        return FakeNC()

    class FakeSink:
        def __init__(self, nc: Any):
            captured["sink_nc"] = nc

        async def publish(self, event: Any):
            captured["published"] = event

    class FakeDispatcher:
        def __init__(self, schedules: Any, publish: Any):
            captured["schedules"] = schedules
            captured["publish"] = publish

        async def handle_message(self, msg: Any):
            captured["message"] = msg

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "nats_url", lambda: "nats://test.delo.sh:4222")
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", FakeSink)
    monkeypatch.setattr(role, "Dispatcher", FakeDispatcher)
    asyncio.run(role.run_dispatcher(stop))
    assert captured["connect_name"] == "candybar-dispatcher"
    assert captured["subject"] == role.TICK_FILTER
    assert captured["stream"] == role.EVENT_STREAM
    assert captured["durable"] == role.DURABLE
    assert captured["config"].filter_subject == role.TICK_FILTER
    assert captured["config"].ack_wait == 120
    assert "message" in captured
    assert captured["disposed"] and captured["drained"]
