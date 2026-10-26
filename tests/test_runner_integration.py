"""Live-Postgres proof of runner kinds through the production executor wiring (Story 7.3).

Env-gated (CANDYBAR_TEST_PG=1). A candybar registered with a `runner` is
resolved by `get_run_target`, started by the real `run_target`, recorded in the
real registry (claim row, terminal row, counters), and its `on_failure` hook
fires exactly once; the systemd kind runs against the stub `systemctl` of
test_systemd_runner.py. The bus is an in-memory recorder.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from prometheus_client import CollectorRegistry

from candybar.executor import ExecutorMetrics, ProcessRegistry
from candybar.executor.targets import run_on_failure, run_target
from candybar.executor.worker import Executor
from candybar.registry import (
    CandybarManifest,
    create_registry_engine,
    get_run_target,
    insert_candybar,
    session_scope,
)
from test_executor_claim_integration import Harness, Msg, _command, _counters, _drop
from test_executor_integration import _ensure_schema
from test_systemd_runner import LOADED, RAN_OK, Stub

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)

PY = sys.executable


class RunnerHarness(Harness):
    """The role.py wiring, with run targets resolved the way the role does."""

    def __init__(self, engine: Any, **run_kwargs: Any) -> None:
        super().__init__(engine)
        self.run_kwargs = run_kwargs
        self.hook_calls = 0

    async def entrypoint(self, candybar_id: UUID):  # type: ignore[override]
        async with session_scope(self.engine) as session:
            return await get_run_target(session, candybar_id)

    async def counting_run(self, **kwargs: Any):
        self.spawns += 1
        return await run_target(root="", **self.run_kwargs, **kwargs)

    async def counting_hook(self, runner: dict, **kwargs: Any) -> None:
        self.hook_calls += 1
        await run_on_failure(runner, **self.run_kwargs, **kwargs)

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
            on_failure=self.counting_hook,
        )


async def _register_runner(engine: Any, runner: dict) -> UUID:
    cid = uuid4()
    manifest = CandybarManifest.model_validate(
        {"name": f"itest-runner-{uuid4().hex[:12]}", "schedule": "* * * * *", "runner": runner}
    )
    async with session_scope(engine) as session:
        await insert_candybar(session, manifest, candybar_id=cid)
    return cid


def test_exec_runner_runs_with_argv_env_and_workdir_and_is_recorded(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    seen = tmp_path / "seen.json"
    code = (
        "import json,os;"
        f"json.dump({{'cwd': os.getcwd(), 'lit': os.environ.get('JOB_LITERAL'), "
        f"'inv': os.environ['CANDYBAR_INVOCATION_ID']}}, open({str(seen)!r}, 'w'))"
    )
    workdir = tmp_path / "wd"
    workdir.mkdir()

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = RunnerHarness(engine)
        cid = await _register_runner(
            engine,
            {
                "kind": "exec",
                "argv": [PY, "-c", code],
                "env": {"JOB_LITERAL": "from-registry"},
                "workdir": str(workdir),
            },
        )
        try:
            cmd = _command(cid)
            msg = Msg(cmd.model_dump_json().encode())
            await harness.executor().handle_message(msg)
            assert msg.acked == 1 and msg.naks == []
            row = await harness.by_key(cmd.idempotency_key)
            assert row is not None and row.result == "success" and row.exit_code == 0
            info = json.loads(seen.read_text())
            assert info["cwd"] == str(workdir) and info["lit"] == "from-registry"
            assert info["inv"] == str(row.id)
            assert await _counters(engine, cid) == (1, 1)
            assert harness.hook_calls == 0
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(scenario())


def test_failed_exec_runner_records_failure_and_fires_on_failure_exactly_once(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    page = tmp_path / "page.json"
    hook = (
        "import json,os;"
        f"json.dump({{'msg': os.environ['CANDYBAR_FAILURE_MESSAGE'], 'id': os.environ['CANDYBAR_ID']}}, "
        f"open({str(page)!r}, 'a'))"
    )

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = RunnerHarness(engine)
        cid = await _register_runner(
            engine,
            {
                "kind": "exec",
                "argv": ["sh", "-c", "echo broke >&2; exit 4"],
                "on_failure": [PY, "-c", hook],
            },
        )
        try:
            cmd = _command(cid)
            first = Msg(cmd.model_dump_json().encode())
            await harness.executor().handle_message(first)
            assert first.acked == 1
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "fail" and row.exit_code == 4 and "broke" in row.stderr_tail
            assert json.loads(page.read_text()) == {"msg": "exit code 4", "id": str(cid)}
            # A redelivery of the recorded failure neither re-runs nor re-pages.
            again = Msg(cmd.model_dump_json().encode(), num_delivered=2)
            await harness.executor().handle_message(again)
            assert again.acked == 1 and harness.spawns == 1 and harness.hook_calls == 1
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(scenario())


def test_exec_runner_timeout_is_recorded_as_a_failure_naming_the_timeout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = RunnerHarness(engine)
        cid = await _register_runner(
            engine, {"kind": "exec", "argv": ["sleep", "30"], "timeout_s": 1}
        )
        try:
            cmd = _command(cid)
            await harness.executor().handle_message(Msg(cmd.model_dump_json().encode()))
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "fail" and row.error_message == "timed out after 1s and was killed"
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(scenario())


def test_systemd_runner_is_recorded_with_the_units_own_accounting(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _ensure_schema()
    stub = Stub(tmp_path, monkeypatch)
    stub.set(show=[LOADED, RAN_OK])

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = RunnerHarness(engine, host=stub.host)
        cid = await _register_runner(engine, {"kind": "systemd", "unit": "cache-prune.service"})
        try:
            cmd = _command(cid)
            msg = Msg(cmd.model_dump_json().encode())
            await harness.executor().handle_message(msg)
            assert msg.acked == 1
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "success"
            assert row.memory_peak_bytes == 52428800 and row.cpu_load == 0.5
            assert stub.calls("start") == [["--user", "start", "--wait", "cache-prune.service"]]
            assert await _counters(engine, cid) == (1, 1)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(scenario())


def test_a_failing_systemd_unit_is_recorded_failed_with_its_journal_tail(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _ensure_schema()
    stub = Stub(tmp_path, monkeypatch)
    failed = {**RAN_OK, "Result": "exit-code", "ExecMainStatus": "3", "ActiveState": "failed"}
    stub.set(show=[LOADED, failed], start_rc=1, journal="the unit said why\n")

    async def scenario() -> None:
        engine = create_registry_engine()
        harness = RunnerHarness(engine, host=stub.host)
        cid = await _register_runner(engine, {"kind": "systemd", "unit": "cache-prune.service"})
        try:
            cmd = _command(cid)
            await harness.executor().handle_message(Msg(cmd.model_dump_json().encode()))
            row = await harness.by_key(cmd.idempotency_key)
            assert row.result == "fail" and row.exit_code == 3
            assert row.error_message == "unit cache-prune.service failed: Result=exit-code, exit status 3"
            assert row.stderr_tail == "the unit said why"
            assert await _counters(engine, cid) == (1, 0)
        finally:
            await _drop(engine, cid)
            await engine.dispose()

    asyncio.run(scenario())
