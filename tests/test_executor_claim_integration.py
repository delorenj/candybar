"""Live-Postgres proof of the deferred executor fixes (env-gated: CANDYBAR_TEST_PG=1).

- A job writing NUL to stderr (a shell job, and a real Rust binary whose
  default panic hook prints a NUL) records once, acks, and never re-executes.
- Hard-kill safety: the claim row commits before spawn; a redelivery of a
  claimed-but-unfinished key is recorded failed/abandoned without a second
  run, and `total_invoked` counts the key exactly once.

Uses throwaway candybar rows (deleted on teardown); the bus is an in-memory
recorder, since the poison was always the Postgres write.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

from candybar.events import build_candybar_invoke
from candybar.events.envelope import CandybarFailedEvent
from candybar.executor import ExecutorMetrics, ProcessRegistry, run_invocation
from candybar.executor.worker import ABANDONED_MESSAGE, RETRY_ON_ABANDON_TAG, Executor
from candybar.registry import (
    CandybarManifest,
    DuplicateInvocationError,
    InvocationClaim,
    candybar_tags,
    claim_invocation,
    create_registry_engine,
    get_entrypoint,
    insert_candybar,
    invocation_by_key,
    record_terminal_invocation,
    session_scope,
)
from candybar.registry.models import Candybar
from test_executor_integration import _ensure_schema

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)


class Msg:
    def __init__(self, data: bytes, num_delivered: int = 1) -> None:
        self.data = data
        self.acked = 0
        self.naks: list[float] = []
        self.metadata = type("Meta", (), {"num_delivered": num_delivered})()

    async def ack(self) -> None:
        self.acked += 1

    async def nak(self, delay: float | None = None) -> None:
        self.naks.append(delay or 0)

    async def term(self) -> None:  # pragma: no cover - not expected here
        raise AssertionError("unexpected term")

    async def in_progress(self) -> None:
        pass


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class Harness:
    """The production executor wiring (role.py) over a real registry."""

    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.events: list[Any] = []
        self.spawns = 0

    async def by_key(self, key: str):
        async with session_scope(self.engine) as session:
            return await invocation_by_key(session, key)

    async def entrypoint(self, candybar_id: UUID):
        async with session_scope(self.engine) as session:
            return await get_entrypoint(session, candybar_id)

    async def record(self, rec) -> bool:
        try:
            async with session_scope(self.engine) as session:
                await record_terminal_invocation(session, rec)
            return True
        except DuplicateInvocationError:
            return False

    async def claim(self, row: InvocationClaim) -> bool:
        try:
            async with session_scope(self.engine) as session:
                await claim_invocation(session, row)
            return True
        except DuplicateInvocationError:
            return False

    async def retry_abandoned(self, candybar_id: UUID) -> bool:
        async with session_scope(self.engine) as session:
            tags = await candybar_tags(session, candybar_id)
        return tags is not None and RETRY_ON_ABANDON_TAG in tags

    async def publish(self, event: Any) -> None:
        event.model_dump_json().encode("utf-8")  # what the NATS sink does
        self.events.append(event)

    async def counting_run(self, **kwargs: Any):
        self.spawns += 1
        return await run_invocation(**kwargs)

    def executor(self) -> Executor:
        return Executor(
            by_key=self.by_key,
            entrypoint=self.entrypoint,
            record=self.record,
            publish=self.publish,
            run=self.counting_run,
            registry=ProcessRegistry(),
            metrics=ExecutorMetrics(CollectorRegistry()),
            claim=self.claim,
            retry_abandoned=self.retry_abandoned,
        )


async def _register(engine: Any, entrypoint: str, tags: list[str] | None = None) -> UUID:
    cid = uuid4()
    manifest = CandybarManifest.model_validate(
        {
            "name": f"itest-claim-{uuid4().hex[:12]}",
            "schedule": "* * * * *",
            "entrypoint": entrypoint,
            "tags": tags or [],
        }
    )
    async with session_scope(engine) as session:
        await insert_candybar(session, manifest, candybar_id=cid)
    return cid


async def _drop(engine: Any, cid: UUID) -> None:
    async with session_scope(engine) as session:
        row = await session.get(Candybar, cid)
        if row is not None:
            await session.delete(row)


async def _counters(engine: Any, cid: UUID) -> tuple[int, int]:
    async with session_scope(engine) as session:
        row = await session.get(Candybar, cid)
        return row.total_invoked, row.total_successful


def _command(cid: UUID):
    return build_candybar_invoke(
        candybar_id=cid,
        name="itest-claim",
        trigger="manual",
        tick_id=None,
        invocation_id=uuid4(),
        command_id=uuid4(),
    )


def _nul_stderr_round_trip(tmp_path: Path, monkeypatch: Any, entrypoint: str, marker: str):
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = Harness(engine)
        cid = await _register(engine, entrypoint)
        try:
            executor = harness.executor()
            cmd = _command(cid)
            first = Msg(cmd.model_dump_json().encode())
            await executor.handle_message(first)
            # Before the scrub: the terminal write raised, the command was
            # nak'd, and every redelivery ran the job again.
            assert first.acked == 1 and first.naks == []
            row = await harness.by_key(cmd.idempotency_key)
            assert row is not None and row.result == "fail"
            assert "\x00" not in row.stderr_tail and marker in row.stderr_tail
            redelivered = Msg(cmd.model_dump_json().encode(), num_delivered=2)
            await executor.handle_message(redelivered)
            assert redelivered.acked == 1 and harness.spawns == 1
            assert await _counters(engine, cid) == (1, 0)
            assert isinstance(harness.events[-1], CandybarFailedEvent)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_nul_in_stderr_records_once_and_never_reexecutes(tmp_path: Path, monkeypatch) -> None:
    entry = _script(tmp_path, "nul.sh", "printf 'before\\000after\\n' >&2; exit 2")
    _nul_stderr_round_trip(tmp_path, monkeypatch, entry, "before�after")


@pytest.mark.skipif(shutil.which("rustc") is None, reason="needs rustc")
def test_rust_panic_with_nul_records_once(tmp_path: Path, monkeypatch) -> None:
    """The 4.3 case: Rust's default panic hook prints the payload verbatim."""
    src = tmp_path / "panic.rs"
    src.write_text('fn main() { panic!("bad\\0byte"); }\n')
    binary = tmp_path / "panic-nul"
    subprocess.run(["rustc", "-O", "-o", str(binary), str(src)], check=True, timeout=300)
    _nul_stderr_round_trip(tmp_path, monkeypatch, str(binary), "bad�byte")


def test_hard_killed_claim_is_abandoned_not_rerun(tmp_path: Path, monkeypatch) -> None:
    """The executor died (SIGKILL/OOM) after claiming: only the claim row
    survived. The redelivery records it abandoned without spawning again."""
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = Harness(engine)
        cid = await _register(engine, _script(tmp_path, "ok.sh", "exit 0"))
        try:
            cmd = _command(cid)
            started = datetime.now(UTC)
            assert await harness.claim(
                InvocationClaim(
                    invocation_id=cmd.data.invocation_id,
                    candybar_id=cid,
                    trigger="manual",
                    tick_id=None,
                    idempotency_key=cmd.idempotency_key,
                    started_at=started,
                )
            )
            claimed = await harness.by_key(cmd.idempotency_key)
            assert claimed.result is None and claimed.finished_at is None
            assert await _counters(engine, cid) == (1, 0)

            msg = Msg(cmd.model_dump_json().encode(), num_delivered=2)
            await harness.executor().handle_message(msg)
            assert harness.spawns == 0 and msg.acked == 1
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "fail" and row.error_message == ABANDONED_MESSAGE
            assert row.finished_at is not None
            assert await _counters(engine, cid) == (1, 0)  # counted once, never successful
            assert isinstance(harness.events[-1], CandybarFailedEvent)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_opted_in_lost_run_reruns_and_counts_once(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = Harness(engine)
        cid = await _register(
            engine, _script(tmp_path, "ok.sh", "exit 0"), tags=[RETRY_ON_ABANDON_TAG]
        )
        try:
            cmd = _command(cid)
            await harness.claim(
                InvocationClaim(
                    invocation_id=cmd.data.invocation_id,
                    candybar_id=cid,
                    trigger="manual",
                    tick_id=None,
                    idempotency_key=cmd.idempotency_key,
                    started_at=datetime.now(UTC),
                )
            )
            msg = Msg(cmd.model_dump_json().encode(), num_delivered=2)
            await harness.executor().handle_message(msg)
            assert harness.spawns == 1 and msg.acked == 1
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "success"
            assert await _counters(engine, cid) == (1, 1)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_normal_run_claims_then_finalizes_counting_once(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = Harness(engine)
        cid = await _register(engine, _script(tmp_path, "ok.sh", "exit 0"))
        try:
            cmd = _command(cid)
            msg = Msg(cmd.model_dump_json().encode())
            await harness.executor().handle_message(msg)
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "success" and row.id == cmd.data.invocation_id
            assert await _counters(engine, cid) == (1, 1)
            # A second terminal write for the finalized key is a duplicate.
            rec_again = Msg(cmd.model_dump_json().encode(), num_delivered=2)
            await harness.executor().handle_message(rec_again)
            assert harness.spawns == 1 and await _counters(engine, cid) == (1, 1)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
