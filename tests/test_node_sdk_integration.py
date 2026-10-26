"""Live proof (Story 4.2): a node SDK candybar fires on schedule end to end.

tick fact -> real Dispatcher (registry schedules, croniter) -> invoke command
on an isolated JetStream stream -> real Executor -> the shipped node example
via candybar-sdk -> terminal row + counters + token_spend in Postgres, metrics
observed, completed/failed facts on the wire.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL),
plus `node` on PATH. Isolated streams and throwaway rows only.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
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
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarCompletedEvent, CandybarFailedEvent
from candybar.executor import ExecutorMetrics, ProcessRegistry, run_invocation
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

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1"
    or os.environ.get("CANDYBAR_TEST_PG") != "1"
    or shutil.which("node") is None,
    reason="requires CANDYBAR_TEST_NATS=1, CANDYBAR_TEST_PG=1 (and DATABASE_URL) and node",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "sdks" / "node" / "examples" / "hello-node"
SDK_ENTRY = REPO_ROOT / "sdks" / "node" / "src" / "index.js"


def _ensure_schema() -> None:
    """Throwaway DATABASE_URL may be empty or just-downgraded: upgrade first."""
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


def test_node_sdk_candybar_fires_on_schedule_end_to_end(tmp_path: Path, monkeypatch) -> None:
    _ensure_schema()
    monkeypatch.setenv("CANDYBAR_WORK_ROOT", str(tmp_path / "work"))
    monkeypatch.setenv("CANDYBAR_CGROUP_ROOT", "")
    monkeypatch.setenv("HELLO_NODE_TOKENS", "321")
    monkeypatch.delenv("HELLO_NODE_FAIL", raising=False)

    failing = tmp_path / "rejects.mjs"
    failing.write_text(
        "#!/usr/bin/env node\n"
        f'import {{ candybar }} from "{SDK_ENTRY.as_uri()}";\n'
        "await candybar(async (ctx) => { ctx.addTokens(17);"
        ' throw new Error("model refused"); });\n'
    )
    failing.chmod(failing.stat().st_mode | stat.S_IXUSR)

    async def scenario() -> None:
        nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
        js = nc.jetstream()
        suffix = uuid4().hex[:12]
        commands_stream = f"CANDYBAR_TEST_NODE_CMD_{suffix.upper()}"
        events_stream = f"CANDYBAR_TEST_NODE_EVT_{suffix.upper()}"
        command_subject = f"candybar.test.{suffix}.invoke"
        event_subject = f"candybar.test.{suffix}.events"
        durable = f"candybar-test-node-{suffix}"
        engine = create_registry_engine()
        example = load_manifest(EXAMPLE_DIR)  # the shipped candybar.toml
        ok_id, fail_id, idle_id = uuid4(), uuid4(), uuid4()
        manifests = {
            ok_id: example.model_copy(update={"name": f"itest-hello-node-{suffix}"}),
            fail_id: CandybarManifest.model_validate({
                "name": f"itest-node-rejects-{suffix}", "schedule": example.schedule,
                "entrypoint": str(failing),
            }),
            # Never matches the tick below: proves the dispatcher evaluated cron.
            idle_id: example.model_copy(
                update={"name": f"itest-node-idle-{suffix}", "schedule": "1 2 3 4 *"}
            ),
        }
        ours = set(manifests)
        try:
            for cid, manifest in manifests.items():
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
                    deliver_policy=DeliverPolicy.ALL, ack_policy=AckPolicy.EXPLICIT,
                    ack_wait=30,
                ),
            )

            async def schedules():
                async with session_scope(engine) as session:
                    return [e for e in await list_schedules(session) if e.id in ours]

            async def publish_command(command) -> None:
                await js.publish(
                    command_subject, command.model_dump_json().encode(),
                    headers={"Nats-Msg-Id": str(command.id)},
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

            async def publish_event(event) -> None:
                await js.publish(
                    event_subject, event.model_dump_json().encode(),
                    headers={"Nats-Msg-Id": str(event.id)},
                )

            metrics = ExecutorMetrics(CollectorRegistry())
            executor = Executor(
                by_key=by_key, entrypoint=entrypoint, record=record, publish=publish_event,
                run=run_invocation, registry=ProcessRegistry(), metrics=metrics,
            )

            # The schedule fires: one UTC minute tick matching */5.
            tick = build_tick_fired(datetime(2026, 10, 26, 6, 35, tzinfo=UTC))
            dispatched = await Dispatcher(schedules, publish_command).dispatch(tick)
            assert dispatched == 2  # ok + rejects; idle's cron does not match

            for msg in await sub.fetch(2, timeout=5):
                await executor.handle_message(msg)

            tick_id = tick.data.tick_id
            async with session_scope(engine) as session:
                ok_row = await invocation_by_key(session, f"candybar:{ok_id}:tick:{tick_id}")
                fail_row = await invocation_by_key(session, f"candybar:{fail_id}:tick:{tick_id}")
                assert ok_row is not None and fail_row is not None
                assert (ok_row.trigger, ok_row.tick_id) == ("scheduled", tick_id)
                assert (ok_row.result, ok_row.exit_code, ok_row.token_spend) == ("success", 0, 321)
                assert (fail_row.result, fail_row.exit_code) == ("fail", 1)
                assert fail_row.error_message == "model refused"
                assert fail_row.token_spend == 17
                assert "candybar-sdk: handler failed: model refused" in (fail_row.stderr_tail or "")
                for cid, invoked, ok in ((ok_id, 1, 1), (fail_id, 1, 0), (idle_id, 0, 0)):
                    row = await session.get(Candybar, cid)
                    assert (row.total_invoked, row.total_successful) == (invoked, ok)

            def sample(name: str, cid, result: str) -> float:
                labels = {"candybar_id": str(cid), "result": result}
                return metrics.registry.get_sample_value(name, labels) or 0.0

            assert sample("candybar_invocations_total", ok_id, "success") == 1
            assert sample("candybar_invocations_total", fail_id, "fail") == 1
            assert sample("candybar_invocation_runtime_milliseconds_count", ok_id, "success") == 1

            info = await js.stream_info(events_stream)
            assert info.state.messages == 4  # invoked + terminal, per candybar
            terminal = {}
            for seq in range(1, 5):
                event = parse_envelope(json.loads((await js.get_msg(events_stream, seq=seq)).data))
                if isinstance(event, (CandybarCompletedEvent, CandybarFailedEvent)):
                    terminal[event.data.candybar_id] = event
            assert isinstance(terminal[ok_id], CandybarCompletedEvent)
            assert terminal[ok_id].data.metrics.token_spend == 321
            assert isinstance(terminal[fail_id], CandybarFailedEvent)
        finally:
            for stream in (commands_stream, events_stream):
                try:
                    await js.delete_stream(stream)
                except NotFoundError:
                    continue
            async with session_scope(engine) as session:
                for cid in ours:
                    row = await session.get(Candybar, cid)
                    if row is not None:
                        await session.delete(row)
            await engine.dispose()
            await nc.drain()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
