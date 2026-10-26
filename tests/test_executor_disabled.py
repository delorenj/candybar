"""Story 7.5 (AD-21): the executor never runs a queued fire of a DISABLED candybar.

A migration rollback disables the candybar after restoring the native
scheduler. Scheduled or misfire commands already queued (say the executor
was down while the dispatcher kept publishing) must not run when the
executor returns, or the job fires on both schedulers. They are recorded as
a `skipped` fact with `reason=disabled` and acked; a manual run still runs.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest

from candybar.events.envelope import CandybarSkippedEvent
from candybar.executor.worker import SKIP_DISABLED, Executor
from test_executor import (
    CANDYBAR_ID,
    TICK_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    make_command,
    make_outcome,
    run,
)


def make(enabled: Any) -> tuple[Executor, FakeStore, FakeBus, list[dict[str, Any]], list[UUID]]:
    store, bus = FakeStore(), FakeBus()
    runs: list[dict[str, Any]] = []
    reads: list[UUID] = []

    async def fake_run(**kwargs: Any):
        runs.append(kwargs)
        return make_outcome()

    async def is_enabled(candybar_id: UUID) -> bool | None:
        reads.append(candybar_id)
        if isinstance(enabled, Exception):
            raise enabled
        return enabled

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
        enabled=is_enabled,
    )
    return executor, store, bus, runs, reads


@pytest.mark.parametrize("trigger", ["scheduled", "misfire"])
def test_queued_fire_of_a_disabled_candybar_is_skipped_not_run(trigger: str) -> None:
    executor, store, bus, runs, reads = make(False)
    cmd = make_command(trigger=trigger)
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    [fact] = bus.events
    assert isinstance(fact, CandybarSkippedEvent)
    assert fact.data.reason == SKIP_DISABLED == "disabled"
    assert fact.data.tick_id == TICK_ID and fact.data.candybar_id == CANDYBAR_ID
    assert fact.causationid == cmd.command_id
    assert msg.acked == 1 and not msg.naks and not msg.termed
    assert reads == [CANDYBAR_ID]
    # A redelivery republishes the same broker-dedupable fact.
    again = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(again))
    assert bus.events[1].id == fact.id and again.acked == 1


def test_enabled_candybar_runs() -> None:
    executor, store, _bus, runs, _ = make(True)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and len(store.records) == 1 and msg.acked == 1


def test_manual_run_of_a_disabled_candybar_still_runs() -> None:
    executor, _store, _bus, runs, reads = make(False)
    msg = FakeMsg(make_command(trigger="manual", tick_id=None).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and reads == []  # an explicit operator run is never second-guessed


def test_enabled_read_failure_naks_without_running() -> None:
    executor, _store, bus, runs, _ = make(OSError("db down"))
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and msg.naks == [1] and msg.acked == 0


def test_skipped_publish_failure_naks() -> None:
    executor, _store, bus, runs, _ = make(False)
    bus.fail_on = {0}
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and msg.naks == [1] and msg.acked == 0
