"""Single durable JetStream tick consumer for the dispatcher role (AD-5)."""

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
from ..registry.store import create_registry_engine, list_schedules, session_scope
from .worker import Dispatcher

log = logging.getLogger(__name__)
EVENT_STREAM = "BLOODBANK_EVENTS"
TICK_FILTER = "bloodbank.evt.cron.tick.>"
DURABLE = "candybar-dispatcher-ticks"


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
            ack_wait=120,
        )
        # Bind to a named stream: no ambiguity if another stream also covers ticks.
        sub = await js.pull_subscribe(TICK_FILTER, durable=DURABLE, stream=EVENT_STREAM, config=config)
        sink = NatsEventSink(nc)

        async def schedules():
            async with session_scope(engine) as session:
                return await list_schedules(session)

        dispatcher = Dispatcher(schedules, sink.publish)
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
