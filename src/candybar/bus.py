"""Shared Bloodbank/NATS wiring for daemon roles (AD-14).

Connect posture is identical across roles: retry the initial connect until
the bus is up or the role is stopped (stop-interruptible wait, never
`asyncio.sleep` — AD-1's clock guard forbids sleep primitives outside
heartbeat/), reconnect forever after connect, drain on shutdown.
`NatsEventSink` publishes cron envelopes with `Nats-Msg-Id` = event id for
server-side dedupe (AD-5).
"""

from __future__ import annotations

import asyncio
import logging
import os

import nats
import nats.errors

from .events.envelope import Envelope

NATS_URL_ENV = "NATS_URL"
DEFAULT_NATS_URL = "nats://localhost:4222"
CONNECT_RETRY_SECONDS = 2.0


def nats_url() -> str:
    return os.environ.get(NATS_URL_ENV, DEFAULT_NATS_URL)


async def connect_nats(
    url: str,
    *,
    name: str,
    stop: asyncio.Event,
    log: logging.Logger,
    retry_seconds: float = CONNECT_RETRY_SECONDS,
) -> nats.NATS | None:
    """Connect, retrying every `retry_seconds` until success or stop."""
    nc: nats.NATS | None = None
    while nc is None and not stop.is_set():
        try:
            nc = await nats.connect(url, name=name, max_reconnect_attempts=-1)
        except (nats.errors.Error, OSError, TimeoutError) as exc:
            log.warning(
                "NATS connect to %s failed (%s: %s); retrying in %.0fs",
                url,
                type(exc).__name__,
                exc,
                retry_seconds,
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=retry_seconds)
            except TimeoutError:
                pass  # not stopped: loop around and retry
    return nc


class NatsEventSink:
    """Publishes cron envelopes to JetStream with id-dedupe headers."""

    def __init__(self, nc: nats.NATS) -> None:
        self._js = nc.jetstream()

    async def publish(self, event: Envelope) -> None:
        await self._js.publish(
            event.subject,
            event.model_dump_json().encode(),
            headers={"Nats-Msg-Id": str(event.id)},
        )
