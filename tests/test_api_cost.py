"""The cost surface of the REST API over the in-memory registry rig.

`cost` is on every definition response and is never null (the Holocene
contract); `GET /candybars/{id}/cost` adds thresholds and what-ifs, `GET /costs`
ranks, `POST /costs/refresh` recomputes. The SQL totals behind it are proven on
live Postgres in tests/test_cost_integration.py; here a stand-in cost store
hands the routes totals directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

import candybar.api.app as app_module
from candybar import costs
from contract_shapes import LIST_KEYS, SINGLE_KEYS, assert_contract_shape
from test_api_definitions import Rig, rig  # noqa: F401  (rig is a fixture)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def totals(**kw: Any) -> dict[str, Any]:
    """100 runs a window, 20 core-seconds and 10 s each."""
    base = {
        "runs": 100, "failedRuns": 2, "runtimeMs": 1_000_000,
        "cpuRuns": 100, "cpuSeconds": 2000.0, "gpuSeconds": 0.0,
        "memRuns": 100, "memPeakBytesSum": 100 * 50 * 1024 * 1024,
        "memPeakBytesMax": 50 * 1024 * 1024, "memGibSeconds": 100 * (50 / 1024) * 10,
        "tokenSum": 0, "tokenRuns": 0, "usdSum": 0, "usdRuns": 0,
    }
    return {**base, **kw}


class CostStore:
    """Stands in for the candybar_costs table."""

    def __init__(self) -> None:
        self.rows: dict[UUID, SimpleNamespace] = {}
        self.refreshed: list[tuple[UUID | None, int | None]] = []

    def put(self, candybar_id: UUID, window_days: int = 7, **kw: Any) -> None:
        self.rows[candybar_id] = SimpleNamespace(
            candybar_id=candybar_id, window_days=window_days, computed_at=NOW,
            totals=totals(**kw),
        )


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch, rig: Rig) -> CostStore:  # noqa: F811
    store = CostStore()

    async def list_costs(session: Any) -> dict[UUID, Any]:
        return dict(store.rows)

    async def get_cost(session: Any, candybar_id: UUID) -> Any:
        return store.rows.get(candybar_id)

    async def refresh_costs(session: Any, candybar_id: UUID | None = None, **kw: Any) -> int:
        store.refreshed.append((candybar_id, kw.get("window_days")))
        return 1 if candybar_id else len(rig.rows)

    monkeypatch.setattr(app_module, "list_costs", list_costs)
    monkeypatch.setattr(app_module, "get_cost", get_cost)
    monkeypatch.setattr(app_module, "refresh_costs", refresh_costs)
    return store


# --- the contract: a cost on every definition response ----------------------------


def test_the_list_and_detail_always_carry_a_cost_object(rig: Rig, store: CostStore) -> None:  # noqa: F811
    measured = rig.add("measured")
    fresh = rig.add("fresh")
    store.put(measured.id)
    client = rig.client()
    listed = {c["name"]: c for c in client.get("/candybars").json()}
    for item in listed.values():
        assert_contract_shape(item, LIST_KEYS)
        assert isinstance(item["cost"], dict) and item["cost"]["tier"]
    assert listed["measured"]["cost"]["runs"] == 100
    assert listed["fresh"]["cost"]["tier"] == "unmeasured"  # not aggregated yet: not zero, not green
    assert listed["fresh"]["cost"]["computedAt"] is None
    detail = client.get(f"/candybars/{fresh.id}").json()
    assert_contract_shape(detail, SINGLE_KEYS)
    assert detail["cost"]["tier"] == "unmeasured"


def test_every_single_definition_response_carries_it(rig: Rig, store: CostStore) -> None:  # noqa: F811
    row = rig.add("everywhere")
    client = rig.client()
    registered = client.post(
        "/candybars", json={"name": "new-one", "schedule": "0 * * * *", "entrypoint": "/bin/true"}
    )
    assert registered.status_code == 201 and registered.json()["cost"]["tier"] == "unmeasured"
    for response in (
        client.patch(f"/candybars/{row.id}", json={"description": "changed"}),
        client.patch(f"/candybars/{row.id}", json={"description": "changed"}),  # the no-op path
        client.post(f"/candybars/{row.id}/enabled", json={"enabled": False}),
        client.post(f"/candybars/{row.id}/enabled", json={"enabled": False}),  # already that state
        client.put(f"/candybars/{row.id}/noise"),
    ):
        assert response.status_code == 200, response.text
        assert response.json()["cost"]["tier"] == "unmeasured"


# --- derivation at read time ------------------------------------------------------


def test_the_projection_is_the_per_run_average_times_the_schedules_fires(rig: Rig, store: CostStore) -> None:  # noqa: F811
    every_5 = rig.add("often", schedule="*/5 * * * *")
    hourly = rig.add("hourly", schedule="0 * * * *")
    for row in (every_5, hourly):
        store.put(row.id)  # 20 core-s a run
    body = {c["name"]: c["cost"] for c in rig.client().get("/candybars").json()}
    assert body["often"]["firesPerDay"] == 288.0
    assert body["often"]["perDay"]["cpuS"] == 5760.0
    assert body["often"]["tier"] == "red" and body["often"]["driver"] == "cpu"
    assert body["hourly"]["perDay"]["cpuS"] == 480.0
    assert body["hourly"]["tier"] == "yellow"
    # Changing a schedule needs no recompute: the same totals, a new projection.
    rig.rows[hourly.id].schedule = "12 3 * * *"
    assert rig.client().get(f"/candybars/{hourly.id}").json()["cost"]["perDay"]["cpuS"] == 20.0


def test_a_runner_candybar_reads_its_kind_from_the_runner(rig: Rig, store: CostStore) -> None:  # noqa: F811
    flow = rig.add("flow", runner={"kind": "n8n", "webhook": "http://n8n.test/webhook/x"})
    store.put(flow.id)
    cost = rig.client().get(f"/candybars/{flow.id}").json()["cost"]
    assert cost["tiers"]["cpu"] == "unmeasured"
    assert any("n8n runs the workflow itself" in n for n in cost["notes"])


def test_the_cost_route_adds_thresholds_and_what_ifs(rig: Rig, store: CostStore) -> None:  # noqa: F811
    row = rig.add("often", schedule="*/5 * * * *")
    store.put(row.id)
    body = rig.client().get(f"/candybars/{row.id}/cost").json()
    assert body["name"] == "often" and body["candybarId"] == str(row.id)
    assert body["cost"]["tier"] == "red"
    assert body["thresholds"]["cpu"] == {"yellow": 300.0, "red": 1800.0, "unit": "core-s/day"}
    assert set(body["thresholds"]) == set(costs.DIMENSIONS)
    by_label = {v["label"]: v for v in body["whatIf"]}
    assert by_label["daily"]["tier"] == "green" and by_label["daily"]["perDay"]["cpuS"] == 20.0
    assert by_label["hourly"]["tier"] == "yellow"
    assert by_label["every 15 min"]["tier"] == "red"  # 96 x 20 = 1920 core-s a day
    assert body["configError"] is None


def test_the_cost_route_of_an_unknown_candybar_is_a_404_problem(rig: Rig, store: CostStore) -> None:  # noqa: F811
    response = rig.client().get(f"/candybars/{uuid4()}/cost")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_costs_ranks_most_expensive_first_with_unmeasured_last(rig: Rig, store: CostStore) -> None:  # noqa: F811
    cheap = rig.add("cheap", schedule="12 3 * * *")
    hot = rig.add("hot", schedule="*/5 * * * *")
    warm = rig.add("warm", schedule="0 * * * *")
    rig.add("never-ran", schedule="0 * * * *")
    store.put(cheap.id)
    store.put(hot.id)
    store.put(warm.id)
    body = rig.client().get("/costs").json()
    assert [c["name"] for c in body["candybars"]] == ["hot", "warm", "cheap", "never-ran"]
    assert [c["cost"]["tier"] for c in body["candybars"]] == ["red", "yellow", "green", "unmeasured"]
    assert body["windowDays"] == 7 and body["thresholds"]["usd"]["red"] == 1.0
    first = body["candybars"][0]
    assert {"id", "name", "schedule", "timezone", "enabled", "archived", "noise", "tags"} <= set(first)


# --- config -----------------------------------------------------------------------


def test_thresholds_come_from_the_env(rig: Rig, store: CostStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv(costs.THRESHOLDS_ENV, '{"cpu": [5, 10]}')
    row = rig.add("hourly", schedule="0 * * * *")
    store.put(row.id)  # 480 core-s/day
    body = rig.client().get(f"/candybars/{row.id}/cost").json()
    assert body["thresholds"]["cpu"]["red"] == 10.0 and body["cost"]["tiers"]["cpu"] == "red"
    assert body["thresholds"]["tokens"]["red"] == 2_000_000.0  # the rest stay at the defaults


def test_a_bad_threshold_override_does_not_stop_the_api_and_is_reported(
    rig: Rig, store: CostStore, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    monkeypatch.setenv(costs.THRESHOLDS_ENV, "{broken")
    row = rig.add("hourly", schedule="0 * * * *")
    store.put(row.id)
    client = rig.client()
    body = client.get(f"/candybars/{row.id}/cost").json()
    assert "not JSON" in body["configError"]
    assert body["thresholds"]["cpu"]["red"] == 1800.0  # the defaults
    assert client.get("/candybars").status_code == 200
    assert "not JSON" in client.get("/costs").json()["configError"]


def test_a_bad_window_setting_falls_back_to_a_week(rig: Rig, store: CostStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv(costs.WINDOW_DAYS_ENV, "ninety")
    body = rig.client().get("/costs").json()
    assert body["windowDays"] == 7 and "not an integer" in body["configError"]


def test_the_window_comes_from_the_env_and_a_stored_row_keeps_its_own(rig: Rig, store: CostStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv(costs.WINDOW_DAYS_ENV, "14")
    stale = rig.add("stale")
    rig.add("fresh")
    store.put(stale.id, window_days=7)
    body = {c["name"]: c["cost"] for c in rig.client().get("/candybars").json()}
    assert body["stale"]["windowDays"] == 7  # the totals were computed over 7 days
    assert body["fresh"]["windowDays"] == 14
    assert rig.client().get("/costs").json()["windowDays"] == 14


# --- refresh ---------------------------------------------------------------------


def test_refresh_all_and_one(rig: Rig, store: CostStore, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    monkeypatch.setenv(costs.WINDOW_DAYS_ENV, "10")
    one = rig.add("one")
    rig.add("two")
    client = rig.client()
    assert client.post("/costs/refresh").json() == {"refreshed": 2, "windowDays": 10}
    assert client.post(f"/costs/refresh?candybarId={one.id}").json() == {
        "refreshed": 1, "windowDays": 10,
    }
    assert store.refreshed == [(None, 10), (one.id, 10)]
    assert client.post(f"/costs/refresh?candybarId={uuid4()}").status_code == 404
    assert client.post("/costs/refresh?candybarId=nope").status_code == 422
