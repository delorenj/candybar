"""Live proof that a Rust SDK candybar fires on schedule end to end (story 4.3).

The shipped `sdks/rust/examples/hello` crate is built with cargo and
registered from its own `candybar.toml`. The real `Dispatcher` evaluates a
matching tick against the registry's schedules and publishes the invoke
command onto an isolated JetStream stream. The real `Executor` consumes it
and spawns the compiled binary, and the SDK's result lands in the
invocation row, the counters, the metrics and the completed event. A
panicking Rust candybar lands `fail` with its message and its spend.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL),
plus cargo on PATH. Isolated test streams and throwaway rows only.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

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

from candybar.dispatcher import Dispatcher
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarFailedEvent,
    CandybarInvokeCommand,
    CandybarInvokedEvent,
)
from candybar.executor import ExecutorMetrics, ProcessRegistry
from candybar.executor.worker import Executor
from candybar.registry import (
    CandybarManifest,
    DuplicateInvocationError,
    create_registry_engine,
    get_entrypoint,
    insert_candybar,
    invocation_by_key,
    load_manifest,
    record_terminal_invocation,
    session_scope,
)
from candybar.registry.models import Candybar
from candybar.registry.store import TerminalInvocation, list_schedules
from test_executor_integration import _ensure_schema
from test_sdk_rust import HELLO_DIR, build_hello, build_outcomes_probe

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)


def test_rust_sdk_candybar_fires_on_schedule_end_to_end(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    hello_bin = build_hello()
    probe_bin = build_outcomes_probe()

    async def scenario() -> None:
        monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
        monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
        monkeypatch.setenv("CANDYBAR_EXAMPLE_MODE", "panic")  # read by the probe only

        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        commands_stream = f"CANDYBAR_TEST_RUST_CMD_{suffix.upper()}"
        events_stream = f"CANDYBAR_TEST_RUST_EVT_{suffix.upper()}"
        command_subject = f"candybar.test.{suffix}.invoke"
        event_subject = f"candybar.test.{suffix}.events"
        durable = f"candybar-test-rust-{suffix}"
        engine = create_registry_engine()
        hello_id, probe_id = uuid4(), uuid4()

        try:
            # Register both from manifests: the shipped hello candybar.toml
            # (renamed so reruns never collide), and the panicking probe.
            shipped = load_manifest(HELLO_DIR)
            assert Path(shipped.entrypoint) == hello_bin.resolve()
            hello = shipped.model_copy(update={"name": f"itest-rust-hello-{suffix}"})
            panicky = CandybarManifest.model_validate(
                {"name": f"itest-rust-panic-{suffix}", "schedule": "* * * * *", "entrypoint": str(probe_bin)}
            )
            async with session_scope(engine) as session:
                await insert_candybar(session, hello, candybar_id=hello_id)
                await insert_candybar(session, panicky, candybar_id=probe_id)

            await js.add_stream(StreamConfig(
                name=commands_stream, subjects=[command_subject],
                retention=RetentionPolicy.WORK_QUEUE,
            ))
            await js.add_stream(StreamConfig(
                name=events_stream, subjects=[event_subject], retention=RetentionPolicy.LIMITS
            ))
            sub = await js.pull_subscribe(
                command_subject, durable=durable, stream=commands_stream,
                config=ConsumerConfig(
                    durable_name=durable, filter_subject=command_subject,
                    deliver_policy=DeliverPolicy.ALL, ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=30,
                ),
            )

            class RerouteSink:
                """The production envelope and dedupe header, on a test subject."""

                def __init__(self, subject: str) -> None:
                    self.subject = subject

                async def publish(self, event) -> None:
                    await js.publish(
                        self.subject,
                        event.model_dump_json().encode(),
                        headers={"Nats-Msg-Id": str(event.id)},
                    )

            async def schedules():
                # The real registry read, narrowed to this test's rows so a
                # shared database's other candybars never reach the stream.
                async with session_scope(engine) as session:
                    entries = await list_schedules(session)
                return [e for e in entries if e.id in (hello_id, probe_id)]

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

            metrics = ExecutorMetrics(CollectorRegistry())
            executor = Executor(
                by_key=by_key,
                entrypoint=entrypoint,
                record=record,
                publish=RerouteSink(event_subject).publish,
                registry=ProcessRegistry(),
                metrics=metrics,
            )

            # The tick: every-minute schedules match any UTC minute.
            tick = build_tick_fired(datetime(2026, 10, 26, 6, 15, tzinfo=UTC))
            dispatcher = Dispatcher(schedules, RerouteSink(command_subject).publish)
            assert await dispatcher.dispatch(tick) == 2

            commands = {}
            for msg in await sub.fetch(2, timeout=5):
                cmd = parse_envelope(json.loads(msg.data))
                assert isinstance(cmd, CandybarInvokeCommand)
                assert cmd.data.trigger == "scheduled" and cmd.data.tick_id == tick.data.tick_id
                commands[cmd.data.candybar_id] = (cmd, msg)
            assert set(commands) == {hello_id, probe_id}
            for _, msg in commands.values():
                await executor.handle_message(msg)

            hello_cmd, _ = commands[hello_id]
            probe_cmd, _ = commands[probe_id]
            assert hello_cmd.idempotency_key == f"candybar:{hello_id}:tick:{tick.data.tick_id}"

            async with session_scope(engine) as session:
                ok = await invocation_by_key(session, hello_cmd.idempotency_key)
                assert ok is not None
                assert (ok.trigger, ok.tick_id, ok.result, ok.exit_code) == (
                    "scheduled", tick.data.tick_id, "success", 0,
                )
                assert ok.token_spend == 1545 and ok.cost_usd == Decimal("0.0042")
                bad = await invocation_by_key(session, probe_cmd.idempotency_key)
                assert bad is not None
                assert (bad.result, bad.exit_code) == ("fail", 1)
                assert bad.error_message == "panic: kaboom"
                assert bad.token_spend == 13
                assert "kaboom" in (bad.stderr_tail or "")
                for cid, successes in ((hello_id, 1), (probe_id, 0)):
                    row = await session.get(Candybar, cid)
                    assert (row.total_invoked, row.total_successful) == (1, successes)

            def count(cid, result):
                return metrics.registry.get_sample_value(
                    "candybar_invocations_total", {"candybar_id": str(cid), "result": result}
                )

            assert (count(hello_id, "success"), count(hello_id, "fail")) == (1, 0)
            assert (count(probe_id, "success"), count(probe_id, "fail")) == (0, 1)

            info = await js.stream_info(events_stream)
            events = [
                parse_envelope(json.loads((await js.get_msg(events_stream, seq=seq)).data))
                for seq in range(1, info.state.messages + 1)
            ]
            invoked = [e for e in events if isinstance(e, CandybarInvokedEvent)]
            assert {e.data.candybar_id for e in invoked} == {hello_id, probe_id}
            (completed,) = [e for e in events if isinstance(e, CandybarCompletedEvent)]
            assert completed.data.candybar_id == hello_id
            assert completed.data.trigger == "scheduled"
            assert completed.data.metrics.token_spend == 1545
            assert completed.data.metrics.cost_usd == 0.0042
            (failed,) = [e for e in events if isinstance(e, CandybarFailedEvent)]
            assert failed.data.candybar_id == probe_id
            assert failed.data.error.message == "panic: kaboom"
            assert (await js.stream_info(commands_stream)).state.messages == 0  # both acked
        finally:
            for stream in (commands_stream, events_stream):
                try:
                    await js.delete_stream(stream)
                except NotFoundError:
                    continue
            async with session_scope(engine) as session:
                for cid in (hello_id, probe_id):
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)  # cascades invocations
            await engine.dispose()
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=120))
