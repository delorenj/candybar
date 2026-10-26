"""Live proof: a real command off JetStream runs a real script and lands in Postgres.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL).
Uses isolated test streams on the bus (never production subjects/durables) and
throwaway registry rows (deleted on teardown); the command/event envelopes are
the exact production payloads.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from pathlib import Path
from uuid import UUID, uuid4

import nats
import pytest
from nats.js.api import (
    AckPolicy,
    ConsumerConfig,
    DeliverPolicy,
    RetentionPolicy,
    StreamConfig,
)
from nats.js.errors import NotFoundError
from prometheus_client import CollectorRegistry
from sqlalchemy import select

from candybar.events import build_candybar_invoke, parse_envelope
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarFailedEvent,
    CandybarInvokedEvent,
)
from candybar.executor import ExecutorMetrics, ProcessRegistry, run_invocation
from candybar.executor.worker import Executor
from candybar.registry import (
    CandybarManifest,
    DuplicateInvocationError,
    create_registry_engine,
    get_entrypoint,
    insert_candybar,
    invocation_by_key,
    record_terminal_invocation,
    session_scope,
)
from candybar.registry.models import Candybar, Invocation
from candybar.registry.store import TerminalInvocation

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)

OK_SCRIPT = """#!/bin/sh
echo "hello from $CANDYBAR_INVOCATION_ID"
exit 0
"""

FAIL_SCRIPT = """#!/bin/sh
echo "something broke" >&2
exit 3
"""


def _make_script(tmp_path: Path, name: str, body: str) -> str:
    script = tmp_path / name
    script.write_text(body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_schema() -> None:
    """The throwaway DATABASE_URL may be empty (CI) or just-downgraded (local
    after the registry suite ran): upgrade to head before touching rows."""
    from alembic import command
    from alembic.config import Config

    from candybar.registry import normalize_dsn

    cfg = Config(str(REPO_ROOT / "deploy" / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url",
        normalize_dsn(os.environ.get("DATABASE_URL", "postgresql://localhost:5432/candybar_test")),
    )
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "head")


def test_command_to_terminal_row_end_to_end(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()

    async def scenario() -> None:
        monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
        monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        commands_stream = f"CANDYBAR_TEST_EXEC_CMD_{suffix.upper()}"
        events_stream = f"CANDYBAR_TEST_EXEC_EVT_{suffix.upper()}"
        command_subject = f"candybar.test.{suffix}.invoke"
        event_subject = f"candybar.test.{suffix}.events"
        durable = f"candybar-test-exec-{suffix}"
        engine = create_registry_engine()
        ok_id, fail_id = uuid4(), uuid4()
        ok_entry = _make_script(tmp_path, "ok.sh", OK_SCRIPT)
        fail_entry = _make_script(tmp_path, "fail.sh", FAIL_SCRIPT)
        spawns = 0

        try:
            # Register two candybars through the real registry write path.
            for cid, name, entry in (
                (ok_id, f"itest-ok-{suffix}", ok_entry),
                (fail_id, f"itest-fail-{suffix}", fail_entry),
            ):
                manifest = CandybarManifest.model_validate(
                    {"name": name, "schedule": "* * * * *", "entrypoint": entry}
                )
                async with session_scope(engine) as session:
                    await insert_candybar(session, manifest, candybar_id=cid)

            await js.add_stream(StreamConfig(name=commands_stream, subjects=[command_subject]))
            await js.add_stream(StreamConfig(
                name=events_stream, subjects=[event_subject], retention=RetentionPolicy.LIMITS
            ))
            sub = await js.pull_subscribe(
                command_subject, durable=durable, stream=commands_stream,
                config=ConsumerConfig(
                    durable_name=durable, filter_subject=command_subject,
                    deliver_policy=DeliverPolicy.NEW, ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=30,
                ),
            )

            async def by_key(key: str):
                async with session_scope(engine) as session:
                    return await invocation_by_key(session, key)

            async def entrypoint(candybar_id):
                async with session_scope(engine) as session:
                    return await get_entrypoint(session, candybar_id)

            async def record(rec: TerminalInvocation) -> bool:
                try:
                    async with session_scope(engine) as session:
                        await record_terminal_invocation(session, rec)
                    return True
                except DuplicateInvocationError:
                    return False

            async def publish(event) -> None:
                await js.publish(
                    event_subject,
                    event.model_dump_json().encode(),
                    headers={"Nats-Msg-Id": str(event.id)},
                )

            async def counting_run(**kwargs):
                nonlocal spawns
                spawns += 1
                return await run_invocation(**kwargs)

            metrics = ExecutorMetrics(CollectorRegistry())
            executor = Executor(
                by_key=by_key,
                entrypoint=entrypoint,
                record=record,
                publish=publish,
                run=counting_run,
                registry=ProcessRegistry(),
                metrics=metrics,
            )

            def metric(name: str, cid: UUID, result: str) -> float:
                value = metrics.registry.get_sample_value(
                    name, {"candybar_id": str(cid), "result": result}
                )
                return value or 0.0

            ok_command = build_candybar_invoke(
                candybar_id=ok_id,
                name=f"itest-ok-{suffix}",
                trigger="scheduled",
                tick_id="tick:29883000",
            )
            await js.publish(
                command_subject,
                ok_command.model_dump_json().encode(),
                headers={"Nats-Msg-Id": str(ok_command.id)},
            )
            first = (await sub.fetch(1, timeout=5))[0]
            await executor.handle_message(first)

            # Terminal row + counters landed.
            async with session_scope(engine) as session:
                row = await invocation_by_key(session, ok_command.idempotency_key)
                assert row is not None
                assert row.result == "success" and row.exit_code == 0
                assert row.runtime_ms is not None and row.runtime_ms >= 0
                # Unscoped in this test: measures persist as the spine's zero.
                assert row.memory_peak_bytes == 0 and row.cpu_load == 0.0
                candybar = await session.get(Candybar, ok_id)
                assert candybar.total_invoked == 1 and candybar.total_successful == 1

            # invoked + completed on the wire.
            info = await js.stream_info(events_stream)
            assert info.state.messages == 2
            seen = []
            for seq in (1, 2):
                fetched = await js.get_msg(events_stream, seq=seq)
                seen.append(parse_envelope(json.loads(fetched.data)))
            assert isinstance(seen[0], CandybarInvokedEvent)
            assert isinstance(seen[1], CandybarCompletedEvent)

            # Redelivery: no second execution; republish dedupes on the bus.
            await js.publish(command_subject, ok_command.model_dump_json().encode())
            second = (await sub.fetch(1, timeout=5))[0]
            await executor.handle_message(second)
            assert spawns == 1
            info = await js.stream_info(events_stream)
            assert info.state.messages == 2  # same deterministic id -> deduped
            async with session_scope(engine) as session:
                count = await session.execute(
                    select(Invocation).where(
                        Invocation.idempotency_key == ok_command.idempotency_key
                    )
                )
                assert len(count.scalars().all()) == 1
            # Story 1.7: metrics agree with the registry counters — the
            # redelivery did not recount; runtime observed once, from the row.
            assert metric("candybar_invocations_total", ok_id, "success") == 1
            assert metric("candybar_invocation_runtime_milliseconds_count", ok_id, "success") == 1
            assert metric("candybar_invocation_runtime_milliseconds_sum", ok_id, "success") == (
                row.runtime_ms
            )

            # Failure path: exit 3 -> failed event, counters +1/+0.
            fail_command = build_candybar_invoke(
                candybar_id=fail_id,
                name=f"itest-fail-{suffix}",
                trigger="scheduled",
                tick_id="tick:29883000",
            )
            await js.publish(
                command_subject,
                fail_command.model_dump_json().encode(),
                headers={"Nats-Msg-Id": str(fail_command.id)},
            )
            third = (await sub.fetch(1, timeout=5))[0]
            await executor.handle_message(third)
            async with session_scope(engine) as session:
                row = await invocation_by_key(session, fail_command.idempotency_key)
                assert row is not None and row.result == "fail" and row.exit_code == 3
                assert row.stderr_tail and "something broke" in row.stderr_tail
                candybar = await session.get(Candybar, fail_id)
                assert candybar.total_invoked == 1 and candybar.total_successful == 0
            info = await js.stream_info(events_stream)
            assert info.state.messages == 4
            last = parse_envelope(json.loads((await js.get_msg(events_stream, seq=4)).data))
            assert isinstance(last, CandybarFailedEvent)
            assert metric("candybar_invocations_total", fail_id, "fail") == 1
            assert metric("candybar_invocations_total", fail_id, "success") == 0
        finally:
            for stream in (commands_stream, events_stream):
                try:
                    await js.delete_stream(stream)
                except NotFoundError:
                    continue
            async with session_scope(engine) as session:
                for cid in (ok_id, fail_id):
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)  # cascades invocations
            await engine.dispose()
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))


def test_concurrent_terminal_records_never_lose_counter_updates() -> None:
    """Regression (found by the Story 1.7 live smoke): parallel executions of
    one candybar each commit their terminal row; the counters must equal the
    row count. An ORM read-modify-write lost updates under READ COMMITTED."""
    _ensure_schema()

    async def scenario() -> None:
        from datetime import UTC, datetime

        engine = create_registry_engine()
        cid = uuid4()
        manifest = CandybarManifest.model_validate(
            {"name": f"itest-race-{uuid4().hex[:12]}", "schedule": "* * * * *",
             "entrypoint": "/bin/true"}
        )
        try:
            async with session_scope(engine) as session:
                await insert_candybar(session, manifest, candybar_id=cid)

            async def one(i: int, key: str | None = None) -> str:
                now = datetime.now(UTC)
                key = key or f"candybar:{cid}:manual:{uuid4()}"
                rec = TerminalInvocation(
                    invocation_id=uuid4(),
                    candybar_id=cid,
                    trigger="manual",
                    tick_id=None,
                    idempotency_key=key,
                    result="success" if i % 3 else "fail",
                    exit_code=0 if i % 3 else 1,
                    runtime_ms=i,
                    error_message=None,
                    stderr_tail=None,
                    token_spend=0,
                    cost_usd=None,
                    started_at=now,
                    finished_at=now,
                )
                async with session_scope(engine) as session:
                    await record_terminal_invocation(session, rec)
                return key

            keys = await asyncio.gather(*(one(i) for i in range(12)))
            async with session_scope(engine) as session:
                candybar = await session.get(Candybar, cid)
                assert candybar.total_invoked == 12
                assert candybar.total_successful == 8  # i % 3 != 0 for 8 of 0..11

            # A duplicate key raises and its counter bump rolls back with it
            # (the increment now runs before the insert in one transaction).
            with pytest.raises(DuplicateInvocationError):
                await one(1, key=keys[0])
            async with session_scope(engine) as session:
                candybar = await session.get(Candybar, cid)
                assert (candybar.total_invoked, candybar.total_successful) == (12, 8)
        finally:
            async with session_scope(engine) as session:
                row = await session.get(Candybar, cid)
                if row is not None:
                    await session.delete(row)
            await engine.dispose()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
