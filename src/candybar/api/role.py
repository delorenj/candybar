"""Production wiring for the api role (AD-2, AD-11, AD-14).

Story 1.4 serves `POST /candybars`; Story 1.7 adds `GET /metrics` (the
process-default Prometheus registry, so the executor's series recorded in
this process are scrapeable here); Story 1.8 adds `POST /candybars/{id}/invoke`
(manual invoke onto the command stream); Epic 5 extends the app with the CAP-8
read surface. The role connects to NATS (same retry posture as the
heartbeat), builds the registry engine from `DATABASE_URL`, and serves the
FastAPI app under uvicorn until the shared stop event is set.

Story 5.4 feeds the live-update hub behind `GET /schedule/stream` from the
bus: one ephemeral ORDERED consumer on `bloodbank.evt.cron.>`, delivering
new facts only. It is non-durable, never acks and never competes with the
dispatcher's tick durable (AD-5); the hub dedupes ticks on `tick_id`. On stop
every stream is closed first, so uvicorn can finish inside docker's 10s grace.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import nats
import nats.errors
import uvicorn
from nats.errors import (
    BadSubscriptionError,
    ConnectionClosedError,
    ConnectionDrainingError,
)
from nats.js.api import DeliverPolicy

from candybar.bus import NatsEventSink, connect_nats, nats_url
from candybar.registry import create_registry_engine, session_scope

from .app import create_app
from .stream import ScheduleStream, parse_fact

log = logging.getLogger(__name__)

API_HOST_ENV = "CANDYBAR_API_HOST"
API_PORT_ENV = "CANDYBAR_API_PORT"
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8700
# Backstop for a stream that cannot flush on stop (a client that stopped
# reading): uvicorn cancels it rather than outlive docker's 10s grace.
GRACEFUL_SHUTDOWN_SECONDS = 3

EVENT_STREAM = "BLOODBANK_EVENTS"
STREAM_FILTER = "bloodbank.evt.cron.>"  # every cron fact, ticks included


async def subscribe_feed(
    nc: nats.NATS,
    hub: ScheduleStream,
    *,
    stream: str = EVENT_STREAM,
    filter_subject: str = STREAM_FILTER,
) -> Any:
    """Feed `hub` from an ordered, ephemeral JetStream consumer (new facts only).

    Ordered delivery is lossless across reconnects: nats-py re-creates the
    consumer from the last stream sequence it saw. Messages are handled one
    at a time, in stream order, and anything that is not a cron fact is
    dropped.
    """
    js = nc.jetstream()

    async def on_message(msg: Any) -> None:
        fact = parse_fact(msg.data)
        if fact is None:
            log.warning("live stream: dropped a non-cron message on %s", msg.subject)
            return
        await hub.handle(fact)

    return await js.subscribe(
        filter_subject,
        stream=stream,
        cb=on_message,
        ordered_consumer=True,
        deliver_policy=DeliverPolicy.NEW,
    )


async def run_api(stop: asyncio.Event) -> None:
    """Serve the candybar API until `stop` is set."""
    url = nats_url()
    nc = await connect_nats(url, name="candybar-api", stop=stop, log=log)
    if nc is None:
        log.info("api role stopped before NATS connect")
        return
    log.info("api role connected to %s", url)

    engine = create_registry_engine()
    hub = ScheduleStream(session_factory=lambda: session_scope(engine))
    app = create_app(
        session_factory=lambda: session_scope(engine),
        sink=NatsEventSink(nc),
        stream=hub,
    )
    host = os.environ.get(API_HOST_ENV, DEFAULT_API_HOST)
    port = int(os.environ.get(API_PORT_ENV, str(DEFAULT_API_PORT)))
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="info",
            access_log=False,
            timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_SECONDS,
        )
    )
    feed = None
    try:
        await hub.start()
        try:
            feed = await subscribe_feed(nc, hub)
        except (nats.errors.Error, OSError, TimeoutError) as exc:
            # REST keeps serving; the stream then only says `ready`.
            log.error("live stream has no bus feed (%s: %s)", type(exc).__name__, exc)
        serve = asyncio.create_task(server.serve(), name="candybar-api-uvicorn")
        await stop.wait()
        hub.close()  # end open SSE responses, or uvicorn waits on them forever
        server.should_exit = True
        await serve
    finally:
        hub.close()
        if feed is not None:
            try:
                await feed.unsubscribe()
            except (ConnectionClosedError, ConnectionDrainingError, BadSubscriptionError) as exc:
                log.warning("live stream unsubscribe during stop failed (%s)", exc)
        await engine.dispose()
        await nc.drain()
        log.info("api role drained")
