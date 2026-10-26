"""Production wiring for the api role (AD-2, AD-11, AD-14).

Story 1.4 serves `POST /candybars`; Story 1.7 adds `GET /metrics` (the
process-default Prometheus registry, so the executor's series recorded in
this process are scrapeable here); Epic 5 extends the app with the CAP-8
read surface. The role connects to NATS (same retry posture as the
heartbeat), builds the registry engine from `DATABASE_URL`, and serves the
FastAPI app under uvicorn until the shared stop event is set.
"""

from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from candybar.bus import NatsEventSink, connect_nats, nats_url
from candybar.registry import create_registry_engine, session_scope

from .app import create_app

log = logging.getLogger(__name__)

API_HOST_ENV = "CANDYBAR_API_HOST"
API_PORT_ENV = "CANDYBAR_API_PORT"
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8700


async def run_api(stop: asyncio.Event) -> None:
    """Serve the candybar API until `stop` is set."""
    url = nats_url()
    nc = await connect_nats(url, name="candybar-api", stop=stop, log=log)
    if nc is None:
        log.info("api role stopped before NATS connect")
        return
    log.info("api role connected to %s", url)

    engine = create_registry_engine()
    app = create_app(
        session_factory=lambda: session_scope(engine),
        sink=NatsEventSink(nc),
    )
    host = os.environ.get(API_HOST_ENV, DEFAULT_API_HOST)
    port = int(os.environ.get(API_PORT_ENV, str(DEFAULT_API_PORT)))
    server = uvicorn.Server(
        uvicorn.Config(app, host=host, port=port, log_level="info", access_log=False)
    )
    try:
        serve = asyncio.create_task(server.serve(), name="candybar-api-uvicorn")
        await stop.wait()
        server.should_exit = True
        await serve
    finally:
        await engine.dispose()
        await nc.drain()
        log.info("api role drained")
