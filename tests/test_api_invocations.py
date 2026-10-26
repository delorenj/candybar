"""Unit tests for the Story 3.3 invocation read routes (AD-9 "queryable via the API").

`GET /invocations/{id}` resolves an `invocation_id` exemplar to its stored
row; `GET /candybars/{id}/invocations` lists a candybar's recorded
invocations newest first. Store lookups are monkeypatched at the app module
(as in test_api_invoke.py); the live-Postgres round trip of the read
functions is in tests/test_registry_integration.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app, invocation_row_json
from candybar.registry.models import Invocation

CANDYBAR = UUID("01900000-0000-7000-8000-0000000000aa")
T0 = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)


def make_invocation(i: int = 0, **overrides: Any) -> Invocation:
    fields: dict[str, Any] = {
        "id": uuid4(),
        "candybar_id": CANDYBAR,
        "trigger": "scheduled",
        "tick_id": f"tick:{29883000 + i}",
        "idempotency_key": f"candybar:{CANDYBAR}:tick:tick:{29883000 + i}",
        "result": "success",
        "exit_code": 0,
        "runtime_ms": 1234,
        "error_message": None,
        "stderr_tail": None,
        "token_spend": 1200,
        "cost_usd": Decimal("0.03"),
        "memory_peak_bytes": 64 * 1024 * 1024,
        "cpu_load": 0.75,
        "gpu_load": 0.5,
        "started_at": T0 + timedelta(minutes=i),
        "finished_at": T0 + timedelta(minutes=i, seconds=2),
        "created_at": T0 + timedelta(minutes=i, seconds=2),
    }
    fields.update(overrides)
    return Invocation(**fields)


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    class Rig:
        def __init__(self) -> None:
            self.invocations: dict[UUID, Invocation] = {}
            self.candybars = {CANDYBAR: SimpleNamespace(id=CANDYBAR, name="llm-digest")}
            self.list_calls: list[tuple[UUID, int]] = []
            self.db_error: BaseException | None = None

        def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
            @asynccontextmanager
            async def session_factory() -> AsyncIterator[Any]:
                yield object()

            return TestClient(
                create_app(session_factory=session_factory, sink=None),
                raise_server_exceptions=raise_server_exceptions,
            )

    rig = Rig()

    async def fake_get_invocation(session: Any, invocation_id: UUID) -> Any:
        if rig.db_error is not None:
            raise rig.db_error
        return rig.invocations.get(invocation_id)

    async def fake_get_candybar(session: Any, candybar_id: UUID) -> Any:
        return rig.candybars.get(candybar_id)

    async def fake_list_invocations(session: Any, candybar_id: UUID, limit: int) -> list[Any]:
        rig.list_calls.append((candybar_id, limit))
        rows = [r for r in rig.invocations.values() if r.candybar_id == candybar_id]
        rows.sort(key=lambda r: (r.started_at, r.id), reverse=True)
        return rows[:limit]

    monkeypatch.setattr(app_module, "get_invocation", fake_get_invocation)
    monkeypatch.setattr(app_module, "get_candybar", fake_get_candybar)
    monkeypatch.setattr(app_module, "list_invocations", fake_list_invocations)
    return rig


def assert_problem(resp: Any, status: int) -> dict[str, Any]:
    assert resp.status_code == status, resp.text
    assert resp.headers["content-type"].startswith("application/problem+json")
    body = resp.json()
    assert body["status"] == status
    return body


# ---------------------------------------------------------------------------
# GET /invocations/{id}
# ---------------------------------------------------------------------------


def test_known_invocation_returns_the_full_record(rig: Any) -> None:
    row = make_invocation()
    rig.invocations[row.id] = row
    resp = rig.client().get(f"/invocations/{row.id}")
    assert resp.status_code == 200
    assert resp.json() == {
        "id": str(row.id),
        "candybarId": str(CANDYBAR),
        "trigger": "scheduled",
        "tickId": row.tick_id,
        "idempotencyKey": row.idempotency_key,
        "result": "success",
        "exitCode": 0,
        "errorMessage": None,
        "stderrTail": None,
        # all five CAP-3 measures + both token fields
        "runtimeMs": 1234,
        "memoryPeakBytes": 64 * 1024 * 1024,
        "cpuLoad": 0.75,
        "gpuLoad": 0.5,
        "tokenSpend": 1200,
        "costUsd": 0.03,
        "startedAt": row.started_at.isoformat(),
        "finishedAt": row.finished_at.isoformat(),
    }


def test_bare_executable_record_reports_zero_tokens_and_null_cost(rig: Any) -> None:
    row = make_invocation(token_spend=0, cost_usd=None, memory_peak_bytes=0, cpu_load=0.0, gpu_load=0.0)
    rig.invocations[row.id] = row
    body = rig.client().get(f"/invocations/{row.id}").json()
    assert body["tokenSpend"] == 0 and body["costUsd"] is None
    assert body["memoryPeakBytes"] == 0 and body["cpuLoad"] == 0 and body["gpuLoad"] == 0


def test_failed_manual_record_carries_failure_fields(rig: Any) -> None:
    row = make_invocation(
        trigger="manual", tick_id=None, result="fail", exit_code=1,
        error_message="exit code 1", stderr_tail="boom", token_spend=500,
    )
    rig.invocations[row.id] = row
    body = rig.client().get(f"/invocations/{row.id}").json()
    assert body["result"] == "fail" and body["exitCode"] == 1
    assert body["tickId"] is None and body["trigger"] == "manual"
    assert body["errorMessage"] == "exit code 1" and body["stderrTail"] == "boom"
    assert body["tokenSpend"] == 500


def test_unknown_or_in_flight_invocation_is_404(rig: Any) -> None:
    body = assert_problem(rig.client().get(f"/invocations/{uuid4()}"), 404)
    assert body["title"] == "Invocation not found"


def test_malformed_invocation_id_is_422(rig: Any) -> None:
    assert_problem(rig.client().get("/invocations/not-a-uuid"), 422)


def test_database_failure_is_500_problem(rig: Any) -> None:
    rig.db_error = OSError("connection refused")
    resp = rig.client(raise_server_exceptions=False).get(f"/invocations/{uuid4()}")
    assert_problem(resp, 500)


def test_cost_usd_serializes_as_a_json_number() -> None:
    body = invocation_row_json(make_invocation(cost_usd=Decimal("1.5")))
    assert body["costUsd"] == 1.5 and isinstance(body["costUsd"], float)


# ---------------------------------------------------------------------------
# GET /candybars/{id}/invocations
# ---------------------------------------------------------------------------


def test_list_returns_newest_first_bounded_by_limit(rig: Any) -> None:
    rows = [make_invocation(i) for i in range(4)]
    for r in rows:
        rig.invocations[r.id] = r
    resp = rig.client().get(f"/candybars/{CANDYBAR}/invocations?limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["candybarId"] == str(CANDYBAR)
    assert [i["id"] for i in body["invocations"]] == [str(rows[3].id), str(rows[2].id)]
    assert rig.list_calls == [(CANDYBAR, 2)]


def test_list_default_limit_is_50(rig: Any) -> None:
    resp = rig.client().get(f"/candybars/{CANDYBAR}/invocations")
    assert resp.status_code == 200 and resp.json()["invocations"] == []
    assert rig.list_calls == [(CANDYBAR, 50)]


def test_list_unknown_candybar_is_404(rig: Any) -> None:
    body = assert_problem(rig.client().get(f"/candybars/{uuid4()}/invocations"), 404)
    assert body["title"] == "Candybar not found"
    assert rig.list_calls == []


@pytest.mark.parametrize("limit", ["0", "-1", "501", "lots"])
def test_list_bad_limit_is_422(rig: Any, limit: str) -> None:
    assert_problem(rig.client().get(f"/candybars/{CANDYBAR}/invocations?limit={limit}"), 422)
    assert rig.list_calls == []


def test_list_accepts_the_max_limit(rig: Any) -> None:
    assert rig.client().get(f"/candybars/{CANDYBAR}/invocations?limit=500").status_code == 200
    assert rig.list_calls == [(CANDYBAR, 500)]


def test_timestamps_are_serialized_in_utc_whatever_the_db_session_zone() -> None:
    """The live Postgres session zone is America/New_York: timestamptz values
    come back at -04:00/-05:00 and must still be emitted as UTC (AD-13)."""
    from datetime import timezone

    eastern = timezone(timedelta(hours=-4))
    row = make_invocation(
        started_at=datetime(2026, 10, 26, 2, 58, 32, tzinfo=eastern),
        finished_at=datetime(2026, 10, 26, 2, 58, 33, tzinfo=eastern),
    )
    body = invocation_row_json(row)
    assert body["startedAt"] == "2026-10-26T06:58:32+00:00"
    assert body["finishedAt"] == "2026-10-26T06:58:33+00:00"
    assert invocation_row_json(make_invocation(finished_at=None))["finishedAt"] is None


def test_candybar_rows_are_serialized_in_utc_too() -> None:
    from datetime import timezone

    from candybar.api.app import candybar_row_json

    row = SimpleNamespace(
        id=CANDYBAR, name="x", description="", tags=[], schedule="* * * * *",
        timezone="UTC", entrypoint="/bin/true", noise=False,
        created_at=datetime(2026, 10, 26, 2, 0, tzinfo=timezone(timedelta(hours=-4))),
    )
    assert candybar_row_json(row)["createdAt"] == "2026-10-26T06:00:00+00:00"
