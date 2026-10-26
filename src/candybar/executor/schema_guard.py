"""Executor-side helper for the schema guard (AD-17); the guard itself is `registry.schema_guard`."""

from __future__ import annotations

import asyncio


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep `seconds`, waking early when `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
