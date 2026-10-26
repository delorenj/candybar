"""Story 7.2 (AD-17): the host executor.

Covers the three code-owned pieces — the executor's own `/metrics` endpoint,
the alembic-head guard, and their wiring into the role — plus the tracked
deploy artifacts (systemd unit, container composition) that make it a host
process. The suite-wide conftest turns the endpoint and the guard off; every
test here switches on exactly what it drives.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import CollectorRegistry, Counter

from candybar.executor import metrics_server, role
from candybar.executor.metrics_server import MetricsServer, metrics_bind, render
from candybar.registry import schema_guard
from candybar.registry.schema_guard import (
    SchemaGate,
    SchemaGuardError,
    database_head,
    own_head,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
HOST_DIR = REPO_ROOT / "deploy" / "host"


# ---------------------------------------------------------------------------
# /metrics endpoint
# ---------------------------------------------------------------------------


def _registry() -> CollectorRegistry:
    reg = CollectorRegistry()
    counter = Counter("host_exec_test", "test counter", ["candybar_id"], registry=reg)
    counter.labels("abc").inc(2, exemplar={"invocation_id": "inv-1"})
    return reg


def test_metrics_bind_defaults_and_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(metrics_server.METRICS_PORT_ENV, raising=False)
    monkeypatch.delenv(metrics_server.METRICS_HOST_ENV, raising=False)
    assert metrics_bind() == ("127.0.0.1", 8701)
    monkeypatch.setenv(metrics_server.METRICS_HOST_ENV, "0.0.0.0")
    monkeypatch.setenv(metrics_server.METRICS_PORT_ENV, "9999")
    assert metrics_bind() == ("0.0.0.0", 9999)
    monkeypatch.setenv(metrics_server.METRICS_PORT_ENV, "")
    assert metrics_bind() is None


def test_render_negotiates_openmetrics_for_exemplars() -> None:
    reg = _registry()
    plain = render("GET", "/metrics", "", reg).decode()
    assert plain.startswith("HTTP/1.1 200 OK")
    assert "text/plain" in plain
    assert 'host_exec_test_total{candybar_id="abc"} 2.0' in plain
    om = render("GET", "/metrics?x=1", "application/openmetrics-text", reg).decode()
    assert "application/openmetrics-text" in om
    assert 'invocation_id="inv-1"' in om  # the exemplar survives only in OpenMetrics


def test_render_routes() -> None:
    reg = _registry()
    assert render("GET", "/healthz", "", reg).startswith(b"HTTP/1.1 200 OK")
    assert render("GET", "/nope", "", reg).startswith(b"HTTP/1.1 404")
    assert render("POST", "/metrics", "", reg).startswith(b"HTTP/1.1 405")


async def _get(port: int, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


def test_server_serves_over_a_real_socket() -> None:
    async def scenario() -> None:
        server = MetricsServer("127.0.0.1", 0, registry=_registry())
        await server.start()
        try:
            assert server.port != 0
            ok = await _get(
                server.port,
                b"GET /metrics HTTP/1.1\r\nHost: x\r\nAccept: application/openmetrics-text\r\n\r\n",
            )
            assert ok.startswith(b"HTTP/1.1 200 OK")
            assert b'invocation_id="inv-1"' in ok
            assert (await _get(server.port, b"GET /healthz HTTP/1.1\r\n\r\n")).startswith(
                b"HTTP/1.1 200"
            )
            bad = await _get(server.port, b"\r\n\r\n")
            assert bad.startswith(b"HTTP/1.1 400")
        finally:
            await server.close()
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", server.port)

    asyncio.run(scenario())


def test_start_metrics_server_survives_a_bind_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Observability never gates execution: a taken port logs and returns None."""

    async def scenario() -> None:
        taken = MetricsServer("127.0.0.1", 0, registry=_registry())
        await taken.start()
        try:
            monkeypatch.setenv(metrics_server.METRICS_HOST_ENV, "127.0.0.1")
            monkeypatch.setenv(metrics_server.METRICS_PORT_ENV, str(taken.port))
            assert await metrics_server.start_metrics_server() is None
        finally:
            await taken.close()

    asyncio.run(scenario())
    assert "WITHOUT a scrape endpoint" in caplog.text


def test_start_metrics_server_disabled_by_empty_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(metrics_server.METRICS_PORT_ENV, "")
    assert asyncio.run(metrics_server.start_metrics_server()) is None


# ---------------------------------------------------------------------------
# alembic-head guard
# ---------------------------------------------------------------------------


def _script_heads() -> list[str]:
    """Independent read of the migration scripts: revisions nobody revises."""
    versions = (REPO_ROOT / "deploy" / "alembic" / "versions").glob("*.py")
    revisions, downs = set(), set()
    for path in versions:
        text = path.read_text()
        rev = re.search(r'^revision(?::\s*str)?\s*=\s*"([^"]+)"', text, re.MULTILINE)
        down = re.search(r'^down_revision[^=]*=\s*"([^"]+)"', text, re.MULTILINE)
        assert rev, path
        revisions.add(rev.group(1))
        if down:
            downs.add(down.group(1))
    return sorted(revisions - downs)


def test_own_head_is_the_single_script_head() -> None:
    assert own_head() == _script_heads()[0]
    assert len(_script_heads()) == 1


def test_own_head_fails_closed_without_the_checkout(tmp_path: Path) -> None:
    with pytest.raises(SchemaGuardError, match="alembic scripts not found"):
        own_head(tmp_path / "missing.ini")


def test_guard_env_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(schema_guard.SCHEMA_GUARD_ENV, raising=False)
    assert schema_guard.guard_enabled() is True
    for off in ("off", "0", "FALSE"):
        monkeypatch.setenv(schema_guard.SCHEMA_GUARD_ENV, off)
        assert schema_guard.guard_enabled() is False


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_gate_follows_the_database_head(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="candybar.registry.schema_guard")
    heads: list[Any] = ["0004", "0004", "0005", "0006"]
    reads: list[Any] = []

    async def fake_head(_engine: Any) -> Any:
        value = heads.pop(0)
        reads.append(value)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(schema_guard, "database_head", fake_head)
    clock = _Clock()
    gate = SchemaGate(
        object(), "0005", clock=clock, blocked_recheck=5.0, open_recheck=30.0  # type: ignore[arg-type]
    )

    async def scenario() -> list[bool]:
        seen = []
        seen.append(await gate.open())  # 0004: closed
        clock.now += 1
        seen.append(await gate.open())  # cached: no read, still closed
        clock.now += 5
        seen.append(await gate.open())  # 0004 again: closed
        clock.now += 5
        seen.append(await gate.open())  # 0005: open
        clock.now += 10
        seen.append(await gate.open())  # cached open: no read
        clock.now += 30
        seen.append(await gate.open())  # 0006: closed again
        return seen

    assert asyncio.run(scenario()) == [False, False, False, True, True, False]
    assert reads == ["0004", "0004", "0005", "0006"]
    assert "schema guard CLOSED" in caplog.text
    assert "schema guard open" in caplog.text


def test_gate_fails_closed_on_an_unreadable_database(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(_engine: Any) -> Any:
        raise OSError("db down")

    monkeypatch.setattr(schema_guard, "database_head", boom)
    gate = SchemaGate(object(), "0005")  # type: ignore[arg-type]
    assert asyncio.run(gate.open()) is False


@pytest.mark.skipif(os.environ.get("CANDYBAR_TEST_PG") != "1", reason="needs CANDYBAR_TEST_PG=1")
def test_database_head_against_real_postgres() -> None:
    """Never-migrated reads as None; stamped reads as the script head."""
    from alembic import command
    from alembic.config import Config

    from candybar.registry import create_registry_engine, normalize_dsn

    dsn = normalize_dsn(
        os.environ.get("DATABASE_URL", "postgresql://localhost:5432/candybar_test")
    )
    cfg = Config(str(REPO_ROOT / "deploy" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", dsn)
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "head")

    async def scenario() -> None:
        engine = create_registry_engine()
        try:
            assert await database_head(engine) == own_head()
            gate = SchemaGate(engine, own_head())
            assert await gate.open() is True
            assert await SchemaGate(engine, "not-a-head").open() is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# role wiring
# ---------------------------------------------------------------------------


def _infra(captured: dict[str, Any], stop: asyncio.Event, fetch: Any):
    class FakeEngine:
        async def dispose(self) -> None:
            captured["disposed"] = True

    class FakeSub:
        async def fetch(self, *a: Any, **k: Any) -> list[Any]:
            captured["fetches"] = captured.get("fetches", 0) + 1
            return await fetch()

        async def unsubscribe(self) -> None:
            pass

    class FakeJS:
        async def pull_subscribe(self, *a: Any, **k: Any) -> Any:
            return FakeSub()

    class FakeNC:
        def jetstream(self) -> Any:
            return FakeJS()

        async def drain(self) -> None:
            captured["drained"] = True

    async def connect(*a: Any, **k: Any) -> Any:
        captured["connected"] = True
        return FakeNC()

    class FakeSink:
        def __init__(self, nc: Any) -> None:
            pass

        async def publish(self, event: Any) -> None:
            pass

    class FakeExecutor:
        def __init__(self, **kwargs: Any) -> None:
            pass

    return FakeEngine, connect, FakeSink, FakeExecutor


def test_role_consumes_nothing_while_the_gate_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    stop = asyncio.Event()

    async def fetch() -> list[Any]:
        stop.set()
        return []

    FakeEngine, connect, FakeSink, FakeExecutor = _infra(captured, stop, fetch)
    states = iter([False, False, True])

    class FakeGate:
        def __init__(self, engine: Any, expected: str) -> None:
            captured["expected"] = expected

        async def open(self) -> bool:
            return next(states)

    async def fast_sleep(_stop: asyncio.Event, _seconds: float) -> None:
        captured["slept"] = captured.get("slept", 0) + 1
        await asyncio.sleep(0)

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "create_registry_engine", FakeEngine)
    monkeypatch.setattr(role, "NatsEventSink", FakeSink)
    monkeypatch.setattr(role, "Executor", FakeExecutor)
    monkeypatch.setattr(role, "guard_enabled", lambda: True)
    monkeypatch.setattr(role, "own_head", lambda: "0005_head")
    monkeypatch.setattr(role, "SchemaGate", FakeGate)
    monkeypatch.setattr(role, "sleep_or_stop", fast_sleep)
    asyncio.run(asyncio.wait_for(role.run_executor(stop), timeout=10))

    assert captured["expected"] == "0005_head"
    assert captured["slept"] == 2  # two closed checks, then it opened
    assert captured["fetches"] == 1  # and only then did it pull a command
    assert captured["disposed"] and captured["drained"]


def test_role_refuses_to_start_when_its_own_head_is_unknowable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    stop = asyncio.Event()
    _engine, connect, _sink, _executor = _infra(captured, stop, lambda: None)  # type: ignore[arg-type]

    def unknowable() -> str:
        raise SchemaGuardError("no scripts")

    monkeypatch.setattr(role, "connect_nats", connect)
    monkeypatch.setattr(role, "guard_enabled", lambda: True)
    monkeypatch.setattr(role, "own_head", unknowable)
    with pytest.raises(SchemaGuardError):
        asyncio.run(role.run_executor(stop))
    assert "connected" not in captured  # never touched the bus


def test_role_serves_and_closes_the_metrics_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    class FakeServer:
        async def close(self) -> None:
            events.append("closed")

    async def start() -> Any:
        events.append("started")
        return FakeServer()

    async def run(_stop: asyncio.Event) -> None:
        events.append("ran")
        raise RuntimeError("role crashed")

    monkeypatch.setattr(role, "start_metrics_server", start)
    monkeypatch.setattr(role, "_run_executor", run)
    with pytest.raises(RuntimeError):
        asyncio.run(role.run_executor(asyncio.Event()))
    assert events == ["started", "ran", "closed"]  # closed even when the role crashes


# ---------------------------------------------------------------------------
# deploy artifacts: the host unit and the container composition
# ---------------------------------------------------------------------------


def _unit() -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = ""
    for line in (HOST_DIR / "candybar-executor.service").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            current = line
            sections.setdefault(current, [])
        else:
            sections[current].append(line)
    return sections


def test_unit_runs_only_the_executor_role_delegated() -> None:
    unit = _unit()
    service = unit["[Service]"]
    assert "Delegate=yes" in service
    assert "DelegateSubgroup=main" in service
    assert any(line.startswith("ExecStart=") and "run-executor.sh" in line for line in service)
    assert "Restart=always" in service
    assert any(line.startswith("EnvironmentFile=") for line in service)
    assert "WantedBy=default.target" in unit["[Install]"]
    wrapper = (HOST_DIR / "run-executor.sh").read_text()
    assert "--roles executor" in wrapper
    assert wrapper.count("--roles") == 1
    assert os.access(HOST_DIR / "run-executor.sh", os.X_OK)


def test_unit_exposes_metrics_on_8701_for_the_container_network() -> None:
    env = "\n".join(_unit()["[Service]"])
    assert "CANDYBAR_EXECUTOR_METRICS_PORT=8701" in env
    assert "CANDYBAR_EXECUTOR_METRICS_HOST=0.0.0.0" in env


def test_unit_orders_after_the_services_it_depends_on_without_a_hard_binding() -> None:
    # The executor must outlive a candybar-container redeploy: no Requires=/BindsTo=.
    unit_text = (HOST_DIR / "candybar-executor.service").read_text()
    assert not re.search(r"^(Requires|BindsTo)=", unit_text, re.MULTILINE)


def test_host_env_template_holds_only_references() -> None:
    template = (HOST_DIR / "executor.env.template").read_text()
    assigned = [ln for ln in template.splitlines() if ln and not ln.startswith("#")]
    assert any(ln.startswith("DATABASE_URL=op://DeLoSecrets/") for ln in assigned)
    assert not re.search(r"://[^/\s]*:[^/\s@]+@", template)  # no inline credentials


def test_container_runs_no_executor() -> None:
    compose = (REPO_ROOT / "deploy" / "compose.yml").read_text()
    match = re.search(r"^\s*command:\s*\[(.*?)\]", compose, re.MULTILINE)
    assert match, "compose must pin the container roles explicitly"
    assert "heartbeat,dispatcher,api" in match.group(1).replace(" ", "").replace('"', "")
    assert "executor" not in match.group(1)
    # no execution environment left in the container
    for dead in ("privileged: true", "gpus: all", "pid: host"):
        assert dead not in compose
    # and no host/container routing field anywhere in the invoke path
    for src in (REPO_ROOT / "src" / "candybar").rglob("*.py"):
        assert not re.search(r"\bexecutor_host\b|\brun_on\b|\bplacement\b", src.read_text()), src
