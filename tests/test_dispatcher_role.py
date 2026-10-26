"""Dispatcher role wiring: durable consumer, registry inventory, sink and shutdown."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime
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
            captured.setdefault("shutdown_order", []).append("drain")

    class FakeJS:
        async def pull_subscribe(self, subject: str, **kwargs: Any):
            captured["subject"] = subject
            captured.update(kwargs)
            return FakeSub()

    class FakeSub:
        async def fetch(self, *_args: Any, **_kwargs: Any):
            stop.set()
            return [object()]

        async def unsubscribe(self):
            captured.setdefault("shutdown_order", []).append("unsubscribe")

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
        def __init__(self, schedules: Any, publish: Any, cursor: Any = None, *, skips: Any = None):
            captured["schedules"] = schedules
            captured["publish"] = publish
            captured["cursor"] = cursor
            captured["skips"] = skips

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
    # Story 2.2: the misfire cursor is the registry row of THE tick durable.
    assert isinstance(captured["cursor"], role.RegistryTickCursor)
    assert captured["cursor"].name == role.DURABLE
    # Story 2.3: operator skips are read from the registry on every tick,
    # through list_skips over the role's engine, for the requested window.
    seen: dict[str, Any] = {}

    @contextlib.asynccontextmanager
    async def fake_scope(engine: Any):
        seen["engine"] = engine
        yield "session"

    async def fake_list_skips(session: Any, start: Any, end: Any):
        seen["args"] = (session, start, end)
        return ["row"]

    monkeypatch.setattr(role, "session_scope", fake_scope)
    monkeypatch.setattr(role, "list_skips", fake_list_skips)
    start, end = datetime(2026, 10, 26, 10, 0, tzinfo=UTC), datetime(2026, 10, 26, 10, 1, tzinfo=UTC)
    assert asyncio.run(captured["skips"](start, end)) == ["row"]
    assert isinstance(seen["engine"], FakeEngine)
    assert seen["args"] == ("session", start, end)
    assert captured["disposed"] and captured["drained"]
    # Fast-stop contract (Story 1.9): unsubscribe before drain so an idle
    # stop completes well inside docker's 10s default grace.
    assert captured["shutdown_order"] == ["unsubscribe", "drain"]


# ---------------------------------------------------------------------------
# Story 2.1: only the dispatcher's single durable consumer evaluates schedules
# ---------------------------------------------------------------------------


def test_dispatcher_role_never_creates_a_second_consumer(monkeypatch: Any):
    """Restarted and concurrent dispatcher roles all bind the ONE production
    durable on the events stream — never an ephemeral or differently named
    consumer. The live proof is in the fault-injection suite."""
    calls: list[dict[str, Any]] = []

    class FakeEngine:
        async def dispose(self):
            return None

    def make_nc(stop: asyncio.Event):
        class FakeSub:
            async def fetch(self, *_args: Any, **_kwargs: Any):
                stop.set()
                return []

            async def unsubscribe(self):
                return None

        class FakeJS:
            async def pull_subscribe(self, subject: str, **kwargs: Any):
                calls.append({"subject": subject, **kwargs})
                return FakeSub()

        class FakeNC:
            def jetstream(self):
                return FakeJS()

            async def drain(self):
                return None

        return FakeNC()

    stops: dict[int, asyncio.Event] = {}

    async def connect(_url: str, **kwargs: Any):
        return make_nc(kwargs["stop"])

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", lambda nc: type("S", (), {"publish": None})())

    async def restarts_then_concurrent():
        for n in range(3):  # sequential restarts
            stops[n] = asyncio.Event()
            await role.run_dispatcher(stops[n])
        pair = [asyncio.Event(), asyncio.Event()]  # two roles at once
        await asyncio.gather(*(role.run_dispatcher(s) for s in pair))

    asyncio.run(restarts_then_concurrent())
    assert len(calls) == 5
    for call in calls:
        assert call["subject"] == role.TICK_FILTER
        assert call["stream"] == role.EVENT_STREAM
        assert call["durable"] == role.DURABLE == "candybar-dispatcher-ticks"
        assert call["config"].durable_name == role.DURABLE
        assert call["config"].filter_subject == role.TICK_FILTER
    assert {c["durable"] for c in calls} == {role.DURABLE}


def test_no_other_module_subscribes_to_ticks():
    """Static guard (AD-5): across src/candybar, the only JetStream/NATS
    subscriptions are the dispatcher's tick durable, the executor's invoke
    durable and the api's live-stream feed (Story 5.4). Only the dispatcher's
    durable evaluates schedules; the feed taps ticks as AD-5 allows any other
    subscriber to: ephemeral and ordered (no durable to compete for ticks),
    deduping on tick_id (tests/test_schedule_stream.py pins that)."""
    import ast
    from pathlib import Path

    src = Path(role.__file__).resolve().parent.parent
    subscribe_attrs = {"subscribe", "pull_subscribe", "pull_subscribe_bind", "add_consumer"}
    found: set[tuple[str, str]] = set()
    calls: dict[tuple[str, str], ast.Call] = {}
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in subscribe_attrs
            ):
                first = node.args[0] if node.args else None
                label = ast.unparse(first) if first is not None else "<none>"
                site = (path.relative_to(src).as_posix(), label)
                found.add(site)
                calls[site] = node
    # A new subscriber must be added here deliberately. Any one that taps raw
    # ticks must be non-durable and dedupe on tick_id (AD-5).
    assert found == {
        ("dispatcher/role.py", "TICK_FILTER"),
        ("executor/role.py", "INVOKE_FILTER"),
        ("api/role.py", "filter_subject"),
    }, f"unexpected NATS subscription sites: {sorted(found)}"
    from candybar.api import role as api_role
    from candybar.executor import role as executor_role

    assert "tick" not in executor_role.INVOKE_FILTER
    feed = {kw.arg: ast.unparse(kw.value) for kw in calls[("api/role.py", "filter_subject")].keywords}
    assert feed["ordered_consumer"] == "True"
    assert "durable" not in feed and "queue" not in feed
    assert api_role.STREAM_FILTER == "bloodbank.evt.cron.>"
