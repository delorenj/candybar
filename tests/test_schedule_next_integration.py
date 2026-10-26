"""Story 5.3 — `GET /schedule/next` over a real Postgres (env-gated: CANDYBAR_TEST_PG=1).

Real candybars and both skip kinds are stored with the registry's own write
paths (plus history the poll must never read: a past skip and terminal
invocations), then the real route answers on a session pinned to a non-UTC
zone, as on the live server. Proven here, on real SQL: the read is exactly
two statements (definitions, future skips) and neither touches
`invocations`; the row is the contract row and is the `current` row of
`GET /schedule`; and 200 candybars answer under the stated live budget.

THROWAWAY DATABASE ONLY: `migrated_db` downgrades the schema to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from candybar.api.app import create_app
from candybar.api.schedule import fire_key
from candybar.registry import (
    CandybarManifest,
    TerminalInvocation,
    add_occurrence_skip,
    add_rest_of_day_skip,
    create_registry_engine,
    insert_candybar,
    list_skips,
    record_terminal_invocation,
    session_scope,
    uuid7,
)
from candybar.registry.models import Candybar
from contract_shapes import assert_schedule_row
from test_registry_integration import migrated_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and a throwaway DATABASE_URL) to run live-Postgres tests",
)

# The stated live latency budget (spec): median of 5 polls, 200 candybars.
LIVE_BUDGET_SECONDS = 0.100
FILLERS = ["0 3 * * *", "0 12 * * *", "0 */2 * * *", "45 11 * * 1-5"]


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 7, 7, hour, minute, second, tzinfo=UTC)


def _manifest(name: str, schedule: str, *, noise: bool = False) -> CandybarManifest:
    return CandybarManifest(
        name=name, description="", tags=[], schedule=schedule, entrypoint="/bin/true", noise=noise
    )


def _ran(candybar_id: Any, fire: datetime) -> TerminalInvocation:
    return TerminalInvocation(
        invocation_id=uuid7(), candybar_id=candybar_id, trigger="scheduled",
        tick_id=f"tick:{int(fire.timestamp()) // 60}", idempotency_key=fire_key(candybar_id, fire),
        result="success", exit_code=0, runtime_ms=5, error_message=None, stderr_tail=None,
        token_spend=0, cost_usd=None,
        started_at=fire + timedelta(seconds=1), finished_at=fire + timedelta(seconds=2),
    )


def test_schedule_next_on_postgres(migrated_db: str) -> None:  # noqa: F811
    async def seed() -> dict[str, Any]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                alpha = await insert_candybar(session, _manifest("alpha", "*/5 * * * *"))
                bravo = await insert_candybar(session, _manifest("bravo", "*/15 * * * *", noise=True))
                minutely = await insert_candybar(session, _manifest("minutely", "* * * * *"))
                for i in range(197):
                    await insert_candybar(session, _manifest(f"filler-{i:03}", FILLERS[i % 4]))
            async with session_scope(engine) as session:
                # One statement backdates every registration before the fixed clock.
                await session.execute(update(Candybar).values(created_at=at(0) - timedelta(days=30)))
            async with session_scope(engine) as session:
                await add_occurrence_skip(session, alpha.id, at(10, 15))  # alpha's next fire
                await add_occurrence_skip(session, alpha.id, at(9, 0))  # history
                await add_rest_of_day_skip(session, minutely.id, date(2026, 7, 7))
            async with session_scope(engine) as session:
                await record_terminal_invocation(session, _ran(alpha.id, at(10, 10)))
                await record_terminal_invocation(session, _ran(bravo.id, at(10)))
            async with session_scope(engine) as session:
                # The open-ended read: every skip at or after cut, no history.
                future = await list_skips(session, at(10, 13))
                assert [(s.candybar_id, s.fire_time, s.day) for s in future] == [
                    (alpha.id, at(10, 15), None),
                    (minutely.id, None, date(2026, 7, 7)),
                ]
                assert len(await list_skips(session, at(9), at(10, 16))) == 3
            return {"alpha": alpha.id, "bravo": bravo.id, "minutely": minutely.id}
        finally:
            await engine.dispose()

    ids = asyncio.run(seed())

    api_engine = create_async_engine(
        migrated_db,
        poolclass=NullPool,
        connect_args={"options": "-c timezone=America/New_York"},
    )
    statements: list[str] = []

    @event.listens_for(api_engine.sync_engine, "before_cursor_execute")
    def _count(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    client = TestClient(
        create_app(
            session_factory=lambda: session_scope(api_engine),
            sink=None,
            now=lambda: at(10, 12, 30),
        )
    )
    assert client.get("/schedule/next").status_code == 200  # warm-up: dialect init
    statements.clear()

    response = client.get("/schedule/next")
    assert response.status_code == 200, response.text
    body = response.json()
    assert_schedule_row(body)
    # alpha@10:15 is skipped, minutely's whole day is skipped: bravo is next.
    assert body == {
        "candybarId": str(ids["bravo"]),
        "name": "bravo",
        "schedule": "*/15 * * * *",
        "fireTime": "2026-07-07T10:15:00+00:00",
        "state": "current",
        "skipped": False,
        "noise": True,
    }

    # Bounded: the definitions read and the future-skips read, nothing else.
    assert len(statements) == 2, statements
    assert "FROM candybars" in statements[0] and "FROM skips" in statements[1]
    assert not any("invocations" in s for s in statements)

    # The same row /schedule marks current.
    window = client.get(
        "/schedule", params={"from": "2026-07-07T10:00:00Z", "to": "2026-07-07T11:00:00Z"}
    )
    assert window.status_code == 200, window.text
    assert [r for r in window.json() if r["state"] == "current"] == [body]

    timings = []
    for _ in range(5):
        started = time.perf_counter()
        assert client.get("/schedule/next").status_code == 200
        timings.append(time.perf_counter() - started)
    assert statistics.median(timings) < LIVE_BUDGET_SECONDS, timings
