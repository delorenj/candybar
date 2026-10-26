"""Story 5.6: the skip operations API, conformant to holocene-view-contract.md.

`POST /candybars/{id}/skip` `{fireTime}` stores one occurrence skip and
`POST /candybars/{id}/skip-rest-of-day` `{date}` stores one rest-of-day skip.
A target the schedule never produces is a 404. Each NEW skip publishes one
`bloodbank.cron.candybar.skipped` fact (reason = the skip kind) FIRST, then
the row is stored; the route's fact id is the store's deterministic skip id,
so a retry broker-dedupes against the first publish. The dispatcher's
per-tick skip read and `/schedule` honor the stored rows, and the SSE hub's
existing `_on_skipped` turns the route's fact into `skip.applied`.

The in-memory registry rig is Story 5.1's (tests/test_api_definitions.py);
the live round trip through the same routes, the real dispatcher and a real
stream is tests/test_skip_api_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import SkipDayRequest, SkipRequest, create_app
from candybar.api.schedule import first_tick_after
from candybar.api.stream import ScheduleStream
from candybar.registry import (
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    occurrence_skip_id,
    rest_of_day_skip_id,
)
from contract_shapes import skip_body_fields, skip_routes
from test_api_definitions import T0, Rig, Row, rig  # noqa: F401  (fixture)

PROBLEM = "application/problem+json"
SKIPPED = "bloodbank.cron.candybar.skipped"

DAY = date(2026, 10, 26)
# The routes are exercised "now" = Monday 2026-10-26 06:30 UTC (a weekday, so
# weekday schedules fire); `at()` builds times on that date.
NOW = T0 + timedelta(minutes=30)


def at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 10, 26, hour, minute, second, tzinfo=UTC)


def iso(instant: datetime) -> str:
    """A UTC instant the API's own way (`utc_iso`): `+00:00`, not `Z`."""
    return instant.astimezone(UTC).isoformat()


class StoredSkip:
    """The rig's record of one stored skip row (SkipRow-shaped: group_skips folds it)."""

    def __init__(
        self, candybar_id: UUID, kind: str, fire_time: datetime | None, day: date | None
    ) -> None:
        self.candybar_id = candybar_id
        self.kind = kind
        self.fire_time = fire_time
        self.day = day
        self.created_at = NOW
        self.reason = None


@pytest.fixture()
def skips_rig(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:  # noqa: F811
    """Story 5.1's rig plus in-memory skip writes and the /schedule reads.

    The store's contract — `(entry, created)`, idempotent, first row kept,
    and the same validation errors — is reproduced: the route's own checks
    must run BEFORE these fakes ever raise.
    """
    rig.stored: dict[UUID, StoredSkip] = {}  # type: ignore[attr-defined]
    rig.writes = 0  # reset; only skip writes counted below

    def _find(kind: str, candybar_id: UUID, fire_time: datetime | None, day: date | None) -> UUID:
        if kind == SKIP_OCCURRENCE:
            return occurrence_skip_id(candybar_id, fire_time)  # type: ignore[arg-type]
        return rest_of_day_skip_id(candybar_id, day)  # type: ignore[arg-type]

    async def add_occurrence_skip(
        session: Any, candybar_id: UUID, fire_time: datetime, *, reason: str | None = None
    ) -> tuple[Any, bool]:
        if candybar_id not in rig.rows:
            raise KeyError(str(candybar_id))
        sid = _find(SKIP_OCCURRENCE, candybar_id, fire_time, None)
        if sid in rig.stored:  # type: ignore[attr-defined]
            return rig.stored[sid], False  # type: ignore[attr-defined]
        entry = StoredSkip(candybar_id, SKIP_OCCURRENCE, fire_time.astimezone(UTC), None)
        rig.stored[sid] = entry  # type: ignore[attr-defined]
        rig.writes += 1
        return entry, True

    async def add_rest_of_day_skip(
        session: Any, candybar_id: UUID, day: date, *, reason: str | None = None
    ) -> tuple[Any, bool]:
        if isinstance(day, datetime) or not isinstance(day, date):
            raise ValueError("day must be a datetime.date (a UTC calendar date)")  # noqa: TRY004
        if candybar_id not in rig.rows:
            raise KeyError(str(candybar_id))
        sid = _find(SKIP_REST_OF_DAY, candybar_id, None, day)
        if sid in rig.stored:  # type: ignore[attr-defined]
            return rig.stored[sid], False  # type: ignore[attr-defined]
        entry = StoredSkip(candybar_id, SKIP_REST_OF_DAY, None, day)
        rig.stored[sid] = entry  # type: ignore[attr-defined]
        rig.writes += 1
        return entry, True

    async def list_skips(session: Any, start: Any, end: Any = None) -> list[Any]:
        # Occurrence rows in the window plus day rows intersecting it (2.4 rule).
        out = []
        for entry in rig.stored.values():  # type: ignore[attr-defined]
            if entry.fire_time is not None:
                if start <= entry.fire_time and (end is None or entry.fire_time < end):
                    out.append(entry)
            elif (
                entry.day >= start.date()
                and (end is None or entry.day <= (end - timedelta(minutes=1)).date())
            ):
                out.append(entry)
        return out

    async def fire_results(session: Any, keys: Any) -> dict[str, str]:
        return {}

    async def skip_stored(session: Any, skip_id: UUID) -> bool:
        return skip_id in rig.stored  # type: ignore[attr-defined]

    monkeypatch.setattr(app_module, "add_occurrence_skip", add_occurrence_skip)
    monkeypatch.setattr(app_module, "add_rest_of_day_skip", add_rest_of_day_skip)
    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "fire_results", fire_results)
    monkeypatch.setattr(app_module, "skip_stored", skip_stored)
    return rig


def client(rig: Rig, *, raise_server_exceptions: bool = True) -> TestClient:  # noqa: F811
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    app = create_app(session_factory=session_factory, sink=rig.sink, now=lambda: NOW)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def stored_of(rig: Rig, row: Row) -> list[StoredSkip]:  # noqa: F811
    candybar_stored = []
    for sid, entry in rig.stored.items():  # type: ignore[attr-defined]
        if sid in {
            occurrence_skip_id(row.id, entry.fire_time) if entry.fire_time else None,
            rest_of_day_skip_id(row.id, entry.day) if entry.day else None,
        }:
            candybar_stored.append(entry)
    return candybar_stored


def skip_facts(rig: Rig) -> list[Any]:  # noqa: F811
    return [e for e in rig.sink.published if e.type == SKIPPED]


# ---------------------------------------------------------------------------
# Contract conformance.


def test_app_serves_exactly_the_contracts_skip_routes(rig: Rig) -> None:  # noqa: F811
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    served = {
        (method, route.path.replace("{candybar_id}", "{id}"))
        for route in create_app(session_factory=session_factory, sink=rig.sink).routes
        if isinstance(route, APIRoute)
        for method in route.methods
        if "/skip" in route.path
    }
    assert served == skip_routes()
    assert skip_routes() == {
        ("POST", "/candybars/{id}/skip"),
        ("POST", "/candybars/{id}/skip-rest-of-day"),
    }


def test_request_models_match_the_contract_bodies() -> None:
    bodies = skip_body_fields()
    assert bodies["/candybars/{id}/skip"] == {"fireTime": "<ts>"}
    assert bodies["/candybars/{id}/skip-rest-of-day"] == {"date": "<YYYY-MM-DD>"}
    assert set(SkipRequest.model_fields) == set(bodies["/candybars/{id}/skip"])
    assert set(SkipDayRequest.model_fields) == set(bodies["/candybars/{id}/skip-rest-of-day"])


# ---------------------------------------------------------------------------
# Happy paths.


def test_skip_occurrence_stores_the_row_and_publishes_first(skips_rig: Rig) -> None:
    row = skips_rig.add("a", schedule="*/5 * * * *")
    fire = at(7, 5)
    writes_at_publish: list[int] = []

    original_publish = skips_rig.sink.publish

    async def publish(event: Any) -> None:
        writes_at_publish.append(skips_rig.writes)
        await original_publish(event)

    skips_rig.sink.publish = publish  # type: ignore[method-assign]
    response = client(skips_rig).post(f"/candybars/{row.id}/skip", json={"fireTime": iso(fire)})
    assert response.status_code == 200
    assert response.json() == {"skipped": True, "fireTime": iso(fire)}
    [entry] = stored_of(skips_rig, row)
    assert entry.kind == SKIP_OCCURRENCE and entry.fire_time == fire
    # Fact first: nothing was written when the publish ran.
    assert writes_at_publish == [0]
    [fact] = skip_facts(skips_rig)
    assert fact.data.candybar_id == row.id
    assert fact.data.reason == SKIP_OCCURRENCE
    assert fact.data.tick_id == f"tick:{int(fire.timestamp()) // 60}"
    # The fact's id is the store's deterministic skip id (broker-dedupable).
    assert fact.id == occurrence_skip_id(row.id, fire)


def test_skip_rest_of_day_stores_the_row_and_publishes_first(skips_rig: Rig) -> None:
    # Registration-minute alignment matters: a day row is found from the
    # first fire AT or after registration.
    row = Row(
        id=uuid4(),
        name="b",
        description="",
        tags=[],
        schedule="0 * * * *",  # hourly
        entrypoint="/bin/true",
        noise=False,
        created_at=datetime(2026, 10, 26, 0, 0, tzinfo=UTC),  # midnight
        updated_at=T0,
    )
    skips_rig.rows[row.id] = row
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip-rest-of-day", json={"date": DAY.isoformat()}
    )
    assert response.status_code == 200
    assert response.json() == {"skipped": True, "date": "2026-10-26"}
    [entry] = stored_of(skips_rig, row)
    assert entry.kind == SKIP_REST_OF_DAY and entry.day == DAY
    [fact] = skip_facts(skips_rig)
    assert fact.data.reason == SKIP_REST_OF_DAY
    # The day's FIRST fire names the fact's tick (00:00 for an hourly schedule).
    assert fact.data.tick_id == f"tick:{int(at(0).timestamp()) // 60}"
    assert fact.id == rest_of_day_skip_id(row.id, DAY)


def test_skip_reflected_in_schedule(skips_rig: Rig) -> None:
    row = skips_rig.add("c", schedule="*/5 * * * *")
    c = client(skips_rig)
    fire = at(7, 5)
    assert c.post(f"/candybars/{row.id}/skip", json={"fireTime": iso(fire)}).status_code == 200
    rows = c.get("/schedule", params={"from": iso(at(7)), "to": iso(at(8))}).json()
    mine = [r for r in rows if r["candybarId"] == str(row.id)]
    by_fire = {r["fireTime"]: r for r in mine}
    assert by_fire[iso(fire)]["skipped"] is True
    assert by_fire[iso(at(7, 0))]["skipped"] is False
    assert by_fire[iso(at(7, 10))]["skipped"] is False


def test_skip_day_reflected_in_schedule(skips_rig: Rig) -> None:
    row = skips_rig.add("d", schedule="0 * * * *")
    c = client(skips_rig)
    assert (
        c.post(f"/candybars/{row.id}/skip-rest-of-day", json={"date": DAY.isoformat()}).status_code
        == 200
    )
    rows = c.get("/schedule", params={"from": iso(at(7)), "to": iso(at(10))}).json()
    mine = [r for r in rows if r["candybarId"] == str(row.id)]
    assert mine and all(r["skipped"] is True for r in mine)
    # The following day is untouched.
    rows = c.get(
        "/schedule",
        params={"from": iso(at(0) + timedelta(days=1)), "to": iso(at(2) + timedelta(days=1))},
    ).json()
    mine = [r for r in rows if r["candybarId"] == str(row.id)]
    assert mine and all(r["skipped"] is False for r in mine)


# ---------------------------------------------------------------------------
# Idempotency.


def test_repeat_skip_publishes_nothing_and_stores_once(skips_rig: Rig) -> None:
    row = skips_rig.add("e", schedule="*/5 * * * *")
    c = client(skips_rig)
    body = {"fireTime": iso(at(7, 5))}
    assert c.post(f"/candybars/{row.id}/skip", json=body).status_code == 200
    assert c.post(f"/candybars/{row.id}/skip", json=body).status_code == 200
    assert len(stored_of(skips_rig, row)) == 1
    # The repeat publishes nothing at all: the route sees the stored row and
    # answers 200 from it (the deterministic id only matters for a concurrent
    # twin racing the check, which the broker dedupes).
    facts = skip_facts(skips_rig)
    assert len(facts) == 1


def test_skip_occurrence_coexisting_with_day_skip(skips_rig: Rig) -> None:
    row = skips_rig.add("f", schedule="*/5 * * * *")
    c = client(skips_rig)
    assert c.post(f"/candybars/{row.id}/skip", json={"fireTime": iso(at(7, 5))}).status_code == 200
    assert (
        c.post(f"/candybars/{row.id}/skip-rest-of-day", json={"date": DAY.isoformat()}).status_code
        == 200
    )
    entries = stored_of(skips_rig, row)
    assert {e.kind for e in entries} == {SKIP_OCCURRENCE, SKIP_REST_OF_DAY}


# ---------------------------------------------------------------------------
# 404s: nonexistent fireTime / id.


@pytest.mark.parametrize(
    "fire",
    [
        at(7, 7),  # off-schedule minute for */5
        at(5, 55),  # a real schedule fire but before registration (06:00)
    ],
    ids=["off-schedule", "pre-registration"],
)
def test_skip_no_such_fire_is_404(skips_rig: Rig, fire: datetime) -> None:
    row = skips_rig.add("g", schedule="*/5 * * * *")
    response = client(skips_rig).post(f"/candybars/{row.id}/skip", json={"fireTime": iso(fire)})
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)
    assert "never fires" in response.json()["detail"]
    assert stored_of(skips_rig, row) == [] and skip_facts(skips_rig) == []


def test_skip_fireless_day_is_404(skips_rig: Rig) -> None:
    row = skips_rig.add("h", schedule="0 9 * * 1-5")  # weekday 09:00
    sunday = date(2026, 10, 25)
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip-rest-of-day", json={"date": sunday.isoformat()}
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)
    assert stored_of(skips_rig, row) == [] and skip_facts(skips_rig) == []


def test_skip_day_before_registration_is_404(skips_rig: Rig) -> None:
    row = skips_rig.add("i", schedule="0 * * * *")
    before = date(2026, 10, 25)  # the candybar registered on the 26th
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip-rest-of-day", json={"date": before.isoformat()}
    )
    assert response.status_code == 404
    assert stored_of(skips_rig, row) == []


@pytest.mark.parametrize("path", ["skip", "skip-rest-of-day"])
def test_unknown_candybar_is_404(skips_rig: Rig, path: str) -> None:
    body = {"fireTime": iso(at(7))} if path == "skip" else {"date": DAY.isoformat()}
    response = client(skips_rig).post(f"/candybars/{uuid4()}/{path}", json=body)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith(PROBLEM)
    assert skip_facts(skips_rig) == []


@pytest.mark.parametrize("path", ["skip", "skip-rest-of-day"])
def test_malformed_id_is_422(skips_rig: Rig, path: str) -> None:
    body = {"fireTime": iso(at(7))} if path == "skip" else {"date": DAY.isoformat()}
    response = client(skips_rig).post(f"/candybars/not-a-uuid/{path}", json=body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM)


# ---------------------------------------------------------------------------
# 422s: bad bodies.


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"fireTime": None},
        {"fireTime": iso(at(7)), "note": "x"},
        {"fireTime": 12345},
        {"fireTime": "tomorrow"},
    ],
    ids=["missing", "null", "extra-key", "wrong-type", "garbage"],
)
def test_skip_bad_body_is_422(skips_rig: Rig, body: dict[str, Any]) -> None:
    row = skips_rig.add("j", schedule="*/5 * * * *")
    response = client(skips_rig).post(f"/candybars/{row.id}/skip", json=body)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM)
    assert stored_of(skips_rig, row) == [] and skip_facts(skips_rig) == []


def test_skip_naive_firetime_is_422(skips_rig: Rig) -> None:
    row = skips_rig.add("k", schedule="*/5 * * * *")
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip", json={"fireTime": "2026-10-26T07:05:00"}
    )
    assert response.status_code == 422
    assert stored_of(skips_rig, row) == []


def test_skip_off_minute_is_422(skips_rig: Rig) -> None:
    row = skips_rig.add("l", schedule="*/5 * * * *")
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip", json={"fireTime": iso(at(7, 5, 30))}
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith(PROBLEM)
    assert "minute" in response.json()["detail"]
    assert stored_of(skips_rig, row) == []


@pytest.mark.parametrize(
    "value",
    ["2026-13-40", "2026-10-26 10:00", "2026-10-26T10:00:00Z", None, 20261026],
    ids=["bad-date", "space-time", "iso-datetime", "null", "number"],
)
def test_skip_day_bad_date_is_422(skips_rig: Rig, value: Any) -> None:
    row = skips_rig.add("m", schedule="0 * * * *")
    body: dict[str, Any] = {"date": value}
    response = client(skips_rig).post(f"/candybars/{row.id}/skip-rest-of-day", json=body)
    assert response.status_code == 422
    assert stored_of(skips_rig, row) == [] and skip_facts(skips_rig) == []


def test_skip_day_extra_key_is_422(skips_rig: Rig) -> None:
    row = skips_rig.add("n", schedule="0 * * * *")
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip-rest-of-day", json={"date": DAY.isoformat(), "x": 1}
    )
    assert response.status_code == 422
    assert stored_of(skips_rig, row) == []


# ---------------------------------------------------------------------------
# Conversions and past fires.


def test_offset_firetime_is_converted_to_utc(skips_rig: Rig) -> None:
    row = skips_rig.add("o", schedule="0 10 * * *")
    response = client(skips_rig).post(
        f"/candybars/{row.id}/skip", json={"fireTime": "2026-10-26T12:00:00+02:00"}
    )
    assert response.status_code == 200
    assert response.json()["fireTime"] == iso(at(10))
    [entry] = stored_of(skips_rig, row)
    assert entry.fire_time == at(10)


def test_past_fire_stores_inertly(skips_rig: Rig) -> None:
    # NOW is 06:30; 06:05 is a real fire of this schedule that already passed.
    row = skips_rig.add("p", schedule="*/5 * * * *")
    response = client(skips_rig).post(f"/candybars/{row.id}/skip", json={"fireTime": iso(at(6, 5))})
    assert response.status_code == 200
    [entry] = stored_of(skips_rig, row)
    assert entry.fire_time == at(6, 5)


# ---------------------------------------------------------------------------
# Failure surfaces.


@pytest.mark.parametrize("path,body", [
    ("skip", {"fireTime": iso(at(7, 5))}),
    ("skip-rest-of-day", {"date": DAY.isoformat()}),
])
def test_bus_down_is_503_and_nothing_stored(
    skips_rig: Rig, path: str, body: dict[str, Any]
) -> None:
    row = skips_rig.add("q", schedule="*/5 * * * *")
    skips_rig.sink.fail = True
    response = client(skips_rig).post(f"/candybars/{row.id}/{path}", json=body)
    assert response.status_code == 503
    assert response.headers["content-type"].startswith(PROBLEM)
    assert stored_of(skips_rig, row) == []


@pytest.mark.parametrize("path,body", [
    ("skip", {"fireTime": iso(at(7, 5))}),
    ("skip-rest-of-day", {"date": DAY.isoformat()}),
])
def test_store_failure_is_500(
    skips_rig: Rig, path: str, body: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    row = skips_rig.add("r", schedule="*/5 * * * *")

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database down")

    monkeypatch.setattr(app_module, "add_occurrence_skip", boom)
    monkeypatch.setattr(app_module, "add_rest_of_day_skip", boom)
    response = client(skips_rig, raise_server_exceptions=False).post(
        f"/candybars/{row.id}/{path}", json=body
    )
    assert response.status_code == 500
    assert response.headers["content-type"].startswith(PROBLEM)
    assert stored_of(skips_rig, row) == []


# ---------------------------------------------------------------------------
# The stream: the route's fact must become `skip.applied` with no stream change.


def _stream_client(rig: Rig) -> tuple[TestClient, ScheduleStream]:  # noqa: F811
    hub = ScheduleStream(
        session_factory=_session_factory(rig),
        now=lambda: NOW,
    )

    app = create_app(
        session_factory=_session_factory(rig), sink=rig.sink, now=lambda: NOW, stream=hub
    )
    return TestClient(app), hub


def _session_factory(rig: Rig) -> Any:  # noqa: F811
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    return session_factory


@pytest.mark.parametrize(
    "path,body,expected_kind",
    [
        ("skip", {"fireTime": iso(at(7, 5))}, SKIP_OCCURRENCE),
        ("skip-rest-of-day", {"date": DAY.isoformat()}, SKIP_REST_OF_DAY),
    ],
)
def test_route_fact_feeds_skip_applied(
    rig: Rig,  # noqa: F811  (the imported fixture)
    path: str,
    body: dict[str, Any],
    expected_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Feed the route's REAL fact to a live hub BEFORE the row is stored: the
    stream emits `skip.applied` whose row already reads skipped:true, off the
    fact alone (the registry row does not exist yet).
    """
    row = rig.add("s", schedule="*/5 * * * *")

    async def noop_add(*args: Any, **kwargs: Any) -> tuple[Any, bool]:
        return StoredSkip(row.id, expected_kind, None, None), True

    async def list_skips(session: Any, start: Any, end: Any = None) -> list[Any]:
        return []

    async def not_stored(session: Any, skip_id: UUID) -> bool:
        return False

    monkeypatch.setattr(app_module, "add_occurrence_skip", noop_add)
    monkeypatch.setattr(app_module, "add_rest_of_day_skip", noop_add)
    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "skip_stored", not_stored)
    # The hub reads the registry itself (stream.py's own imports).
    async def hub_get(session: Any, candybar_id: UUID) -> Row | None:
        return rig.rows.get(candybar_id)

    async def hub_list(session: Any) -> list[Row]:
        return sorted(rig.rows.values(), key=lambda r: (r.created_at, r.id))

    monkeypatch.setattr("candybar.api.stream.list_skips", list_skips)
    monkeypatch.setattr("candybar.api.stream.get_candybar", hub_get)
    monkeypatch.setattr("candybar.api.stream.list_candybars", hub_list)

    c, hub = _stream_client(rig)
    sub_events: list[dict[str, Any]] = []

    async def collect() -> None:
        async for frame in hub.frames():
            if "event: skip.applied" in frame:
                sub_events.append(json.loads(frame.split("data: ", 1)[1]))
                return

    async def drive() -> None:
        await hub.start()
        collector = asyncio.ensure_future(collect())
        await asyncio.sleep(0)  # let the subscriber attach
        # TestClient is sync: run the request off the loop so the hub's
        # own awaits (hub.start's registry read) can complete.
        response = await asyncio.to_thread(c.post, f"/candybars/{row.id}/{path}", json=body)
        assert response.status_code == 200
        [fact] = [e for e in rig.sink.published if e.type == SKIPPED]
        await hub.handle(fact)
        await asyncio.wait_for(collector, timeout=5)
        hub.close()

    asyncio.run(drive())
    [event] = sub_events
    assert event["candybarId"] == str(row.id)
    assert event["reason"] == expected_kind
    assert event["row"] is not None and event["row"]["skipped"] is True


def test_schedule_current_recomputes_around_a_skip(skips_rig: Rig) -> None:
    """An upcoming fire once skipped is no longer `current`: the next row is."""
    row = skips_rig.add("t", schedule="*/5 * * * *")
    c = client(skips_rig)
    cut = first_tick_after(NOW)
    # `*/5` fires at 06:35 (the next fire after NOW=06:30), then 06:40.
    first_fire = datetime(2026, 10, 26, 6, 35, tzinfo=UTC)
    assert cut == datetime(2026, 10, 26, 6, 31, tzinfo=UTC)
    before = c.get("/schedule", params={"from": iso(NOW), "to": iso(at(8))}).json()
    current = [r for r in before if r["state"] == "current"]
    assert len(current) == 1 and current[0]["fireTime"] == iso(first_fire)
    assert c.post(
        f"/candybars/{row.id}/skip", json={"fireTime": iso(first_fire)}
    ).status_code == 200
    after = c.get("/schedule", params={"from": iso(NOW), "to": iso(at(8))}).json()
    mine = [r for r in after if r["candybarId"] == str(row.id)]
    current = [r for r in mine if r["state"] == "current"]
    assert len(current) == 1 and current[0]["fireTime"] == iso(at(6, 40))
    assert current[0]["skipped"] is False
    skipped = [r for r in mine if r["skipped"]]
    assert [r["fireTime"] for r in skipped] == [iso(first_fire)]
