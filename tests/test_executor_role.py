"""Executor role wiring: workqueue durable, registry store, sink, shutdown."""

from __future__ import annotations

import asyncio
from typing import Any

from candybar.daemon import ROLES, resolve_roles
from candybar.executor import role


def test_executor_role_registered() -> None:
    assert ROLES["executor"] is role.run_executor
    assert resolve_roles("executor") == ["executor"]


def test_executor_stops_before_connect_without_engine(monkeypatch: Any) -> None:
    async def connect(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "create_registry_engine", lambda: 1 / 0)
    asyncio.run(role.run_executor(asyncio.Event()))


def _fake_infra(captured: dict[str, Any], stop: asyncio.Event) -> None:
    class FakeEngine:
        async def dispose(self) -> None:
            captured["disposed"] = True

    class FakeNC:
        def jetstream(self) -> Any:
            return FakeJS()

        async def drain(self) -> None:
            captured["drained"] = True
            captured.setdefault("shutdown_order", []).append("drain")

    class FakeJS:
        async def pull_subscribe(self, subject: str, **kwargs: Any) -> Any:
            captured["subject"] = subject
            captured.update(kwargs)
            return FakeSub()

    class FakeSub:
        fetches = 0

        async def fetch(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            FakeSub.fetches += 1
            if FakeSub.fetches == 1:
                return [object()]
            stop.set()
            return []

        async def unsubscribe(self) -> None:
            captured.setdefault("shutdown_order", []).append("unsubscribe")

    async def connect(url: str, **kwargs: Any) -> Any:
        captured["connect_url"] = url
        captured["connect_name"] = kwargs["name"]
        return FakeNC()

    class FakeSink:
        def __init__(self, nc: Any) -> None:
            captured["sink_nc"] = nc

        async def publish(self, event: Any) -> None:
            captured["published"] = event

    class FakeExecutor:
        def __init__(self, **kwargs: Any) -> None:
            captured["executor_kwargs"] = kwargs
            self.registry = kwargs["registry"]

        async def handle_message(self, msg: Any) -> None:
            captured["message"] = msg

    return FakeEngine, connect, FakeSink, FakeExecutor


def test_role_wires_workqueue_durable_and_store(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    stop = asyncio.Event()
    FakeEngine, connect, FakeSink, FakeExecutor = _fake_infra(captured, stop)

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "nats_url", lambda: "nats://test.delo.sh:4222")
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", FakeSink)
    monkeypatch.setattr(role, "Executor", FakeExecutor)
    asyncio.run(role.run_executor(stop))

    assert captured["connect_name"] == "candybar-executor"
    assert captured["subject"] == role.INVOKE_FILTER
    assert captured["stream"] == role.COMMAND_STREAM
    assert captured["durable"] == role.DURABLE
    config = captured["config"]
    assert config.filter_subject == role.INVOKE_FILTER
    assert config.ack_wait == role.ACK_WAIT_SECONDS
    # WorkQueue-retention streams reject DeliverPolicy.NEW — pinned by the live bus.
    from nats.js.api import DeliverPolicy

    assert config.deliver_policy == DeliverPolicy.ALL
    assert "message" in captured  # the fetched command reached the executor
    assert callable(captured["executor_kwargs"]["by_key"])
    assert callable(captured["executor_kwargs"]["record"])
    # Story 7.5: the disabled guard is wired (a queued fire of a disabled
    # candybar is skipped, not run).
    assert callable(captured["executor_kwargs"]["enabled"])
    assert captured["disposed"] and captured["drained"]
    # Fast-stop contract (Story 1.9): detach the durable pull consumer before
    # draining, or nats-py waits the full ~30s drain timeout and docker
    # stop's 10s grace SIGKILLs us mid-drain.
    assert captured["shutdown_order"] == ["unsubscribe", "drain"]


def test_stop_terminates_inflight_children(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}
    stop = asyncio.Event()
    FakeEngine, connect, FakeSink, _FakeExecutor = _fake_infra(captured, stop)

    class SlowExecutor:
        def __init__(self, **kwargs: Any) -> None:
            self.registry = kwargs["registry"]

        async def handle_message(self, msg: Any) -> None:
            # Simulate a long run: finish only once the role terminates us.
            while not self.registry.stopping:
                await asyncio.sleep(0.01)
            captured["terminated_during_run"] = True

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "nats_url", lambda: "nats://test.delo.sh:4222")
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", FakeSink)
    monkeypatch.setattr(role, "Executor", SlowExecutor)
    asyncio.run(asyncio.wait_for(role.run_executor(stop), timeout=10))
    assert captured["terminated_during_run"] is True
    assert captured["disposed"] and captured["drained"]


def test_unsubscribe_failure_does_not_mask_drain(monkeypatch: Any) -> None:
    """nats-py unsubscribe raises on a dead/draining connection; the role must
    still drain (Story 1.9 review: an unsubscribe raise in the finally block
    would otherwise turn a graceful stop into a role crash)."""
    captured: dict[str, Any] = {}
    stop = asyncio.Event()
    FakeEngine, _connect, FakeSink, FakeExecutor = _fake_infra(captured, stop)

    import nats.errors

    class ExplodingSub:
        async def fetch(self, *_args: Any, **_kwargs: Any) -> list[Any]:
            stop.set()
            return []

        async def unsubscribe(self) -> None:
            raise nats.errors.ConnectionClosedError

    class FakeJSExploding:
        async def pull_subscribe(self, *_args: Any, **kwargs: Any) -> Any:
            captured.update(kwargs)
            return ExplodingSub()

    class FakeNCExploding:
        def jetstream(self) -> Any:
            return FakeJSExploding()

        async def drain(self) -> None:
            captured["drained"] = True

    async def connect_exploding(*_args: Any, **_kwargs: Any) -> Any:
        return FakeNCExploding()

    monkeypatch.setattr(role, "connect_nats", connect_exploding)
    monkeypatch.setattr(role, "nats_url", lambda: "nats://test.delo.sh:4222")
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", FakeSink)
    monkeypatch.setattr(role, "Executor", FakeExecutor)
    asyncio.run(role.run_executor(stop))
    assert captured["drained"] is True
