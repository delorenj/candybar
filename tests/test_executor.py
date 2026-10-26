"""Executor unit tests: runner protocol/cgroup, worker lifecycle, dedupe, matrix.

Runner tests spawn real (tiny) shell scripts with cgroup scoping disabled or
pointed at a tmpdir; worker tests use in-memory fakes for the bus and registry.
"""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from candybar.events import STDERR_TAIL_MAX, build_candybar_invoke
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarFailedEvent,
    CandybarInvokedEvent,
    InvocationMetrics,
)
from candybar.executor import runner
from candybar.executor.runner import (
    ProcessRegistry,
    ResultFile,
    RunOutcome,
    child_env,
    run_invocation,
)
from candybar.executor.worker import (
    Executor,
    lifecycle_event_id,
    record_from_row,
    resolve_outcome,
    terminal_event_for,
)
from candybar.registry.models import Invocation

CANDYBAR_ID = UUID("11111111-2222-3333-4444-555555555555")
INVOCATION_ID = UUID("66666666-7777-8888-9999-000000000000")
TICK_ID = "tick:29883000"


def run(coro):
    return asyncio.run(coro)


def make_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def invoke_kwargs(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "entrypoint": "/bin/true",
        "invocation_id": INVOCATION_ID,
        "candybar_id": CANDYBAR_ID,
        "trigger": "scheduled",
        "tick_id": TICK_ID,
        "root": "",  # no cgroup in unit tests unless the test says otherwise
    }
    kwargs.update(overrides)
    return kwargs


# ---------------------------------------------------------------------------
# Runner: AD-8 env contract and exit-code capture
# ---------------------------------------------------------------------------


def test_child_env_sets_all_five_vars(tmp_path: Path) -> None:
    env = child_env(
        invocation_id=INVOCATION_ID,
        candybar_id=CANDYBAR_ID,
        trigger="scheduled",
        tick_id=TICK_ID,
        result_file=tmp_path / "result.json",
    )
    assert env["CANDYBAR_INVOCATION_ID"] == str(INVOCATION_ID)
    assert env["CANDYBAR_ID"] == str(CANDYBAR_ID)
    assert env["CANDYBAR_TRIGGER"] == "scheduled"
    assert env["CANDYBAR_TICK_ID"] == TICK_ID
    assert env["CANDYBAR_RESULT_FILE"] == str(tmp_path / "result.json")
    assert env["PATH"] == os.environ["PATH"]  # parent env passes through


def test_child_env_empty_tick_for_manual(tmp_path: Path) -> None:
    env = child_env(
        invocation_id=INVOCATION_ID,
        candybar_id=CANDYBAR_ID,
        trigger="manual",
        tick_id=None,
        result_file=tmp_path / "r.json",
    )
    assert env["CANDYBAR_TICK_ID"] == ""


def test_bare_success_no_result_file(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    capture = tmp_path / "capture"
    entrypoint = make_script(
        tmp_path,
        "ok.sh",
        f'{{ printenv CANDYBAR_INVOCATION_ID; printenv CANDYBAR_ID; printenv CANDYBAR_TRIGGER; '
        f'printenv CANDYBAR_TICK_ID; printenv CANDYBAR_RESULT_FILE; pwd; }} > "{capture}"\nexit 0',
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.exit_code == 0
    assert outcome.result is None and outcome.result_error is None
    assert outcome.spawn_error is None
    lines = capture.read_text().splitlines()
    assert lines[0] == str(INVOCATION_ID)
    assert lines[1] == str(CANDYBAR_ID)
    assert lines[2] == "scheduled"
    assert lines[3] == TICK_ID
    # The result file lives in a fresh per-invocation work dir.
    assert Path(lines[4]).name == "result.json"
    assert lines[4].startswith(str(tmp_path / "work"))
    assert not Path(lines[4]).exists()  # cleaned up after the run
    assert lines[5] == str(tmp_path)  # cwd is the entrypoint's directory


def test_bare_failure_captures_stderr_tail(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(
        tmp_path, "fail.sh", "printf '%0.sA' $(seq 600) >&2; echo TAIL-MARKER >&2; exit 3"
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.exit_code == 3
    assert len(outcome.stderr_tail) == STDERR_TAIL_MAX
    assert outcome.stderr_tail.endswith("TAIL-MARKER\n")
    assert outcome.result is None


def test_result_file_success_with_metrics(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(
        tmp_path,
        "sdk.sh",
        'printf \'{"status":"success","metrics":{"token_spend":5}}\' > "$CANDYBAR_RESULT_FILE"\nexit 0',
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.exit_code == 0
    assert outcome.result is not None
    assert outcome.result.status == "success"
    assert outcome.result.metrics.token_spend == 5
    assert outcome.result.metrics.cost_usd is None


def test_result_file_unknown_keys_ignored(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(
        tmp_path,
        "fwd.sh",
        'printf \'{"status":"success","future_field":123}\' > "$CANDYBAR_RESULT_FILE"\nexit 0',
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.result is not None and outcome.result.status == "success"
    assert outcome.result_error is None


def test_invalid_result_file_flagged(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(
        tmp_path, "bad.sh", 'echo "not json" > "$CANDYBAR_RESULT_FILE"\nexit 0'
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.exit_code == 0
    assert outcome.result is None
    assert outcome.result_error is not None and "invalid result file" in outcome.result_error
    assert len(outcome.result_error) <= STDERR_TAIL_MAX


def test_wrong_shape_result_file_flagged(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(
        tmp_path, "bad2.sh", 'printf \'{"status":"maybe"}\' > "$CANDYBAR_RESULT_FILE"\nexit 0'
    )
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.result is None and outcome.result_error is not None


def test_spawn_failure_is_an_outcome_not_an_exception(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=str(tmp_path / "missing.sh"))))
    assert outcome.exit_code is None
    assert outcome.spawn_error is not None


def test_runtime_and_timestamps_recorded(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(tmp_path, "slow.sh", "sleep 0.05\nexit 0")
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint)))
    assert outcome.runtime_ms >= 40
    assert outcome.started_at.tzinfo is not None and outcome.finished_at >= outcome.started_at


# ---------------------------------------------------------------------------
# Runner: cgroup v2 scope mechanics
# ---------------------------------------------------------------------------


def test_cgroup_scope_created_and_removed(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    root = tmp_path / "cg"
    entrypoint = make_script(tmp_path, "scoped.sh", "exit 0")
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, root=str(root))))
    assert outcome.scoped is True
    assert not (root / str(INVOCATION_ID)).exists()  # scope removed after exit


def test_cgroup_degrades_when_root_unwritable(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("plain file")  # mkdir under a file fails
    entrypoint = make_script(tmp_path, "plain.sh", "exit 0")
    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, root=str(blocker))))
    assert outcome.scoped is False
    assert outcome.exit_code == 0  # execution never fails on cgroup privilege


def test_on_wait_hook_fires_during_long_runs(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setattr(runner, "WAIT_SLICE_SECONDS", 0.05)
    entrypoint = make_script(tmp_path, "long.sh", "sleep 0.3\nexit 0")
    waits = 0

    async def on_wait() -> None:
        nonlocal waits
        waits += 1

    outcome = run(run_invocation(**invoke_kwargs(entrypoint=entrypoint, on_wait=on_wait)))
    assert outcome.exit_code == 0
    assert waits >= 1


def test_terminate_all_sigterms_running_child(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(tmp_path, "forever.sh", "sleep 30\nexit 0")
    registry = ProcessRegistry()

    async def scenario() -> RunOutcome:
        task = asyncio.create_task(
            run_invocation(**invoke_kwargs(entrypoint=entrypoint, registry=registry))
        )
        for _ in range(500):
            if registry.procs:
                break
            await asyncio.sleep(0.01)
        assert registry.procs, "child never registered"
        await registry.terminate_all()
        return await asyncio.wait_for(task, timeout=10)

    outcome = run(scenario())
    assert registry.stopping is True
    assert outcome.exit_code not in (0, None)


# ---------------------------------------------------------------------------
# Result resolution precedence
# ---------------------------------------------------------------------------


def make_outcome(**overrides: Any) -> RunOutcome:
    now = datetime.now(UTC)
    base: dict[str, Any] = {
        "exit_code": 0,
        "runtime_ms": 12,
        "stderr_tail": "",
        "result": None,
        "result_error": None,
        "spawn_error": None,
        "scoped": False,
        "started_at": now,
        "finished_at": now,
    }
    base.update(overrides)
    return RunOutcome(**base)


def test_resolve_bare_success() -> None:
    resolved = resolve_outcome(make_outcome())
    assert resolved.success and resolved.metrics.token_spend == 0


def test_resolve_nonzero_exit_fails_even_with_success_file() -> None:
    resolved = resolve_outcome(
        make_outcome(exit_code=1, result=ResultFile(status="success"))
    )
    assert not resolved.success and resolved.message == "exit code 1"


def test_resolve_sdk_fail_on_zero_exit() -> None:
    resolved = resolve_outcome(
        make_outcome(result=ResultFile(status="fail", message="boom"))
    )
    assert not resolved.success and resolved.message == "boom"


def test_resolve_invalid_result_fails() -> None:
    resolved = resolve_outcome(make_outcome(result_error="invalid result file: x"))
    assert not resolved.success and "invalid result file" in (resolved.message or "")


def test_resolve_spawn_failure() -> None:
    resolved = resolve_outcome(make_outcome(exit_code=None, spawn_error="PermissionError"))
    assert not resolved.success and "spawn failed" in (resolved.message or "")


def test_resolve_shutdown_message() -> None:
    resolved = resolve_outcome(make_outcome(exit_code=-15), stopped=True)
    assert not resolved.success and resolved.message == "executor stopped (SIGTERM)"


def test_resolve_messages_capped_at_500() -> None:
    resolved = resolve_outcome(make_outcome(exit_code=None, spawn_error="x" * 900))
    assert resolved.message is not None and len(resolved.message) <= STDERR_TAIL_MAX


# ---------------------------------------------------------------------------
# Worker: full command lifecycle
# ---------------------------------------------------------------------------


class FakeMsg:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.acked = 0
        self.naks: list[float] = []
        self.termed = 0
        self.in_progress_calls = 0

    async def ack(self) -> None:
        self.acked += 1

    async def nak(self, delay: float | None = None) -> None:
        self.naks.append(delay or 0)

    async def term(self) -> None:
        self.termed += 1

    async def in_progress(self) -> None:
        self.in_progress_calls += 1


def make_command(**overrides: Any):
    kwargs: dict[str, Any] = {
        "candybar_id": CANDYBAR_ID,
        "name": "unit-job",
        "trigger": "scheduled",
        "tick_id": TICK_ID,
        "invocation_id": INVOCATION_ID,
        "command_id": UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    }
    kwargs.update(overrides)
    return build_candybar_invoke(**kwargs)


class FakeBus:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.events: list[Any] = []
        self.fail_on = fail_on or set()

    async def publish(self, event: Any) -> None:
        if len(self.events) in self.fail_on:
            raise OSError("bus down")
        self.events.append(event)


class FakeStore:
    def __init__(self, entrypoint: str | None = "/bin/true") -> None:
        self.rows: dict[str, Invocation] = {}
        self.records: list[Any] = []
        self.entrypoint = entrypoint
        self.record_result = True

    async def by_key(self, key: str) -> Invocation | None:
        return self.rows.get(key)

    async def get_entrypoint(self, candybar_id: UUID) -> str | None:
        return self.entrypoint

    async def record(self, rec: Any) -> bool:
        self.records.append(rec)
        return self.record_result


def make_row(key: str, result: str = "success", **overrides: Any) -> Invocation:
    now = datetime.now(UTC)
    fields: dict[str, Any] = {
        "id": INVOCATION_ID,
        "candybar_id": CANDYBAR_ID,
        "trigger": "scheduled",
        "tick_id": TICK_ID,
        "idempotency_key": key,
        "result": result,
        "exit_code": 0 if result == "success" else 1,
        "runtime_ms": 12,
        "error_message": None if result == "success" else "exit code 1",
        "stderr_tail": None,
        "token_spend": 0,
        "cost_usd": None,
        "started_at": now,
        "finished_at": now,
        "created_at": now,
    }
    fields.update(overrides)
    return Invocation(**fields)


def make_executor(
    store: FakeStore,
    bus: FakeBus,
    outcome: RunOutcome | None = None,
    registry: ProcessRegistry | None = None,
) -> tuple[Executor, list[dict[str, Any]]]:
    runs: list[dict[str, Any]] = []

    async def fake_run(**kwargs: Any) -> RunOutcome:
        runs.append(kwargs)
        return outcome if outcome is not None else make_outcome()

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
        registry=registry,
    )
    return executor, runs


def test_scheduled_happy_path() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    cmd = make_command()
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))

    assert len(runs) == 1
    assert runs[0]["entrypoint"] == "/bin/true"
    assert runs[0]["tick_id"] == TICK_ID

    invoked, completed = bus.events
    assert isinstance(invoked, CandybarInvokedEvent)
    assert isinstance(completed, CandybarCompletedEvent)
    assert invoked.id == lifecycle_event_id("invoked", INVOCATION_ID)
    assert completed.id == lifecycle_event_id("success", INVOCATION_ID)
    for event in (invoked, completed):
        assert event.correlationid == cmd.correlationid == cmd.command_id
        assert event.causationid == cmd.command_id
        assert event.ordering_key == f"candybar:{CANDYBAR_ID}"
    assert completed.data.exit_code == 0
    assert completed.data.metrics.token_spend == 0

    assert len(store.records) == 1
    rec = store.records[0]
    assert rec.result == "success" and rec.exit_code == 0
    assert rec.idempotency_key == f"candybar:{CANDYBAR_ID}:tick:{TICK_ID}"
    assert rec.token_spend == 0 and rec.cost_usd is None
    assert msg.acked == 1 and not msg.naks and not msg.termed


def test_failure_path_emits_failed_and_records() -> None:
    store, bus = FakeStore(), FakeBus()
    outcome = make_outcome(exit_code=3, stderr_tail="boom")
    executor, _runs = make_executor(store, bus, outcome)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))

    assert isinstance(bus.events[1], CandybarFailedEvent)
    failed = bus.events[1]
    assert failed.id == lifecycle_event_id("fail", INVOCATION_ID)
    assert failed.data.exit_code == 3
    assert failed.data.error.stderr_tail == "boom"
    assert failed.data.error.redacted is True
    assert store.records[0].result == "fail"
    assert msg.acked == 1


def test_manual_trigger_passes_empty_tick() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    cmd = make_command(trigger="manual", tick_id=None)
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs[0]["tick_id"] is None and runs[0]["trigger"] == "manual"
    assert store.records[0].trigger == "manual"
    assert store.records[0].idempotency_key.startswith(f"candybar:{CANDYBAR_ID}:manual:")


def test_sdk_metrics_land_on_event_and_row() -> None:
    store, bus = FakeStore(), FakeBus()
    outcome = make_outcome(
        result=ResultFile(status="success", metrics=InvocationMetrics(token_spend=42))
    )
    executor, _runs = make_executor(store, bus, outcome)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert bus.events[1].data.metrics.token_spend == 42
    assert store.records[0].token_spend == 42


def test_redelivery_never_reexecutes_and_republishes() -> None:
    store, bus = FakeStore(), FakeBus()
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key, result="fail", exit_code=7)
    executor, runs = make_executor(store, bus)
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))

    assert runs == [] and store.records == []
    assert len(bus.events) == 1
    republished = bus.events[0]
    assert isinstance(republished, CandybarFailedEvent)
    assert republished.id == lifecycle_event_id("fail", INVOCATION_ID)
    assert republished.data.exit_code == 7
    assert msg.acked == 1


def test_redelivery_republish_failure_naks() -> None:
    store, bus = FakeStore(), FakeBus(fail_on={0})
    cmd = make_command()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key)
    executor, _runs = make_executor(store, bus)
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert msg.naks == [1] and msg.acked == 0


def test_inflight_duplicate_naks_without_executing() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    cmd = make_command()
    executor._inflight.add(cmd.idempotency_key)
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and msg.naks == [5]


def test_duplicate_record_race_republishes_winner() -> None:
    store, bus = FakeStore(), FakeBus()
    cmd = make_command()
    store.record_result = False  # simulate losing the unique-key race
    runs: list[dict[str, Any]] = []

    async def by_key_after_race(key: str) -> Invocation | None:
        if store.records:  # the winner's row exists once we tried to record
            return make_row(key)
        return None

    async def fake_run(**kwargs: Any) -> RunOutcome:
        runs.append(kwargs)
        return make_outcome()

    executor = Executor(
        by_key=by_key_after_race,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
    )
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1  # executed once, then lost the record race
    assert isinstance(bus.events[-1], CandybarCompletedEvent)
    assert msg.acked == 1


def test_unknown_candybar_fails_and_terms() -> None:
    store, bus = FakeStore(entrypoint=None), FakeBus()
    executor, runs = make_executor(store, bus)
    cmd = make_command()
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    assert isinstance(bus.events[0], CandybarFailedEvent)
    assert bus.events[0].data.exit_code is None
    assert "unknown candybar" in (bus.events[0].data.error.message or "")
    assert msg.termed == 1


def test_poison_payloads_term_without_execution() -> None:
    store, bus = FakeStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    garbage = FakeMsg(b"this is not json")
    run(executor.handle_message(garbage))
    assert garbage.termed == 1

    from candybar.events import build_tick_fired

    tick = build_tick_fired(datetime(2026, 10, 25, 12, 35, tzinfo=UTC))
    wrong_type = FakeMsg(tick.model_dump_json().encode())
    run(executor.handle_message(wrong_type))
    assert wrong_type.termed == 1
    assert runs == [] and bus.events == []


def test_invoked_publish_failure_naks_before_running() -> None:
    store, bus = FakeStore(), FakeBus(fail_on={0})
    executor, runs = make_executor(store, bus)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    assert msg.naks == [1] and msg.acked == 0


def test_dedupe_read_failure_naks_without_execution() -> None:
    store, bus = FakeStore(), FakeBus()

    async def broken_by_key(key: str) -> Invocation | None:
        raise OSError("db down")

    executor, runs = make_executor(store, bus)
    executor._by_key = broken_by_key  # type: ignore[method-assign]
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and msg.naks == [1] and msg.acked == 0


def test_entrypoint_lookup_failure_naks() -> None:
    store, bus = FakeStore(), FakeBus()

    async def broken_entrypoint(candybar_id: UUID) -> str | None:
        raise OSError("db down")

    executor, runs = make_executor(store, bus)
    executor._entrypoint = broken_entrypoint  # type: ignore[method-assign]
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and msg.naks == [1]


def test_run_crash_naks_and_clears_inflight() -> None:
    store, bus = FakeStore(), FakeBus()

    async def crashing_run(**kwargs: Any) -> RunOutcome:
        raise RuntimeError("work dir unwritable")

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=crashing_run,
    )
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert store.records == [] and msg.naks == [1] and msg.acked == 0
    assert executor._inflight == set()  # the key is free for the redelivery


def test_record_failure_naks() -> None:
    store, bus = FakeStore(), FakeBus()

    async def broken_record(rec: Any) -> bool:
        raise KeyError("candybar deleted mid-run")

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=broken_record,
        publish=bus.publish,
        run=(lambda **kwargs: _ok(kwargs)),
    )
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert msg.naks == [1] and msg.acked == 0


async def _ok(kwargs: Any) -> RunOutcome:
    return make_outcome()


def test_terminal_publish_failure_naks_but_row_is_committed() -> None:
    store, bus = FakeStore(), FakeBus(fail_on={1})
    executor, runs = make_executor(store, bus)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and len(store.records) == 1
    assert msg.naks == [1] and msg.acked == 0


def test_shutdown_marks_running_child_failed() -> None:
    store, bus = FakeStore(), FakeBus()
    registry = ProcessRegistry()

    async def run_until_stopped(**kwargs: Any) -> RunOutcome:
        # Stop arrives while the child runs; terminate_all SIGTERMs it.
        await registry.terminate_all()
        return make_outcome(exit_code=-15)

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=run_until_stopped,
        registry=registry,
    )
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert store.records[0].result == "fail"
    assert store.records[0].error_message == "executor stopped (SIGTERM)"
    assert msg.acked == 1


def test_command_handled_after_stop_began_is_returned_unexecuted() -> None:
    """A command the role fetched in its last poll (or still deduping) at stop.

    The role's finally runs terminate_all BEFORE such a task first runs, so a
    child it spawned would never be signalled and would outlive docker's stop
    grace. It must not run at all: no spawn, no invoked fact, no row — nak'd
    back to the stream for the next executor.
    """
    store, bus = FakeStore(), FakeBus()
    registry = ProcessRegistry()
    run(registry.terminate_all())  # stop already began
    executor, runs = make_executor(store, bus, registry=registry)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and store.records == []
    assert msg.naks == [0] and msg.acked == 0 and msg.termed == 0
    assert executor._inflight == set()


def test_child_spawned_after_terminate_all_is_sigtermed_on_arrival(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """The spawn raced shutdown: terminate_all swept before the child existed."""
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    entrypoint = make_script(tmp_path, "forever.sh", "sleep 30\nexit 0")
    registry = ProcessRegistry()

    async def scenario() -> RunOutcome:
        await registry.terminate_all()  # nothing registered yet
        return await asyncio.wait_for(
            run_invocation(**invoke_kwargs(entrypoint=entrypoint, registry=registry)),
            timeout=10,
        )

    outcome = run(scenario())
    assert outcome.exit_code not in (0, None)
    assert outcome.runtime_ms < 10_000
    assert not registry.procs


def test_terminal_event_id_is_deterministic() -> None:
    cmd = make_command()
    rec = record_from_row(make_row(cmd.idempotency_key))
    first = terminal_event_for(cmd, rec)
    second = terminal_event_for(cmd, rec)
    assert first.id == second.id
    fail_rec = record_from_row(make_row(cmd.idempotency_key, result="fail", exit_code=1))
    assert terminal_event_for(cmd, fail_rec).id != first.id


# ---------------------------------------------------------------------------
# Story 2.1: the in-process claim is taken before the first await
# ---------------------------------------------------------------------------


class YieldingStore(FakeStore):
    """A registry whose reads yield to the loop, like a real DB round-trip.

    `record` lands the row so a later redelivery sees it — the same shape as
    the live registry.
    """

    async def by_key(self, key: str) -> Invocation | None:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return self.rows.get(key)

    async def get_entrypoint(self, candybar_id: UUID) -> str | None:
        await asyncio.sleep(0)
        return self.entrypoint

    async def record(self, rec: Any) -> bool:
        self.records.append(rec)
        self.rows[rec.idempotency_key] = make_row(rec.idempotency_key)
        return True


def test_concurrent_same_key_deliveries_execute_once() -> None:
    """Two deliveries of one key in flight together: one spawn, the other naks.

    Before the fix the claim was taken just before spawn, after three awaits;
    both deliveries passed the dedupe read and the job ran twice.
    """
    store, bus = YieldingStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    cmd = make_command()
    first = FakeMsg(cmd.model_dump_json().encode())
    second = FakeMsg(cmd.model_dump_json().encode())

    async def both() -> None:
        await asyncio.gather(executor.handle_message(first), executor.handle_message(second))

    run(both())
    assert len(runs) == 1
    assert len(store.records) == 1
    assert first.acked == 1 and second.naks == [5] and second.acked == 0
    assert executor._inflight == set()

    # The nak'd delivery comes back after the row landed: dedupe, never re-run.
    again = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(again))
    assert len(runs) == 1 and again.acked == 1


def test_claim_released_on_every_early_exit() -> None:
    """Dedupe-read failure, unknown candybar, and the redelivery republish
    path all return before spawn; none may leak the claim."""
    cmd = make_command()

    class BrokenRead(FakeStore):
        async def by_key(self, key: str) -> Invocation | None:
            raise OSError("db down")

    for store in (BrokenRead(), FakeStore(entrypoint=None)):
        executor, runs = make_executor(store, FakeBus())
        run(executor.handle_message(FakeMsg(cmd.model_dump_json().encode())))
        assert runs == [] and executor._inflight == set()

    store = FakeStore()
    store.rows[cmd.idempotency_key] = make_row(cmd.idempotency_key)
    executor, runs = make_executor(store, FakeBus())
    run(executor.handle_message(FakeMsg(cmd.model_dump_json().encode())))
    assert runs == [] and executor._inflight == set()


def test_distinct_manual_uuids_execute_separately() -> None:
    """The manual dedupe key is per command UUID: two invokes, two runs."""
    store, bus = YieldingStore(), FakeBus()
    executor, runs = make_executor(store, bus)
    for n in (1, 2):
        cmd = make_command(
            trigger="manual",
            tick_id=None,
            command_id=UUID(f"aaaaaaaa-bbbb-cccc-dddd-00000000000{n}"),
            invocation_id=UUID(f"66666666-7777-8888-9999-00000000000{n}"),
        )
        assert cmd.idempotency_key == f"candybar:{CANDYBAR_ID}:manual:{cmd.command_id}"
        run(executor.handle_message(FakeMsg(cmd.model_dump_json().encode())))
    assert len(runs) == 2
    assert len({rec.idempotency_key for rec in store.records}) == 2
