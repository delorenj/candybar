"""Production wiring for the heartbeat role (AD-2, AD-14).

Connects to NATS JetStream (retrying until the bus is up or the role is
stopped), wraps the JetStream context in a NatsTickSink, and runs the tick
loop. The NATS URL comes from the `NATS_URL` env var — the ecosystem
convention shared with the bloodbank services; the deploy story (1.9) wires
the container value.
"""

from __future__ import annotations

import asyncio
import logging
import os

import nats
import nats.errors

from .publisher import Heartbeat, NatsTickSink

log = logging.getLogger(__name__)

NATS_URL_ENV = "NATS_URL"
DEFAULT_NATS_URL = "nats://localhost:4222"
CONNECT_RETRY_SECONDS = 2.0


def nats_url() -> str:
    return os.environ.get(NATS_URL_ENV, DEFAULT_NATS_URL)


async def run_heartbeat(stop: asyncio.Event) -> None:
    """Run the tick publisher against NATS until `stop` is set."""
    url = nats_url()
    nc: nats.NATS | None = None
    while nc is None and not stop.is_set():
        try:
            nc = await nats.connect(
                url,
                name="candybar-heartbeat",
                max_reconnect_attempts=-1,  # reconnect forever after connect (daemon posture)
            )
        except (nats.errors.Error, OSError, TimeoutError) as exc:
            log.warning(
                "NATS connect to %s failed (%s: %s); retrying in %.0fs",
                url,
                type(exc).__name__,
                exc,
                CONNECT_RETRY_SECONDS,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=CONNECT_RETRY_SECONDS)
            except TimeoutError:
                pass  # not stopped: loop around and retry the connect
    if nc is None:
        log.info("heartbeat role stopped before NATS connect")
        return
    log.info("heartbeat role connected to %s", url)
    try:
        await Heartbeat(NatsTickSink(nc.jetstream())).run(stop)
    finally:
        await nc.drain()
        log.info("heartbeat role drained")
