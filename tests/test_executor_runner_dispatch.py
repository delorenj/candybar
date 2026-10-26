"""Story 7.3 (AD-18): the worker's side of runner kinds.

What the executor hands its `run` (the bare entrypoint or the stored runner
dict) and when the `on_failure` hook fires: once, after a FRESH recorded
failure, never for a success, a redelivery, an abandoned run or a shutdown.
"""

from __future__ import annotations

import asyncio
from typing import Any

from test_executor import (
    CANDYBAR_ID,
    INVOCATION_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    make_command,
    make_outcome,
    make_row,
)

from candybar.executor.runner import ProcessRegistry
from candybar.executor.worker import Executor

EXEC_RUNNER = {"kind": "exec", "argv": ["/usr/bin/job"], "env": {}, "on_failure": ["/usr/bin/page"]}
SYSTEMD_RUNNER = {"kind": "systemd", "unit": "cache-prune.service"}


def go(coro: Any) -> Any:
    return asyncio.run(coro)


class Rig:
    def __init__(self, target: Any, outcome: Any | None = None, *, registry: Any = None) -> None:
        self.store = FakeStore(entrypoint=target)
        self.bus = FakeBus()
        self.runs: list[dict[str, Any]] = []
        self.hooks: list[tuple[dict, dict[str, Any]]] = []

        async def fake_run(**kwargs: Any) -> Any:
            self.runs.append(kwargs)
            return outcome if outcome is not None else make_outcome()

        async def fake_hook(runner: dict, **kwargs: Any) -> None:
            self.hooks.append((runner, kwargs))

        self.executor = Executor(
            by_key=self.store.by_key,
            entrypoint=self.store.get_entrypoint,
            record=self.store.record,
            publish=self.bus.publish,
            run=fake_run,
            registry=registry,
            on_failure=fake_hook,
        )

    def deliver(self) -> FakeMsg:
        msg = FakeMsg(make_command().model_dump_json().encode())
        go(self.executor.handle_message(msg))
        return msg


def test_a_bare_entrypoint_is_handed_to_run_as_entrypoint() -> None:
    rig = Rig("/opt/jobs/run.sh")
    rig.deliver()
    assert rig.runs[0]["entrypoint"] == "/opt/jobs/run.sh" and "runner" not in rig.runs[0]


def test_a_stored_runner_is_handed_to_run_as_runner() -> None:
    rig = Rig(SYSTEMD_RUNNER)
    rig.deliver()
    assert rig.runs[0]["runner"] == SYSTEMD_RUNNER and "entrypoint" not in rig.runs[0]
    assert rig.runs[0]["candybar_id"] == CANDYBAR_ID


def test_on_failure_fires_once_after_a_recorded_failure_with_the_message() -> None:
    rig = Rig(EXEC_RUNNER, make_outcome(exit_code=2))
    msg = rig.deliver()
    assert msg.acked == 1
    ((runner, kwargs),) = rig.hooks
    assert runner == EXEC_RUNNER
    assert kwargs["message"] == "exit code 2"
    assert kwargs["invocation_id"] == INVOCATION_ID and kwargs["candybar_id"] == CANDYBAR_ID
    assert kwargs["trigger"] == "scheduled"
    # The failure was recorded BEFORE the hook could matter.
    assert rig.store.records[0].result == "fail"


def test_on_failure_does_not_fire_for_a_success() -> None:
    rig = Rig(EXEC_RUNNER, make_outcome(exit_code=0))
    rig.deliver()
    assert rig.hooks == []


def test_on_failure_does_not_fire_for_a_bare_entrypoint() -> None:
    rig = Rig("/opt/jobs/run.sh", make_outcome(exit_code=1))
    rig.deliver()
    assert rig.hooks == []


def test_on_failure_does_not_fire_for_a_redelivered_recorded_failure() -> None:
    rig = Rig(EXEC_RUNNER, make_outcome(exit_code=1))
    rig.deliver()
    assert len(rig.hooks) == 1
    key = rig.store.records[0].idempotency_key
    rig.store.rows[key] = make_row(key, result="fail")
    rig.deliver()
    assert len(rig.hooks) == 1 and len(rig.runs) == 1  # no re-run, no second page


def test_on_failure_does_not_fire_when_the_failure_was_the_executors_own_shutdown() -> None:
    registry = ProcessRegistry()
    registry.stopping = True
    rig = Rig(EXEC_RUNNER, make_outcome(exit_code=-15), registry=registry)
    msg = rig.deliver()
    # Stopping before the spawn returns the command to the stream, unexecuted.
    assert rig.hooks == [] and msg.naks and not rig.runs


def test_a_lost_run_recorded_abandoned_does_not_page() -> None:
    rig = Rig(EXEC_RUNNER)
    key = f"candybar:{CANDYBAR_ID}:tick:tick:29883000"
    rig.store.rows[key] = make_row(key, result=None)  # claimed, never finished
    rig.deliver()
    assert rig.hooks == [] and rig.runs == []


def test_a_hook_that_raises_cannot_fail_the_invocation() -> None:
    rig = Rig(EXEC_RUNNER, make_outcome(exit_code=1))

    async def exploding(runner: dict, **kwargs: Any) -> None:
        raise RuntimeError("hook blew up")

    rig.executor._on_failure = exploding
    msg = rig.deliver()
    # Recorded, published, acked: a faulty hook never strands the command.
    assert msg.acked == 1 and not msg.naks
    assert len(rig.store.records) == 1 and len(rig.bus.events) == 2
