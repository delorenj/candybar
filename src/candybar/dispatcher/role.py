"""Single durable JetStream tick consumer for the dispatcher role (AD-5)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from nats.errors import (
    BadSubscriptionError,
    ConnectionClosedError,
    ConnectionDrainingError,
)
from nats.errors import TimeoutError as NatsTimeoutError
from nats.js.api import AckPolicy, ConsumerConfig, DeliverPolicy
from nats.js.errors import NotFoundError

from ..bus import NatsEventSink, connect_nats, nats_url
from ..registry.store import (
    advance_dispatch_cursor,
    create_registry_engine,
    get_dispatch_cursor,
    list_schedules,
    session_scope,
)
from .worker import Dispatcher

log = logging.getLogger(__name__)
EVENT_STREAM = "BLOODBANK_EVENTS"
TICK_FILTER = "bloodbank.evt.cron.tick.>"
DURABLE = "candybar-dispatcher-ticks"
# Crash horizon for an unacked tick: a dispatcher that dies mid-tick has its
# tick redelivered (to the same durable) after this many seconds.
ACK_WAIT_SECONDS = 120


class RegistryTickCursor:
    """The dispatcher's last-seen tick, durable in the registry (Story 2.2).

    Keyed by the durable consumer name: the cursor belongs to the one tick
    consumer that evaluates schedules (AD-5). Each call is its own short
    transaction; nothing is cached in dispatcher memory.
    """

    def __init__(self, engine: Any, name: str = DURABLE) -> None:
        self._engine = engine
        self.name = name

    async def load(self) -> datetime | None:
        async with session_scope(self._engine) as session:
            return await get_dispatch_cursor(session, self.name)

    async def advance(self, minute: datetime) -> None:
        async with session_scope(self._engine) as session:
            await advance_dispatch_cursor(session, self.name, minute)


async def run_dispatcher(stop: asyncio.Event) -> None:
    """Connect, attach one durable pull consumer, and process ticks until stop."""
    nc = await connect_nats(nats_url(), name="candybar-dispatcher", stop=stop, log=log)
    if nc is None:
        return
    engine = create_registry_engine()
    sub = None
    try:
        js = nc.jetstream()
        config = ConsumerConfig(
            durable_name=DURABLE,
            filter_subject=TICK_FILTER,
            deliver_policy=DeliverPolicy.NEW,
            ack_policy=AckPolicy.EXPLICIT,
            ack_wait=ACK_WAIT_SECONDS,
        )
        # Bind to a named stream: no ambiguity if another stream also covers ticks.
        sub = await js.pull_subscribe(TICK_FILTER, durable=DURABLE, stream=EVENT_STREAM, config=config)
        sink = NatsEventSink(nc)

        async def schedules():
            async with session_scope(engine) as session:
                return await list_schedules(session)

        dispatcher = Dispatcher(schedules, sink.publish, cursor=RegistryTickCursor(engine, DURABLE))
        while not stop.is_set():
            try:
                messages = await sub.fetch(1, timeout=1)
            except (NatsTimeoutError, TimeoutError):
                continue
            for message in messages:
                await dispatcher.handle_message(message)
    except NotFoundError:
        log.exception("tick event stream %s not found", EVENT_STREAM)
        raise
    finally:
        await engine.dispose()
        # Unsubscribe the pull consumer before draining, same fast-stop
        # contract as the executor role (docker stop grace is 10s).
        # Best-effort: unsubscribe raises on a dead/draining connection and
        # must not mask the drain.
        if sub is not None:
            try:
                await sub.unsubscribe()
            except (ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError) as exc:
                log.warning("unsubscribe during stop failed (%s); draining anyway", exc)
        await nc.drain()
        log.info("dispatcher role drained")
