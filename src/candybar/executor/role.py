"""Single durable workqueue consumer of invoke commands for the executor role (AD-5, AD-7).

Each consumed command runs as its own asyncio task — a slow job must not
head-of-line block others. On stop, in-flight children are SIGTERMed (the
worker records them `failed`), then the role waits for the tasks to record
and ack before draining.
"""

from __future__ import annotations

import asyncio
import logging

from nats.errors import (
    BadSubscriptionError,
    ConnectionClosedError,
    ConnectionDrainingError,
)
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from ..bus import NatsEventSink, connect_nats, nats_url
from ..registry.models import Invocation
from ..registry.schema_guard import SchemaGate, guard_enabled, own_head
from ..registry.store import (
    DuplicateInvocationError,
    InvocationClaim,
    TerminalInvocation,
    candybar_enabled,
    candybar_tags,
    claim_invocation,
    create_registry_engine,
    get_run_target,
    invocation_by_key,
    record_terminal_invocation,
    session_scope,
)
from .gpu import GPU_SAMPLER
from .metrics_server import start_metrics_server
from .runner import ProcessRegistry
from .schema_guard import sleep_or_stop
from .worker import RETRY_ON_ABANDON_TAG, Executor

log = logging.getLogger(__name__)
COMMAND_STREAM = "BLOODBANK_COMMANDS"
INVOKE_FILTER = "bloodbank.cmd.cron.candybar.invoke"
DURABLE = "candybar-executor-invoke"
# Executions may run long; the worker sends in_progress heartbeats every
# WAIT_SLICE_SECONDS while a child runs, so this ceiling is a crash horizon.
ACK_WAIT_SECONDS = 3600


async def run_executor(stop: asyncio.Event) -> None:
    """Connect, attach the workqueue durable, and execute commands until stop."""
    # The scrape endpoint comes up first and stays up through a bus outage or a
    # closed schema guard: those are exactly the moments worth observing.
    metrics_server = await start_metrics_server()
    try:
        await _run_executor(stop)
    finally:
        if metrics_server is not None:
            await metrics_server.close()


async def _run_executor(stop: asyncio.Event) -> None:
    # Fail closed on an unknowable own-head before touching the bus (AD-17).
    expected_head = own_head() if guard_enabled() else None
    nc = await connect_nats(nats_url(), name="candybar-executor", stop=stop, log=log)
    if nc is None:
        return
    engine = create_registry_engine()
    gate = SchemaGate(engine, expected_head) if expected_head is not None else None
    tasks: set[asyncio.Task[None]] = set()
    registry = ProcessRegistry()
    sub = None
    try:
        js = nc.jetstream()
        config = ConsumerConfig(
            durable_name=DURABLE,
            filter_subject=INVOKE_FILTER,
            # WorkQueue retention streams reject DeliverPolicy.NEW; ALL is safe
            # here because acked commands leave the stream — a rejoining
            # durable only ever sees unacked work.
            deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ACK_WAIT_SECONDS,
        )
        sub = await js.pull_subscribe(
            INVOKE_FILTER, durable=DURABLE, stream=COMMAND_STREAM, config=config
        )
        sink = NatsEventSink(nc)

        async def by_key(key: str) -> Invocation | None:
            async with session_scope(engine) as session:
                return await invocation_by_key(session, key)

        async def entrypoint(candybar_id) -> str | dict | None:
            async with session_scope(engine) as session:
                return await get_run_target(session, candybar_id)

        async def record(rec: TerminalInvocation) -> bool:
            try:
                async with session_scope(engine) as session:
                    await record_terminal_invocation(session, rec)
                return True
            except DuplicateInvocationError:
                return False

        async def claim(row: InvocationClaim) -> bool:
            try:
                async with session_scope(engine) as session:
                    await claim_invocation(session, row)
                return True
            except DuplicateInvocationError:
                return False

        async def enabled(candybar_id) -> bool | None:
            async with session_scope(engine) as session:
                return await candybar_enabled(session, candybar_id)

        async def retry_abandoned(candybar_id) -> bool:
            async with session_scope(engine) as session:
                tags = await candybar_tags(session, candybar_id)
            return tags is not None and RETRY_ON_ABANDON_TAG in tags

        executor = Executor(
            by_key=by_key,
            entrypoint=entrypoint,
            record=record,
            publish=sink.publish,
            registry=registry,
            claim=claim,
            retry_abandoned=retry_abandoned,
            enabled=enabled,
        )
        while not stop.is_set():
            if gate is not None and not await gate.open():
                # Registry and checkout disagree on the schema (AD-17): consume
                # nothing, leave the commands on the stream, look again soon.
                await sleep_or_stop(stop, 1)
                continue
            try:
                messages = await sub.fetch(1, timeout=1)
            except (NatsTimeoutError, TimeoutError):
                continue
            for message in messages:
                task = asyncio.create_task(executor.handle_message(message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except NotFoundError:
        log.exception("command stream %s not found", COMMAND_STREAM)
        raise
    finally:
        if tasks:
            log.info("stopping: terminating %d in-flight invocation(s)", len(tasks))
            await registry.terminate_all()
            # Children exit on SIGTERM; tasks then record failed + ack/term.
            await asyncio.gather(*tasks, return_exceptions=True)
        # Reap the shared pmon sampler (normally already stopped with the last
        # invocation) so no reader task outlives the loop.
        await GPU_SAMPLER.aclose()
        await engine.dispose()
        # Detach the durable pull consumer BEFORE draining: nats-py otherwise
        # waits the full drain timeout (~30s) for the pull subscription, and
        # `docker stop`'s 10s default grace would SIGKILL us mid-drain.
        # Best-effort: on a dead or draining connection unsubscribe raises
        # (ConnectionClosedError et al.) and must not mask the drain.
        if sub is not None:
            try:
                await sub.unsubscribe()
            except (ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError) as exc:
                log.warning("unsubscribe during stop failed (%s); draining anyway", exc)
        await nc.drain()
        log.info("executor role drained")
