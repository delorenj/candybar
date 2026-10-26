"""Story 5.2 — the schedule window over a real Postgres (env-gated: CANDYBAR_TEST_PG=1).

Real candybars, terminal invocations and both skip kinds are stored with the
registry's own write paths, then read through the real `GET /schedule` route
on a session pinned to a non-UTC zone (the live server's is America/New_York):
row order and states, `lastResult` resolved by idempotency key (scheduled and
misfire share it; manual never matches), skip flags, the registration floor,
and the contract row shape on every row. `fire_results` is also driven past
psycopg's 65,535-parameter ceiling to pin the single text[] parameter.

THROWAWAY DATABASE ONLY: `migrated_db` downgrades the schema to base afterwards.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
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
    fire_results,
    insert_candybar,
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


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 7, 7, hour, minute, second, tzinfo=UTC)


def _manifest(name: str, schedule: str) -> CandybarManifest:
    return CandybarManifest(
        name=name, description="", tags=[], schedule=schedule, entrypoint="/bin/true"
    )


def _terminal(candybar_id: Any, fire: datetime, trigger: str, result: str) -> TerminalInvocation:
    key = fire_key(candybar_id, fire) if trigger != "manual" else f"candybar:{candybar_id}:manual:x"
    tick = None if trigger == "manual" else f"tick:{int(fire.timestamp()) // 60}"
    return TerminalInvocation(
        invocation_id=uuid7(), candybar_id=candybar_id, trigger=trigger, tick_id=tick,
        idempotency_key=key, result=result, exit_code=0 if result == "success" else 1,
        runtime_ms=5, error_message=None, stderr_tail=None, token_spend=0,
        cost_usd=Decimal(0) if trigger == "manual" else None,
        started_at=fire + timedelta(seconds=1), finished_at=fire + timedelta(seconds=2),
    )


def test_schedule_window_round_trip_on_postgres(migrated_db: str) -> None:  # noqa: F811
    async def seed() -> tuple[Any, Any, Any]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                alpha = await insert_candybar(session, _manifest("alpha", "*/5 * * * *"))
                bravo = await insert_candybar(session, _manifest("bravo", "*/15 * * * *"))
                late = await insert_candybar(session, _manifest("late-joiner", "*/5 * * * *"))
            async with session_scope(engine) as session:
                for cid, created in (
                    (alpha.id, at(0)),
                    (bravo.id, at(0)),
                    (late.id, at(10, 7, 20)),  # registered mid-window
                ):
                    await session.execute(
                        update(Candybar).where(Candybar.id == cid).values(created_at=created)
                    )
            async with session_scope(engine) as session:
                await record_terminal_invocation(session, _terminal(alpha.id, at(10), "scheduled", "success"))
                await record_terminal_invocation(session, _terminal(alpha.id, at(10, 5), "misfire", "fail"))
                await record_terminal_invocation(session, _terminal(bravo.id, at(10), "manual", "success"))
            async with session_scope(engine) as session:
                await add_occurrence_skip(session, alpha.id, at(10, 5))  # after it ran
                await add_occurrence_skip(session, alpha.id, at(10, 15))
                await add_rest_of_day_skip(session, bravo.id, date(2026, 7, 7))
            async with session_scope(engine) as session:
                # One text[] parameter: far past the 65,535 bind-parameter limit.
                keys = [f"candybar:{alpha.id}:tick:tick:{n}" for n in range(70_000)]
                keys.append(fire_key(alpha.id, at(10)))
                assert await fire_results(session, keys) == {fire_key(alpha.id, at(10)): "success"}
                assert await fire_results(session, []) == {}
            return alpha.id, bravo.id, late.id
        finally:
            await engine.dispose()

    alpha_id, bravo_id, late_id = asyncio.run(seed())

    # The TestClient runs its own loop (NullPool: no cross-loop connections);
    # the session zone is pinned off UTC, as on the live server.
    api_engine = create_async_engine(
        migrated_db,
        poolclass=NullPool,
        connect_args={"options": "-c timezone=America/New_York"},
    )
    client = TestClient(
        create_app(
            session_factory=lambda: session_scope(api_engine),
            sink=None,
            now=lambda: at(10, 12, 30),
        )
    )
    response = client.get(
        "/schedule", params={"from": "2026-07-07T10:00:00Z", "to": "2026-07-07T10:30:00Z"}
    )
    assert response.status_code == 200, response.text
    rows = response.json()
    for row in rows:
        assert_schedule_row(row)

    names = {str(alpha_id): "alpha", str(bravo_id): "bravo", str(late_id): "late-joiner"}
    table = [
        (
            datetime.fromisoformat(r["fireTime"]).strftime("%H:%M"),
            names[r["candybarId"]],
            r["state"],
            r["skipped"],
            r.get("lastResult"),
        )
        for r in rows
    ]
    assert table == [
        ("10:00", "alpha", "past", False, "success"),
        ("10:00", "bravo", "past", True, None),  # day-skipped; the manual run never matches
        ("10:05", "alpha", "past", False, "fail"),  # misfire key; the later skip keeps history
        ("10:10", "alpha", "past", False, None),
        ("10:10", "late-joiner", "past", False, None),  # nothing before registration
        ("10:15", "alpha", "upcoming", True, None),
        ("10:15", "bravo", "upcoming", True, None),
        ("10:15", "late-joiner", "current", False, None),
        ("10:20", "alpha", "upcoming", False, None),
        ("10:20", "late-joiner", "upcoming", False, None),
        ("10:25", "alpha", "upcoming", False, None),
        ("10:25", "late-joiner", "upcoming", False, None),
    ]
    assert all(r["fireTime"].endswith("+00:00") for r in rows)
    bad = client.get("/schedule", params={"from": "2026-07-07T10:00:00", "to": "2026-07-07T11:00:00Z"})
    assert bad.status_code == 422 and bad.headers["content-type"] == "application/problem+json"
