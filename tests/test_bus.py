"""Unit tests for candybar.bus: connect_nats retry posture and NatsEventSink."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

import pytest

from candybar.bus import NatsEventSink, connect_nats
from candybar.events.builders import build_candybar_created

LOG = logging.getLogger("test.bus")


def test_connect_nats_returns_none_when_stopped_before_connect(monkeypatch: Any) -> None:
    async def fake_connect(url: str, **kwargs: Any) -> Any:
        raise AssertionError("must not attempt a connect once stopped")

    monkeypatch.setattr("candybar.bus.nats.connect", fake_connect)

    async def drive() -> Any:
        stop = asyncio.Event()
        stop.set()
        return await connect_nats("nats://x:4222", name="t", stop=stop, log=LOG)

    assert asyncio.run(drive()) is None


def test_connect_nats_retries_until_success(monkeypatch: Any) -> None:
    attempts: list[dict[str, Any]] = []

    class FakeNC:
        pass

    async def fake_connect(url: str, **kwargs: Any) -> Any:
        attempts.append({"url": url, **kwargs})
        if len(attempts) < 3:
            raise ConnectionError("refused")
        return FakeNC()

    monkeypatch.setattr("candybar.bus.nats.connect", fake_connect)

    async def drive() -> Any:
        return await connect_nats(
            "nats://x:4222", name="t", stop=asyncio.Event(), log=LOG, retry_seconds=0.01
        )

    nc = asyncio.run(drive())
    assert isinstance(nc, FakeNC)
    assert len(attempts) == 3
    assert all(a["max_reconnect_attempts"] == -1 for a in attempts)


def test_connect_nats_stops_mid_retry(monkeypatch: Any) -> None:
    async def fake_connect(url: str, **kwargs: Any) -> Any:
        raise ConnectionError("refused")

    monkeypatch.setattr("candybar.bus.nats.connect", fake_connect)

    async def drive() -> Any:
        stop = asyncio.Event()
        task = asyncio.create_task(
            connect_nats("nats://x:4222", name="t", stop=stop, log=LOG, retry_seconds=30)
        )
        await asyncio.sleep(0.05)  # let the first attempt fail into the retry wait
        stop.set()
        return await asyncio.wait_for(task, timeout=2)  # must NOT wait out the 30s

    assert asyncio.run(drive()) is None


def _refused_url() -> str:
    """A loopback URL nothing listens on (bound, then released)."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"nats://127.0.0.1:{port}"


def test_connect_nats_real_client_stops_during_bus_outage(caplog: Any) -> None:
    """Against REAL nats-py, not a raising double: with max_reconnect_attempts=-1
    nats.connect never raises while the bus is down — it retries the initial
    connect internally forever. A stop must still end the connect promptly,
    and the per-attempt errors log as one line each, never a traceback."""

    async def drive() -> Any:
        stop = asyncio.Event()
        task = asyncio.create_task(
            connect_nats(_refused_url(), name="t", stop=stop, log=LOG, retry_seconds=30)
        )
        await asyncio.sleep(2.5)  # past nats-py's 2s reconnect_time_wait: >= 2 attempts
        stop.set()
        return await asyncio.wait_for(task, timeout=3)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(drive()) is None
    refusals = [r for r in caplog.records if r.name == LOG.name and "Connect call failed" in r.getMessage()]
    assert len(refusals) >= 2
    assert not any(r.exc_info for r in caplog.records)


class _FakeJS:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def publish(self, subject: str, payload: bytes, **kwargs: Any) -> None:
        self.calls.append({"subject": subject, "payload": payload, **kwargs})


class _FakeNC:
    def __init__(self, js: _FakeJS) -> None:
        self._js = js

    def jetstream(self) -> _FakeJS:
        return self._js


def test_nats_event_sink_publishes_with_msg_id_header() -> None:
    js = _FakeJS()
    sink = NatsEventSink(_FakeNC(js))
    event = build_candybar_created(
        candybar_id=uuid4(), name="demo", description="", tags=[], schedule="* * * * *"
    )

    asyncio.run(sink.publish(event))

    assert len(js.calls) == 1
    call = js.calls[0]
    assert call["subject"] == "bloodbank.evt.cron.candybar.created"
    assert call["headers"] == {"Nats-Msg-Id": str(event.id)}
    assert str(event.id) in call["payload"].decode()


@pytest.mark.parametrize("env", [{}, {"NATS_URL": "nats://bus:4222"}])
def test_nats_url_env_convention(monkeypatch: Any, env: dict[str, str]) -> None:
    from candybar.bus import DEFAULT_NATS_URL, nats_url

    monkeypatch.delenv("NATS_URL", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert nats_url() == env.get("NATS_URL", DEFAULT_NATS_URL)
