"""Story 7.11 (AD-20): overlap skip and executor-backlog coalescing.

Overlap: an invoke for a candybar with a LIVE invocation is recorded
`skipped(reason=overlap)` and acked — in-process for every runner kind (the
single executor's live set), plus a unit-state probe for `systemd` runners so
a hand-started or pre-restart run also counts. Backlog: a `scheduled` command
whose fire is over BACKLOG_MAX_AGE old runs only as the latest non-skipped
fire (then with trigger `misfire`); an older one is recorded
`skipped(reason=executor_backlog_coalesced)` and acked, so commands queued
while the executor was down never burst-replay.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from candybar.dispatcher.schedule import NO_SKIPS, SkipSet
from candybar.events.envelope import CandybarSkippedEvent
from candybar.executor.worker import (
    SKIP_BACKLOG,
    SKIP_OVERLAP,
    Executor,
)
from test_executor import (
    CANDYBAR_ID,
    FakeBus,
    FakeMsg,
    FakeStore,
    make_command,
    make_outcome,
    run,
)

ONE_MINUTE = timedelta(minutes=1)


def tick_id_of(fire: datetime) -> str:
    return f"tick:{int(fire.timestamp()) // 60}"


def minute_floor(instant: datetime) -> datetime:
    return instant.astimezone(UTC).replace(second=0, microsecond=0)


def make(
    *,
    entrypoint: Any = "/bin/true",
    unit_busy: Any = None,
    schedule_tz: tuple[str, str] | None = None,
    skips: SkipSet = NO_SKIPS,
    outcome: Any = None,
) -> tuple[Executor, FakeStore, FakeBus, list[dict[str, Any]], list[tuple[Any, ...]]]:
    store, bus = FakeStore(), FakeBus()
    store.entrypoint = entrypoint
    runs: list[dict[str, Any]] = []
    skip_reads: list[tuple[Any, ...]] = []

    async def fake_run(**kwargs: Any):
        runs.append(kwargs)
        return outcome if outcome is not None else make_outcome()

    ports: dict[str, Any] = {}
    if unit_busy is not None:
        ports["unit_busy"] = unit_busy
    if schedule_tz is not None:

        async def fire_context(candybar_id: UUID) -> tuple[str, str] | None:
            if isinstance(schedule_tz, Exception):
                raise schedule_tz
            return schedule_tz

        async def skip_set(candybar_id: UUID, tz: Any, start: datetime, end: datetime) -> SkipSet:
            skip_reads.append((candybar_id, tz, start, end))
            if isinstance(skips, Exception):
                raise skips
            return skips

        ports["fire_context"] = fire_context
        ports["skip_set"] = skip_set

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
        **ports,
    )
    return executor, store, bus, runs, skip_reads


# ---------------------------------------------------------------------------
# Overlap (in-process live set; every runner kind)
# ---------------------------------------------------------------------------


def drive_overlap(second: dict[str, Any]) -> tuple[Any, ...]:
    """Two concurrent invokes of one candybar; the first runs long."""
    store, bus = FakeStore(), FakeBus()
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[dict[str, Any]] = []

    async def fake_run(**kwargs: Any):
        runs.append(kwargs)
        started.set()
        await release.wait()
        return make_outcome()

    executor = Executor(
        by_key=store.by_key,
        entrypoint=store.get_entrypoint,
        record=store.record,
        publish=bus.publish,
        run=fake_run,
    )

    async def scenario() -> tuple[FakeMsg, FakeMsg]:
        fire = minute_floor(datetime.now(UTC))
        cmd1 = make_command(tick_id=tick_id_of(fire))
        # The second invoke's tick only has to differ from the first's (a
        # shared key would take the in-flight dedupe path instead of overlap).
        cmd2 = make_command(**{"tick_id": tick_id_of(fire + 2 * ONE_MINUTE), **second})
        msg1 = FakeMsg(cmd1.model_dump_json().encode())
        msg2 = FakeMsg(cmd2.model_dump_json().encode())
        first = asyncio.create_task(executor.handle_message(msg1))
        await started.wait()
        await executor.handle_message(msg2)
        release.set()
        await first
        return msg1, msg2

    msg1, msg2 = asyncio.run(scenario())
    return store, bus, runs, msg1, msg2


def test_a_second_invoke_while_live_is_skipped_overlap_never_run() -> None:
    store, bus, runs, msg1, msg2 = drive_overlap({})

    assert len(runs) == 1  # only the first invocation ran
    skipped = [e for e in bus.events if isinstance(e, CandybarSkippedEvent)]
    assert len(skipped) == 1
    assert skipped[0].data.reason == SKIP_OVERLAP == "overlap"
    assert skipped[0].data.candybar_id == CANDYBAR_ID
    assert msg2.acked == 1 and not msg2.naks and not msg2.termed
    # The first run completed and recorded normally.
    assert msg1.acked == 1 and store.records[0].result == "success"


def test_a_manual_run_during_a_live_run_is_skipped_overlap() -> None:
    _store, bus, runs, _msg1, msg2 = drive_overlap({"trigger": "manual", "tick_id": None})
    assert len(runs) == 1
    skipped = [e for e in bus.events if isinstance(e, CandybarSkippedEvent)]
    assert len(skipped) == 1 and skipped[0].data.reason == SKIP_OVERLAP
    assert msg2.acked == 1


def test_the_overlap_slot_frees_after_the_run() -> None:
    executor, store, _bus, runs, _reads = make()
    fire = minute_floor(datetime.now(UTC))
    for minute in range(2):
        msg = FakeMsg(make_command(tick_id=tick_id_of(fire + minute * ONE_MINUTE)).model_dump_json().encode())
        run(executor.handle_message(msg))
        assert msg.acked == 1
    assert len(runs) == 2 and len(store.records) == 2


# ---------------------------------------------------------------------------
# Overlap (systemd unit probe: hand-started or pre-restart runs)
# ---------------------------------------------------------------------------


def systemd_executor(busy: Any):
    async def probe(unit: str) -> bool | None:
        if isinstance(busy, Exception):
            raise busy
        return busy

    return make(
        entrypoint={"kind": "systemd", "unit": "some-job.service"},
        unit_busy=probe,
    )


def test_a_busy_unit_is_skipped_overlap_never_started() -> None:
    executor, store, bus, runs, _reads = systemd_executor(True)
    cmd = make_command()
    msg = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    [fact] = bus.events
    assert isinstance(fact, CandybarSkippedEvent) and fact.data.reason == SKIP_OVERLAP
    assert msg.acked == 1
    # A redelivery republishes the same broker-dedupable fact.
    again = FakeMsg(cmd.model_dump_json().encode())
    run(executor.handle_message(again))
    assert bus.events[1].id == fact.id and again.acked == 1


def test_an_unanswerable_probe_naks_neither_skipping_nor_running() -> None:
    executor, _store, bus, runs, _reads = systemd_executor(None)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and msg.naks == [1] and msg.acked == 0


def test_a_crashed_probe_naks() -> None:
    executor, _store, bus, runs, _reads = systemd_executor(OSError("bus down"))
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and msg.naks == [1]


def test_an_idle_unit_runs() -> None:
    executor, store, _bus, runs, _reads = systemd_executor(False)
    msg = FakeMsg(make_command().model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and runs[0]["runner"] == {"kind": "systemd", "unit": "some-job.service"}
    assert store.records[0].result == "success"


# ---------------------------------------------------------------------------
# Executor-backlog coalescing
# ---------------------------------------------------------------------------


def backlog_ports(skips: SkipSet = NO_SKIPS, schedule_tz: tuple[str, str] = ("* * * * *", "UTC")):
    return make(schedule_tz=schedule_tz, skips=skips)


def test_a_stale_command_with_a_later_fire_is_coalesced_not_run() -> None:
    executor, store, bus, runs, skip_reads = backlog_ports()
    fire = minute_floor(datetime.now(UTC)) - 5 * ONE_MINUTE
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    [fact] = bus.events
    assert isinstance(fact, CandybarSkippedEvent)
    assert fact.data.reason == SKIP_BACKLOG == "executor_backlog_coalesced"
    assert fact.data.tick_id == tick_id_of(fire)
    assert msg.acked == 1 and not msg.naks
    assert len(skip_reads) == 1  # one windowed skip read, not one per stale minute


def test_the_latest_non_skipped_fire_survives_and_runs_as_misfire() -> None:
    now = minute_floor(datetime.now(UTC))
    fire = now - 5 * ONE_MINUTE
    # Every later minute is operator-skipped (cover a couple past `now` too,
    # so a minute roll mid-test cannot change the ruling).
    covered = frozenset(fire + n * ONE_MINUTE for n in range(1, 9))
    executor, store, bus, runs, _reads = backlog_ports(SkipSet(occurrences=covered))
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1
    assert runs[0]["trigger"] == "misfire" and runs[0]["tick_id"] == tick_id_of(fire)
    assert store.records[0].trigger == "misfire" and store.records[0].result == "success"
    kinds = [type(e).__name__ for e in bus.events]
    assert kinds == ["CandybarInvokedEvent", "CandybarCompletedEvent"]
    assert bus.events[0].data.trigger == "misfire"


def test_a_previous_minute_command_does_not_steal_the_current_minutes_run() -> None:
    # The live 17:37-17:41 outage drill reproduced this: an earlier-minute
    # command arrived at 17:41:00 (only 60s old, below BACKLOG_MAX_AGE), ran,
    # and overlap-skipped the actual 17:41 fire. The previous-minute command
    # must be coalesced even when not yet 2 minutes old.
    executor, store, bus, runs, _reads = backlog_ports()
    fire = minute_floor(datetime.now(UTC)) - ONE_MINUTE
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    [fact] = bus.events
    assert fact.data.reason == SKIP_BACKLOG and msg.acked == 1


def test_a_fresh_fire_runs_without_the_backlog_check() -> None:
    executor, store, _bus, runs, skip_reads = backlog_ports()
    fire = minute_floor(datetime.now(UTC))
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and runs[0]["trigger"] == "scheduled"
    assert store.records[0].trigger == "scheduled"
    assert skip_reads == []  # fresh fires never pay for the check


def test_a_misfire_command_is_not_re_coalesced() -> None:
    executor, _store, _bus, runs, skip_reads = backlog_ports()
    fire = minute_floor(datetime.now(UTC)) - 30 * ONE_MINUTE
    msg = FakeMsg(make_command(trigger="misfire", tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and runs[0]["trigger"] == "misfire"
    assert skip_reads == []


def test_an_own_fire_operator_skipped_after_dispatch_is_skipped_with_its_reason() -> None:
    now = minute_floor(datetime.now(UTC))
    fire = now - 5 * ONE_MINUTE
    # My fire AND every later fire up to now are skipped, so the latest
    # non-skipped fire is older than mine: mine was suppressed post-dispatch.
    covered = frozenset(fire + n * ONE_MINUTE for n in range(9))
    executor, store, bus, runs, _reads = backlog_ports(SkipSet(occurrences=covered))
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and store.records == []
    [fact] = bus.events
    assert fact.data.reason == "occurrence"  # the operator-skip kind, not backlog
    assert msg.acked == 1


def test_a_backlog_read_failure_naks_instead_of_guessing() -> None:
    executor, _store, bus, runs, _reads = make(
        schedule_tz=OSError("db down")  # type: ignore[arg-type]
    )
    fire = minute_floor(datetime.now(UTC)) - 5 * ONE_MINUTE
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == [] and bus.events == [] and msg.naks == [1] and msg.acked == 0


def test_an_unresolvable_stored_zone_reads_as_utc() -> None:
    executor, _store, bus, runs, _reads = backlog_ports(
        schedule_tz=("* * * * *", "Bogus/Zone")  # type: ignore[arg-type]
    )
    fire = minute_floor(datetime.now(UTC)) - 5 * ONE_MINUTE
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert runs == []
    [fact] = bus.events
    assert fact.data.reason == SKIP_BACKLOG and msg.acked == 1


def test_without_backlog_ports_a_stale_command_runs_as_before() -> None:
    executor, store, _bus, runs, _reads = make()  # ports unwired
    fire = minute_floor(datetime.now(UTC)) - 60 * ONE_MINUTE
    msg = FakeMsg(make_command(tick_id=tick_id_of(fire)).model_dump_json().encode())
    run(executor.handle_message(msg))
    assert len(runs) == 1 and store.records[0].trigger == "scheduled"
