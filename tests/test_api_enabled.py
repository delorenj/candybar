"""Story 7.5 (AD-21): the `enabled` flag over the API.

`POST /candybars/{id}/enabled` `{enabled}` sets the flag through the noise
flow (lock, one `updated` fact with `changes: {enabled}` FIRST, then the
write); register accepts `enabled: false` and the `created` fact carries it;
the definition shape lists `enabled`. A disabled candybar has no `current`
or `upcoming` `/schedule` row, and of its past fires only those that ran
are rows. The in-memory rig is Story 5.1's (tests/test_api_definitions.py);
the live-Postgres round trip is tests/test_enabled_integration.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.api.schedule import (
    STATE_PAST,
    WindowCandybar,
    attach_results,
    fire_key,
    next_row,
    visible_rows,
    window_rows,
)
from contract_shapes import (
    LIST_KEYS,
    SINGLE_KEYS,
    assert_contract_shape,
    assert_schedule_row,
)
from test_api_definitions import T0, Rig, rig  # noqa: F401  (fixture)

PROBLEM = "application/problem+json"
UPDATED = "bloodbank.cron.candybar.updated"
CREATED = "bloodbank.cron.candybar.created"
NOW = T0 + timedelta(minutes=30)  # 06:30Z


@pytest.fixture()
def wired(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:  # noqa: F811
    rig.results: dict[str, str] = {}  # type: ignore[attr-defined]

    async def list_skips(session: Any, start: Any, end: Any = None) -> list[Any]:
        return []

    async def fire_results(session: Any, keys: Any) -> dict[str, str]:
        return {k: v for k, v in rig.results.items() if k in set(keys)}  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "fire_results", fire_results)
    return rig


def client(rig: Rig, *, raise_server_exceptions: bool = True) -> TestClient:  # noqa: F811
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    app = create_app(session_factory=session_factory, sink=rig.sink, now=lambda: NOW)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def test_disable_publishes_one_fact_then_writes(wired: Rig) -> None:
    row = wired.add("alpha")
    resp = client(wired).post(f"/candybars/{row.id}/enabled", json={"enabled": False})
    assert resp.status_code == 200
    assert_contract_shape(resp.json(), SINGLE_KEYS)
    assert resp.json()["enabled"] is False
    assert row.enabled is False and wired.writes == 1
    [fact] = wired.sink.published
    assert fact.type == UPDATED and fact.data.changes == {"enabled": False}
    assert fact.ordering_key == f"candybar:{row.id}"


def test_setting_the_current_state_is_a_no_op(wired: Rig) -> None:
    row = wired.add("alpha")
    resp = client(wired).post(f"/candybars/{row.id}/enabled", json={"enabled": True})
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert wired.sink.published == [] and wired.writes == 0


def test_enable_round_trip(wired: Rig) -> None:
    row = wired.add("alpha", enabled=False)
    resp = client(wired).post(f"/candybars/{row.id}/enabled", json={"enabled": True})
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert [e.data.changes for e in wired.sink.published] == [{"enabled": True}]


def test_unknown_candybar_is_a_404(wired: Rig) -> None:
    resp = client(wired).post(f"/candybars/{uuid4()}/enabled", json={"enabled": False})
    assert resp.status_code == 404 and resp.headers["content-type"] == PROBLEM


@pytest.mark.parametrize("body", [{}, {"enabled": "nope"}, {"enabled": False, "extra": 1}, {"enabled": None}])
def test_body_is_exactly_the_wanted_state(wired: Rig, body: dict[str, Any]) -> None:
    row = wired.add("alpha")
    resp = client(wired).post(f"/candybars/{row.id}/enabled", json=body)
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert row.enabled is True and wired.sink.published == []


def test_bus_failure_leaves_the_flag(wired: Rig) -> None:
    row = wired.add("alpha")
    wired.sink.fail = True
    resp = client(wired).post(f"/candybars/{row.id}/enabled", json={"enabled": False})
    assert resp.status_code == 503 and "enabled flag is unchanged" in resp.json()["detail"]
    assert row.enabled is True and wired.writes == 0


def test_register_disabled_carries_the_flag_on_the_fact_and_the_row(wired: Rig) -> None:
    resp = client(wired).post(
        "/candybars",
        json={"name": "migrant", "schedule": "23 5 * * *", "timezone": "America/New_York",
              "runner": {"kind": "systemd", "unit": "cache-prune-daily.service"},
              "enabled": False},
    )
    assert resp.status_code == 201
    assert resp.json()["enabled"] is False
    [fact] = wired.sink.published
    assert fact.type == CREATED and fact.data.enabled is False
    listed = client(wired).get("/candybars").json()
    [item] = listed
    assert_contract_shape(item, LIST_KEYS)
    assert item["enabled"] is False


def test_register_defaults_to_enabled(wired: Rig) -> None:
    resp = client(wired).post(
        "/candybars", json={"name": "plain", "schedule": "* * * * *", "entrypoint": "/bin/true"}
    )
    assert resp.status_code == 201 and resp.json()["enabled"] is True
    assert wired.sink.published[0].data.enabled is True


def test_patch_cannot_touch_enabled(wired: Rig) -> None:
    row = wired.add("alpha")
    resp = client(wired).patch(f"/candybars/{row.id}", json={"enabled": False})
    assert resp.status_code == 422  # the dedicated route owns it
    assert row.enabled is True


# ---------------------------------------------------------------------------
# /schedule and /schedule/next.


def _window(rig: Rig, start: str, end: str) -> list[dict[str, Any]]:  # noqa: F811
    resp = client(rig).get("/schedule", params={"from": start, "to": end})
    assert resp.status_code == 200
    for item in resp.json():
        assert_schedule_row(item)
    return resp.json()


def test_disabled_candybar_has_no_current_or_upcoming_rows(wired: Rig) -> None:
    on = wired.add("on", schedule="*/10 * * * *")
    off = wired.add("off", schedule="*/10 * * * *", enabled=False)
    rows = _window(wired, "2026-10-26T06:00:00Z", "2026-10-26T07:00:00Z")
    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in rows:
        by_name.setdefault(item["name"], []).append(item)
    assert {r["state"] for r in by_name["on"]} == {"past", "current", "upcoming"}
    assert "off" not in by_name  # past fires never ran, no future fires at all
    # A past fire that DID run (while it was enabled) is still a row.
    wired.results[fire_key(off.id, T0 + timedelta(minutes=10))] = "success"  # type: ignore[attr-defined]
    rows = _window(wired, "2026-10-26T06:00:00Z", "2026-10-26T07:00:00Z")
    off_rows = [r for r in rows if r["name"] == "off"]
    assert [(r["fireTime"], r["state"], r.get("lastResult")) for r in off_rows] == [
        ("2026-10-26T06:10:00+00:00", "past", "success")
    ]
    assert on.enabled


def test_next_skips_disabled_candybars(wired: Rig) -> None:
    wired.add("soon-off", schedule="31 6 * * *", enabled=False)
    wired.add("later-on", schedule="45 6 * * *")
    resp = client(wired).get("/schedule/next")
    assert resp.status_code == 200 and resp.json()["name"] == "later-on"


def test_only_disabled_candybars_means_no_next(wired: Rig) -> None:
    wired.add("off", schedule="* * * * *", enabled=False)
    assert client(wired).get("/schedule/next").status_code == 404


def test_pure_helpers_agree() -> None:
    off = WindowCandybar(id=uuid4(), name="off", schedule="*/15 * * * *", noise=False,
                         created_at=T0, enabled=False)
    on = WindowCandybar(id=uuid4(), name="on", schedule="*/15 * * * *", noise=False,
                        created_at=T0)
    rows = window_rows([off, on], T0, T0 + timedelta(hours=1), now=NOW, skips={})
    assert {r.state for r in rows if r.name == "off"} == {STATE_PAST}
    assert next_row([off, on], now=NOW, skips={}).name == "on"
    ran = fire_key(off.id, T0 + timedelta(minutes=15))
    visible = visible_rows(attach_results(rows, {ran: "fail"}))
    assert [(r.name, r.last_result) for r in visible if r.name == "off"] == [("off", "fail")]
