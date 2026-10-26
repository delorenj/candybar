"""The fault-injection suite's live-bus refusal (Story 2.1), without a server.

The suite deletes and recreates the production-named BLOODBANK_* streams, so
its guard is the only thing standing between an opt-in env var and the live
bus. Pin both refusal branches and the throwaway pass-through.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from nats.js.errors import NotFoundError

from test_dispatch_fault_injection_integration import _refuse_live_bus


def _info(name: str, subjects: dict[str, int] | None = None) -> Any:
    return SimpleNamespace(
        config=SimpleNamespace(name=name), state=SimpleNamespace(subjects=subjects)
    )


class FakeJS:
    def __init__(self, streams: dict[str, dict[str, int] | None]) -> None:
        self.streams = streams

    async def streams_info(self) -> list[Any]:
        return [_info(name) for name in self.streams]

    async def stream_info(self, name: str, subjects_filter: str | None = None) -> Any:
        if name not in self.streams:
            raise NotFoundError
        return _info(name, self.streams[name])


def test_foreign_stream_refused() -> None:
    js = FakeJS({"BLOODBANK_EVENTS": {}, "GIB_EVENTS": {}})
    with pytest.raises(pytest.fail.Exception, match="foreign stream 'GIB_EVENTS'"):
        asyncio.run(_refuse_live_bus(js))  # type: ignore[arg-type]


def test_non_candybar_subject_refused() -> None:
    js = FakeJS({"BLOODBANK_EVENTS": {"bloodbank.evt.cron.tick.fired": 3, "bloodbank.evt.agent.session.started": 1}})
    with pytest.raises(pytest.fail.Exception, match="non-candybar traffic"):
        asyncio.run(_refuse_live_bus(js))  # type: ignore[arg-type]


def test_throwaway_bus_with_cron_traffic_passes() -> None:
    js = FakeJS(
        {
            "BLOODBANK_EVENTS": {"bloodbank.evt.cron.tick.fired": 5},
            "BLOODBANK_COMMANDS": None,  # empty stream: no subjects map
            "CANDYBAR_TEST_TICKS_ABC": None,
        }
    )
    asyncio.run(_refuse_live_bus(js))  # type: ignore[arg-type]
