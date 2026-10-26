"""Live proof of AD-12: a manual invoke through the real API runs down the
same executor/registry/metrics path as a scheduled fire.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL).
The API app is the production `create_app` over the real registry; its sink
publishes the exact production command envelope, rerouted onto an isolated
test stream (never the production BLOODBANK_COMMANDS durable).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import httpx2
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

from candybar.api.app import create_app
from candybar.events import parse_envelope
from candybar.events.envelope import (
    CandybarCompletedEvent,
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
    record_terminal_invocation,
    session_scope,
)
from candybar.registry.models import Candybar
from candybar.registry.store import TerminalInvocation
from test_executor_integration import _ensure_schema, _make_script

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)

ENV_SCRIPT = """#!/bin/sh
printf '%s|%s|%s' "$CANDYBAR_TRIGGER" "$CANDYBAR_TICK_ID" "$CANDYBAR_INVOCATION_ID" > "$ENV_OUT"
exit 0
"""


def test_manual_invoke_via_api_runs_and_records_trigger_manual(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()

    async def scenario() -> None:
        monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
        monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")  # no cgroup delegation in CI
        env_out = tmp_path / "env.txt"
        monkeypatch.setenv("ENV_OUT", str(env_out))  # inherited by the child

        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        commands_stream = f"CANDYBAR_TEST_MANUAL_CMD_{suffix.upper()}"
        events_stream = f"CANDYBAR_TEST_MANUAL_EVT_{suffix.upper()}"
        command_subject = f"candybar.test.{suffix}.invoke"
        event_subject = f"candybar.test.{suffix}.events"
        durable = f"candybar-test-manual-{suffix}"
        engine = create_registry_engine()
        cid = uuid4()
        name = f"itest-manual-{suffix}"
        entry = _make_script(tmp_path, "env.sh", ENV_SCRIPT)

        try:
            manifest = CandybarManifest.model_validate(
                {"name": name, "schedule": "0 0 1 1 *", "entrypoint": entry}
            )
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

            api = create_app(
                session_factory=lambda: session_scope(engine),
                sink=RerouteSink(command_subject),
                metrics_registry=CollectorRegistry(),
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

            metrics = ExecutorMetrics(CollectorRegistry())
            executor = Executor(
                by_key=by_key,
                entrypoint=entrypoint,
                record=record,
                publish=RerouteSink(event_subject).publish,
                registry=ProcessRegistry(),
                metrics=metrics,
            )

            transport = httpx2.ASGITransport(app=api)
            async with httpx2.AsyncClient(transport=transport, base_url="http://candybar") as client:
                resp = await client.post(f"/candybars/{cid}/invoke")
                assert resp.status_code == 202, resp.text
                accepted = resp.json()

                # Unknown id: problem+json, nothing reaches the bus.
                missing = await client.post(f"/candybars/{uuid4()}/invoke")
                assert missing.status_code == 404
                assert missing.headers["content-type"] == "application/problem+json"

            assert (await js.stream_info(commands_stream)).state.messages == 1
            msg = (await sub.fetch(1, timeout=5))[0]
            cmd = parse_envelope(json.loads(msg.data))
            assert isinstance(cmd, CandybarInvokeCommand)
            assert cmd.subject == "bloodbank.cmd.cron.candybar.invoke"
            assert cmd.data.trigger == "manual" and cmd.data.tick_id is None
            assert cmd.idempotency_key == accepted["idempotencyKey"]
            assert cmd.idempotency_key == f"candybar:{cid}:manual:{accepted['commandId']}"

            await executor.handle_message(msg)

            # The child saw the AD-8 env with the manual trigger.
            trigger, tick_id, invocation_id = env_out.read_text().split("|")
            assert trigger == "manual"
            assert tick_id == ""
            assert invocation_id == accepted["invocationId"]

            # Registry: row records trigger=manual under the API's key; counters moved.
            async with session_scope(engine) as session:
                row = await invocation_by_key(session, accepted["idempotencyKey"])
                assert row is not None
                assert row.trigger == "manual" and row.tick_id is None
                assert row.id == UUID(accepted["invocationId"])
                assert row.result == "success" and row.exit_code == 0
                candybar = await session.get(Candybar, cid)
                assert candybar.total_invoked == 1 and candybar.total_successful == 1

            # Metrics: same series as a scheduled fire, counted once.
            labels = {"candybar_id": str(cid), "result": "success"}
            assert metrics.registry.get_sample_value("candybar_invocations_total", labels) == 1

            # Lifecycle facts carry the manual trigger.
            info = await js.stream_info(events_stream)
            assert info.state.messages == 2
            events = [
                parse_envelope(json.loads((await js.get_msg(events_stream, seq=seq)).data))
                for seq in (1, 2)
            ]
            assert isinstance(events[0], CandybarInvokedEvent)
            assert isinstance(events[1], CandybarCompletedEvent)
            assert all(e.data.trigger == "manual" for e in events)
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
