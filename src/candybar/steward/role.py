"""The steward role: one durable consumer of steward commands (steward-contract.md section 4).

`candybar daemon --roles steward`, run on the host as
`candybar-steward.service` (an event-driven worker, not a clock: it does
nothing until a command arrives). It attaches the `candybar-steward` durable
on the `BLOODBANK_COMMANDS` WorkQueue stream, filtered to
`bloodbank.cmd.cron.candybar.steward` (disjoint from the executor's invoke
filter, as WorkQueue streams require), and runs at most two tasks at a
time, each as its own asyncio task. Like the executor it consumes nothing
while the registry's alembic head differs from its checkout's (AD-17), so a
pair-deploy never runs against an unmigrated schema.

On stop, in-flight tasks are cancelled: each interrupts its OpenCode
session and records itself failed, then the command is acked.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from nats.errors import (
    BadSubscriptionError,
    ConnectionClosedError,
    ConnectionDrainingError,
)
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from ..bus import NatsEventSink, connect_nats, nats_url
from ..executor.schema_guard import sleep_or_stop
from ..registry.schema_guard import SchemaGate, guard_enabled, own_head
from ..registry.store import create_registry_engine, session_scope
from .jev import Jev
from .opencode import OpenCodeClient
from .worker import Steward

log = logging.getLogger(__name__)

COMMAND_STREAM = "BLOODBANK_COMMANDS"
STEWARD_FILTER = "bloodbank.cmd.cron.candybar.steward"
DURABLE = "candybar-steward"
CONCURRENCY = 2
# Tasks run up to ~1.5 h (a 30 min fix plus its proving run); the worker sends
# in_progress every minute, so this is only the crash horizon after which a
# lost task's command is redelivered (and failed as abandoned).
ACK_WAIT_SECONDS = 600


async def run_steward(stop: asyncio.Event) -> None:
    """Connect, attach the durable, and run steward tasks until stop."""
    expected_head = own_head() if guard_enabled() else None
    nc = await connect_nats(nats_url(), name="candybar-steward", stop=stop, log=log)
    if nc is None:
        return
    engine = create_registry_engine()
    gate = SchemaGate(engine, expected_head) if expected_head is not None else None
    tasks: set[asyncio.Task[None]] = set()
    sub = None
    jev = Jev.from_env()
    if jev is None:
        log.warning("steward: CANDYBAR_JEV_KEY is unset; enrich classifies projects by ledger only")
    try:
        js = nc.jetstream()
        config = ConsumerConfig(
            durable_name=DURABLE,
            filter_subject=STEWARD_FILTER,
            deliver_policy=DeliverPolicy.ALL,  # WorkQueue streams reject NEW
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ACK_WAIT_SECONDS,
        )
        sub = await js.pull_subscribe(STEWARD_FILTER, durable=DURABLE, stream=COMMAND_STREAM, config=config)
        sink = NatsEventSink(nc)
        steward = Steward(
            session_factory=lambda: session_scope(engine),
            publish=sink.publish,
            opencode=OpenCodeClient(),
            jev=jev,
            stop=stop,
        )
        log.info("steward role consuming %s (durable %s, concurrency %d)", STEWARD_FILTER, DURABLE, CONCURRENCY)
        while not stop.is_set():
            if gate is not None and not await gate.open():
                await sleep_or_stop(stop, 1)
                continue
            if len(tasks) >= CONCURRENCY:
                # Both slots busy: wait for one to free (or stop), never fetch more.
                stopper = asyncio.ensure_future(stop.wait())
                try:
                    await asyncio.wait({*tasks, stopper}, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    stopper.cancel()
                continue
            try:
                messages = await sub.fetch(1, timeout=1)
            except (NatsTimeoutError, TimeoutError):
                continue
            for message in messages:
                task = asyncio.create_task(steward.handle_message(message), name="candybar-steward-task")
                tasks.add(task)
                task.add_done_callback(tasks.discard)
    except NotFoundError:
        log.exception("command stream %s not found", COMMAND_STREAM)
        raise
    finally:
        if tasks:
            log.info("stopping: cancelling %d in-flight steward task(s)", len(tasks))
            for task in list(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()
        if sub is not None:
            with suppress(ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError):
                await sub.unsubscribe()
        await nc.drain()
        log.info("steward role drained")
