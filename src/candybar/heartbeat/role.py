"""Production wiring for the heartbeat role (AD-2, AD-14).

Connect/retry posture is the shared one from `candybar.bus` (retry the initial
connect until the bus is up or the role is stopped, reconnect forever after,
drain on shutdown); this module only adds the tick loop on top. The NATS URL
comes from the `NATS_URL` env var — the ecosystem convention shared with the
bloodbank services; the deploy story (1.9) wires the container value.

`CONNECT_RETRY_SECONDS` stays re-exported here: tests monkeypatch it on this
module to accelerate the retry loop.
"""

from __future__ import annotations

import asyncio
import logging

import nats  # noqa: F401  (tests monkeypatch role_mod.nats.connect, intercepting bus.connect_nats)

from candybar.bus import CONNECT_RETRY_SECONDS, connect_nats, nats_url

from .publisher import Heartbeat, NatsTickSink

log = logging.getLogger(__name__)

__all__ = ["CONNECT_RETRY_SECONDS", "nats_url", "run_heartbeat"]


async def run_heartbeat(stop: asyncio.Event) -> None:
    """Run the tick publisher against NATS until `stop` is set."""
    url = nats_url()
    nc = await connect_nats(
        url, name="candybar-heartbeat", stop=stop, log=log, retry_seconds=CONNECT_RETRY_SECONDS
    )
    if nc is None:
        log.info("heartbeat role stopped before NATS connect")
        return
    log.info("heartbeat role connected to %s", url)
    try:
        await Heartbeat(NatsTickSink(nc.jetstream())).run(stop)
    finally:
        await nc.drain()
        log.info("heartbeat role drained")
