"""Story 4.1 end to end: the python-SDK example fires on schedule.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL).
The example is registered from its own `candybar.toml`, a tick matching its
schedule goes through the real `Dispatcher`, the invoke command rides an
isolated JetStream workqueue stream (never the production durable), and the
real `Executor` spawns `hello.py` — which reports its token spend through
the SDK's result file into the row, the counters, the metrics and the
completed event.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime
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
from candybar.dispatcher.schedule import matches_tick
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import (
    CandybarCompletedEvent,
    CandybarInvokeCommand,
    CandybarInvokedEvent,
)
from candybar.executor import ExecutorMetrics, ProcessRegistry
from candybar.executor.metrics import RUNTIME_METRIC
from candybar.executor.worker import Executor
from candybar.registry import (
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

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)

EXAMPLE_DIR = Path(__file__).resolve().parent.parent / "sdks" / "python" / "examples" / "hello"
TICK_MINUTE = datetime(2026, 10, 26, 6, 5, tzinfo=UTC)  # matches */5


def test_python_sdk_example_fires_on_schedule_with_metrics(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()

    async def scenario() -> None:
        monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
        monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
        # `#!/usr/bin/env python3` must resolve to an interpreter with the SDK.
        monkeypatch.setenv("PATH", f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}")

        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        commands_stream = f"CANDYBAR_TEST_SDKPY_CMD_{suffix.upper()}"
        events_stream = f"CANDYBAR_TEST_SDKPY_EVT_{suffix.upper()}"
        command_subject = f"candybar.test.{suffix}.invoke"
        event_subject = f"candybar.test.{suffix}.events"
        durable = f"candybar-test-sdkpy-{suffix}"
        engine = create_registry_engine()
        cid = uuid4()

        # Registered through the real manifest loader, as `candybar register` does.
        manifest = load_manifest(EXAMPLE_DIR)
        assert matches_tick(manifest.schedule, TICK_MINUTE)
        manifest = manifest.model_copy(update={"name": f"{manifest.name}-{suffix}"})

        try:
            async with session_scope(engine) as session:
                await insert_candybar(session, manifest, candybar_id=cid)

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
                # The real registry read, scoped to this test's row so leftovers
                # from other suites sharing the throwaway DB can't dispatch.
                async with session_scope(engine) as session:
                    return [e for e in await list_schedules(session) if e.id == cid]

            dispatcher = Dispatcher(schedules, RerouteSink(command_subject).publish)
            assert await dispatcher.dispatch(build_tick_fired(TICK_MINUTE)) == 1

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

            msg = (await sub.fetch(1, timeout=5))[0]
            cmd = parse_envelope(json.loads(msg.data))
            assert isinstance(cmd, CandybarInvokeCommand)
            assert cmd.data.trigger == "scheduled"
            tick_id = cmd.data.tick_id
            assert cmd.idempotency_key == f"candybar:{cid}:tick:{tick_id}"

            await executor.handle_message(msg)

            # Registry: success row with the SDK-reported token spend + cost.
            async with session_scope(engine) as session:
                row = await invocation_by_key(session, cmd.idempotency_key)
                assert row is not None
                assert row.result == "success" and row.exit_code == 0, row.error_message
                assert row.trigger == "scheduled" and row.tick_id == tick_id
                assert row.token_spend == 142
                assert float(row.cost_usd) == pytest.approx(0.0007)
                candybar = await session.get(Candybar, cid)
                assert candybar.total_invoked == 1 and candybar.total_successful == 1

            # Prometheus: counted once on the success series, runtime observed.
            labels = {"candybar_id": str(cid), "result": "success"}
            reg = metrics.registry
            assert reg.get_sample_value("candybar_invocations_total", labels) == 1
            assert reg.get_sample_value(f"{RUNTIME_METRIC}_count", labels) == 1

            # Lifecycle facts: invoked then completed, carrying the SDK metrics.
            assert (await js.stream_info(events_stream)).state.messages == 2
            events = [
                parse_envelope(json.loads((await js.get_msg(events_stream, seq=seq)).data))
                for seq in (1, 2)
            ]
            assert isinstance(events[0], CandybarInvokedEvent)
            assert isinstance(events[1], CandybarCompletedEvent)
            assert events[1].data.metrics.token_spend == 142
            assert events[1].data.metrics.cost_usd == pytest.approx(0.0007)
            assert (await js.stream_info(commands_stream)).state.messages == 0  # acked
        finally:
            for stream in (commands_stream, events_stream):
                try:
                    await js.delete_stream(stream)
                except NotFoundError:
                    continue
            async with session_scope(engine) as session:
                row = await session.get(Candybar, cid)
                if row is not None:
                    await session.delete(row)  # cascades invocations
            await engine.dispose()
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
