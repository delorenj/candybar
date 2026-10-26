"""Story 5.2: `GET /schedule?from&to` — ordered past/current/upcoming rows.

Registry reads are monkeypatched at the app module with an in-memory rig
(the pattern of test_api_definitions.py); the live-Postgres round trip of the
same route is tests/test_schedule_window_integration.py. Every row any test
receives is checked against the contract row parsed from
holocene-view-contract.md, and one test drives the REAL dispatcher over a
window's ticks to prove the rows' fire times and `skipped` flags are exactly
its commands and `skipped` facts (AD-13).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.api.schedule import (
    MAX_ROWS,
    MAX_WINDOW,
    STATE_CURRENT,
    STATE_PAST,
    STATE_UPCOMING,
    ScheduleRow,
    WindowCandybar,
    WindowError,
    attach_results,
    fire_key,
    first_tick_after,
    window_bounds,
    window_rows,
)
from candybar.dispatcher import Dispatcher
from candybar.dispatcher.schedule import group_skips, matches_tick
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand, CandybarSkippedEvent
from candybar.events.vocabulary import scheduled_idempotency_key, tick_id_for_minute
from candybar.registry import SKIP_OCCURRENCE, SKIP_REST_OF_DAY, SkipEntry
from candybar.registry.store import ScheduleEntry
from contract_shapes import (
    RESULT_VALUES,
    SCHEDULE_OPTIONAL_KEYS,
    SCHEDULE_REQUIRED_KEYS,
    SCHEDULE_ROW_FIELDS,
    STATE_VALUES,
    assert_schedule_row,
)

PROBLEM = "application/problem+json"
DAY = datetime(2026, 10, 26, tzinfo=UTC)


def at(hour: int, minute: int = 0, second: int = 0, *, day: int = 26) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


def z(instant: datetime) -> str:
    return instant.isoformat().replace("+00:00", "Z")


def test_contract_parse_found_the_documented_row() -> None:
    assert SCHEDULE_REQUIRED_KEYS == {
        "candybarId", "name", "schedule", "fireTime", "state", "skipped", "noise",
    }
    assert SCHEDULE_OPTIONAL_KEYS == {"lastResult"}
    assert STATE_VALUES == {"past", "current", "upcoming"}
    assert RESULT_VALUES == {"success", "fail"}
    assert SCHEDULE_ROW_FIELDS["skipped"][1] == "bool"
    # The module's state words are the contract's.
    assert {STATE_PAST, STATE_CURRENT, STATE_UPCOMING} == STATE_VALUES


# ---------------------------------------------------------------------------
# In-memory registry rig.


@dataclass
class Row:
    id: UUID
    name: str
    schedule: str
    noise: bool
    created_at: datetime


@dataclass
class Rig:
    rows: list[Row] = field(default_factory=list)
    skips: list[SkipEntry] = field(default_factory=list)
    results: dict[str, str] = field(default_factory=dict)
    skip_reads: list[tuple[datetime, datetime]] = field(default_factory=list)
    result_reads: list[list[str]] = field(default_factory=list)
    now: datetime = datetime(2026, 10, 26, 10, 12, 30, tzinfo=UTC)
    db_down: bool = False

    def add(self, name: str, schedule: str, **kw: Any) -> Row:
        row = Row(
            id=kw.pop("id", uuid4()),
            name=name,
            schedule=schedule,
            noise=kw.pop("noise", False),
            created_at=kw.pop("created_at", DAY - timedelta(days=30)),
        )
        self.rows.append(row)
        return row

    def skip(self, row: Row, fire: datetime) -> None:
        self.skips.append(
            SkipEntry(uuid4(), row.id, SKIP_OCCURRENCE, fire, None, None, DAY)
        )

    def skip_day(self, row: Row, day: date) -> None:
        self.skips.append(SkipEntry(uuid4(), row.id, SKIP_REST_OF_DAY, None, day, None, DAY))

    def ran(self, row: Row, fire: datetime, result: str) -> None:
        self.results[fire_key(row.id, fire)] = result

    def client(self) -> TestClient:
        @asynccontextmanager
        async def session_factory() -> AsyncIterator[Any]:
            yield object()

        return TestClient(
            create_app(session_factory=session_factory, sink=None, now=lambda: self.now),
            raise_server_exceptions=False,
        )

    def get(self, start: str, end: str) -> list[dict[str, Any]]:
        response = self.client().get("/schedule", params={"from": start, "to": end})
        assert response.status_code == 200, response.text
        rows = response.json()
        assert isinstance(rows, list)
        for row in rows:
            assert_schedule_row(row)
        return rows


def skips_in(entries: list[SkipEntry], start: datetime, end: datetime) -> list[SkipEntry]:
    """The store's list_skips semantics: occurrence rows in [start, end), day rows intersecting."""
    last = (end - timedelta(minutes=1)).date()
    return [
        s
        for s in entries
        if (s.kind == SKIP_OCCURRENCE and start <= s.fire_time < end)
        or (s.kind == SKIP_REST_OF_DAY and start.date() <= s.day <= last)
    ]


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig()

    async def list_candybars(session: Any) -> list[Row]:
        if rig.db_down:
            raise OSError("connection refused")
        return sorted(rig.rows, key=lambda r: (r.created_at, r.id))

    async def list_skips(session: Any, start: datetime, end: datetime) -> list[SkipEntry]:
        rig.skip_reads.append((start, end))
        return skips_in(rig.skips, start, end)

    async def fire_results(session: Any, keys: list[str]) -> dict[str, str]:
        rig.result_reads.append(list(keys))
        return {k: rig.results[k] for k in keys if k in rig.results}

    monkeypatch.setattr(app_module, "list_candybars", list_candybars)
    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "fire_results", fire_results)
    return rig


def brief(rows: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """(HH:MM, name, state) per row — the readable shape of a window."""
    return [(datetime.fromisoformat(r["fireTime"]).strftime("%H:%M"), r["name"], r["state"]) for r in rows]


# ---------------------------------------------------------------------------
# Matrix rows.


def test_day_window_orders_rows_and_marks_exactly_one_current(rig: Rig) -> None:
    # bravo registered first: row order must still be (fireTime, name).
    bravo = rig.add("bravo", "*/15 * * * *", created_at=DAY - timedelta(days=2))
    alpha = rig.add("alpha", "*/5 * * * *", noise=True)
    rows = rig.get(z(at(10)), z(at(10, 30)))
    assert brief(rows) == [
        ("10:00", "alpha", "past"),
        ("10:00", "bravo", "past"),
        ("10:05", "alpha", "past"),
        ("10:10", "alpha", "past"),
        ("10:15", "alpha", "current"),  # name tie-break over bravo@10:15
        ("10:15", "bravo", "upcoming"),
        ("10:20", "alpha", "upcoming"),
        ("10:25", "alpha", "upcoming"),  # 10:30 is outside the half-open window
    ]
    assert [r["state"] for r in rows].count("current") == 1
    assert {r["candybarId"] for r in rows if r["name"] == "alpha"} == {str(alpha.id)}
    assert all(r["noise"] is (r["name"] == "alpha") for r in rows)
    assert all(r["schedule"] == ("*/5 * * * *" if r["name"] == "alpha" else "*/15 * * * *") for r in rows)
    assert all(r["skipped"] is False for r in rows)
    assert all("lastResult" not in r for r in rows)  # nothing ran: omitted, never null
    assert rows[0]["fireTime"] == "2026-10-26T10:00:00+00:00"
    assert bravo.id != alpha.id


def test_skipped_next_fire_is_upcoming_and_current_moves_on(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    rig.add("bravo", "*/15 * * * *")
    rig.skip(alpha, at(10, 15))
    rows = rig.get(z(at(10)), z(at(10, 30)))
    by = {(t, n): (s, r) for (t, n, s), r in zip(brief(rows), rows, strict=True)}
    assert by[("10:15", "alpha")][0] == "upcoming" and by[("10:15", "alpha")][1]["skipped"] is True
    assert by[("10:15", "bravo")][0] == "current" and by[("10:15", "bravo")][1]["skipped"] is False
    # Skipped fires are listed, never silently dropped.
    assert len(rows) == 8


def test_current_skips_past_every_skipped_fire(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    for minute in (15, 20):
        rig.skip(alpha, at(10, minute))
    rows = rig.get(z(at(10, 13)), z(at(10, 30)))
    assert brief(rows) == [
        ("10:15", "alpha", "upcoming"),
        ("10:20", "alpha", "upcoming"),
        ("10:25", "alpha", "current"),
    ]
    assert [r["skipped"] for r in rows] == [True, True, False]


def test_rest_of_day_skip_marks_the_whole_utc_date(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/10 * * * *")
    rig.skip_day(alpha, date(2026, 10, 26))
    rig.now = at(23, 31)
    rows = rig.get(z(at(23, 30)), z(at(0, 30, day=27)))
    assert brief(rows) == [
        ("23:30", "alpha", "past"),
        ("23:40", "alpha", "upcoming"),
        ("23:50", "alpha", "upcoming"),
        ("00:00", "alpha", "current"),  # the next UTC day fires normally
        ("00:10", "alpha", "upcoming"),
        ("00:20", "alpha", "upcoming"),
    ]
    assert [r["skipped"] for r in rows] == [True, True, True, False, False, False]


def test_past_rows_carry_last_result_from_the_invocation_key(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    bravo = rig.add("bravo", "*/15 * * * *")
    rig.ran(alpha, at(10), "success")  # scheduled
    rig.ran(alpha, at(10, 5), "fail")  # misfire: same key as scheduled
    # A manual run is not a schedule row and never resolves one.
    rig.results[f"candybar:{bravo.id}:manual:{uuid4()}"] = "success"
    # An upcoming fire's key is never read, even if one somehow existed.
    rig.ran(alpha, at(10, 20), "success")
    rows = rig.get(z(at(10)), z(at(10, 30)))
    results = {(t, n): r.get("lastResult") for (t, n, _), r in zip(brief(rows), rows, strict=True)}
    assert results[("10:00", "alpha")] == "success"
    assert results[("10:05", "alpha")] == "fail"
    assert results[("10:00", "bravo")] is None and results[("10:10", "alpha")] is None
    assert results[("10:20", "alpha")] is None
    assert all("lastResult" not in r for r in rows if r["state"] != "past")
    # Only past keys were looked up, once, in row order.
    assert rig.result_reads == [
        [
            fire_key(alpha.id, at(10)),
            fire_key(bravo.id, at(10)),
            fire_key(alpha.id, at(10, 5)),
            fire_key(alpha.id, at(10, 10)),
        ]
    ]
    # The key is the dispatcher's scheduled/misfire idempotency key.
    tick = tick_id_for_minute(int(at(10).timestamp()) // 60)
    assert fire_key(alpha.id, at(10)) == f"candybar:{alpha.id}:tick:{tick}"
    assert fire_key(alpha.id, at(10)) == scheduled_idempotency_key(alpha.id, tick)


def test_a_skip_stored_after_the_fire_ran_does_not_rewrite_history(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    rig.skip(alpha, at(10, 5))
    rig.skip(alpha, at(10, 10))
    rig.ran(alpha, at(10, 5), "success")  # it ran before the skip landed
    rows = rig.get(z(at(10, 5)), z(at(10, 15)))
    assert [(r["skipped"], r.get("lastResult")) for r in rows] == [
        (False, "success"),
        (True, None),  # suppressed: no invocation recorded
    ]


def test_past_window_has_no_current_row(rig: Rig) -> None:
    rig.add("alpha", "*/15 * * * *")
    rows = rig.get(z(at(8)), z(at(9)))
    assert [r["state"] for r in rows] == ["past"] * 4


def test_future_window_has_no_current_when_an_earlier_fire_comes_first(rig: Rig) -> None:
    rig.add("alpha", "*/5 * * * *")
    rows = rig.get(z(at(11)), z(at(11, 15)))
    assert [r["state"] for r in rows] == ["upcoming"] * 3
    assert rig.result_reads == []  # no past rows, no result query
    assert rig.skip_reads == [(at(10, 13), at(11, 15))]  # [cut, end)


def test_future_window_current_when_every_earlier_fire_is_skipped(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    for minute in range(15, 60, 5):
        rig.skip(alpha, at(10, minute))
    rows = rig.get(z(at(11)), z(at(11, 15)))
    assert [r["state"] for r in rows] == ["current", "upcoming", "upcoming"]


def test_rows_start_at_registration(rig: Rig) -> None:
    rig.add("alpha", "*/5 * * * *", created_at=at(10, 7, 20))
    rig.add("bravo", "*/5 * * * *", created_at=at(10, 10))
    rows = rig.get(z(at(10)), z(at(10, 15)))
    assert brief(rows) == [("10:10", "alpha", "past"), ("10:10", "bravo", "past")]


def test_odd_bounds_and_offsets(rig: Rig) -> None:
    rig.add("alpha", "*/5 * * * *")
    rows = rig.get("2026-10-26T06:00:30-04:00", "2026-10-26T10:30:30Z")
    times = [t for t, _, _ in brief(rows)]
    assert times[0] == "10:05" and times[-1] == "10:30"  # 10:00 < from, 10:30 < to
    assert all(r["fireTime"].endswith("+00:00") for r in rows)


def test_empty_registry_is_an_empty_array(rig: Rig) -> None:
    assert rig.get(z(at(10)), z(at(11))) == []
    assert rig.result_reads == []


def test_full_31_day_window_is_served(rig: Rig) -> None:
    rig.add("alpha", "0 * * * *")
    rows = rig.get(z(at(0)), z(at(0) + MAX_WINDOW))
    assert len(rows) == 31 * 24


@pytest.mark.parametrize(
    "params",
    [
        {"to": "2026-10-26T11:00:00Z"},
        {"from": "2026-10-26T10:00:00Z"},
        {"from": "2026-10-26T10:00:00", "to": "2026-10-26T11:00:00Z"},
        {"from": "2026-10-26T10:00:00Z", "to": "2026-10-26"},
        {"from": "soon", "to": "2026-10-26T11:00:00Z"},
        {"from": "2026-10-26T10:00:00Z", "to": "2026-10-26T10:00:00Z"},
        {"from": "2026-10-26T11:00:00Z", "to": "2026-10-26T10:00:00Z"},
        {"from": "2026-10-01T00:00:00Z", "to": "2026-11-01T00:01:00Z"},
        {"from": "9999-12-31T23:00:00Z", "to": "9999-12-31T23:59:30Z"},
    ],
)
def test_bad_bounds_are_422_problems(rig: Rig, params: dict[str, str]) -> None:
    rig.add("alpha", "*/5 * * * *")
    response = rig.client().get("/schedule", params=params)
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["status"] == 422


def test_too_many_rows_is_a_422_problem(rig: Rig) -> None:
    rig.add("alpha", "* * * * *")  # 14 days of minutes = 20,160 rows > MAX_ROWS
    response = rig.client().get(
        "/schedule", params={"from": z(at(0)), "to": z(at(0) + timedelta(days=14))}
    )
    assert MAX_ROWS < 14 * 1440
    assert response.status_code == 422 and response.headers["content-type"] == PROBLEM
    assert "narrow" in response.json()["detail"]


def test_db_down_is_a_500_problem(rig: Rig) -> None:
    rig.db_down = True
    response = rig.client().get("/schedule", params={"from": z(at(10)), "to": z(at(11))})
    assert response.status_code == 500 and response.headers["content-type"] == PROBLEM


# ---------------------------------------------------------------------------
# AD-13: the rows ARE the dispatcher's decisions for those ticks.


def test_rows_equal_what_the_dispatcher_publishes_for_each_tick(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    bravo = rig.add("bravo", "*/15 * * * *")
    charlie = rig.add("charlie", "7,22 10-11 * * *")
    delta = rig.add("delta", "0 0 30 2 *")  # valid but never fires
    # Day-of-month OR day-of-week (standard cron): the 26th, though not a
    # Saturday. Pins that croniter's match (dispatcher) and its iteration
    # (rows) agree on the DOM/DOW semantics.
    echo = rig.add("echo", "45 11 26 * 6")
    rig.skip(alpha, at(10, 15))
    rig.skip(bravo, at(10, 45))
    rig.skip_day(charlie, date(2026, 10, 26))
    rig.now = at(9, 59, 30)  # the whole window is ahead; 10:00 is the next tick
    start, end = at(10), at(12)
    rows = rig.get(z(start), z(end))

    entries = [ScheduleEntry(r.id, r.name, r.schedule, r.created_at) for r in rig.rows]
    published: list[Any] = []

    async def schedules() -> list[ScheduleEntry]:
        return entries

    async def publish(envelope: Any) -> None:
        published.append(parse_envelope(json.loads(envelope.model_dump_json())))

    async def skips(lo: datetime, hi: datetime) -> list[SkipEntry]:
        return skips_in(rig.skips, lo, hi)

    dispatcher = Dispatcher(schedules, publish, skips=skips)

    async def drive() -> None:
        minute = start
        while minute < end:
            await dispatcher.dispatch(build_tick_fired(minute))
            minute += timedelta(minutes=1)

    asyncio.run(drive())

    def minute_of(tick_id: str) -> str:
        return datetime.fromtimestamp(int(tick_id.split(":")[1]) * 60, UTC).isoformat()

    commands = {
        (str(e.data.candybar_id), minute_of(e.data.tick_id))
        for e in published
        if isinstance(e, CandybarInvokeCommand)
    }
    skipped = {
        (str(e.data.candybar_id), minute_of(e.data.tick_id))
        for e in published
        if isinstance(e, CandybarSkippedEvent)
    }
    assert commands == {(r["candybarId"], r["fireTime"]) for r in rows if not r["skipped"]}
    assert skipped == {(r["candybarId"], r["fireTime"]) for r in rows if r["skipped"]}
    assert len(skipped) == 6  # alpha 10:15, bravo 10:45, charlie 10:07 + 10:22 + 11:07 + 11:22
    assert not any(r["candybarId"] == str(delta.id) for r in rows)
    assert (str(echo.id), at(11, 45).isoformat()) in commands  # Monday the 26th fires
    for r in rows:
        schedule = r["schedule"]
        assert matches_tick(schedule, datetime.fromisoformat(r["fireTime"]))
    assert {r["state"] for r in rows} == {"current", "upcoming"}
    assert rows[0]["name"] == "alpha" and rows[0]["state"] == "current"
    assert str(charlie.id) in {cid for cid, _ in skipped}


# ---------------------------------------------------------------------------
# Pure helpers (no route).


def candybar(name: str, schedule: str) -> WindowCandybar:
    return WindowCandybar(uuid4(), name, schedule, False, DAY - timedelta(days=1))


def test_cut_is_the_first_tick_strictly_after_now() -> None:
    assert first_tick_after(at(10, 5)) == at(10, 6)  # the 10:05 fire is under way
    assert first_tick_after(at(10, 5, 59)) == at(10, 6)
    assert first_tick_after(datetime(2026, 10, 26, 6, 5, tzinfo=UTC).astimezone()) == at(6, 6)
    with pytest.raises(ValueError):
        first_tick_after(datetime(2026, 10, 26, 10, 5))


def test_window_bounds_validate_and_round_up() -> None:
    assert window_bounds(at(10, 0, 1), at(10, 30, 1)) == (at(10, 1), at(10, 31))
    with pytest.raises(WindowError):
        window_bounds(datetime(2026, 10, 26, 10), at(11))
    with pytest.raises(WindowError):
        window_bounds(at(11), at(11))


def test_window_rows_row_limit_is_exact() -> None:
    rows = window_rows([candybar("alpha", "* * * * *")], at(10), at(11), now=at(9), skips={}, limit=60)
    assert len(rows) == 60
    with pytest.raises(WindowError):
        window_rows([candybar("alpha", "* * * * *")], at(10), at(11), now=at(9), skips={}, limit=59)


def test_attach_results_touches_only_past_rows() -> None:
    alpha = candybar("alpha", "*/5 * * * *")
    rows = window_rows([alpha], at(10), at(10, 15), now=at(10, 6), skips={})
    results = {fire_key(alpha.id, at(10, f)): "fail" for f in (0, 5, 10)}
    out = attach_results(rows, results)
    assert [r.last_result for r in out] == ["fail", "fail", None]  # 10:10 is current
    assert out[2].state == STATE_CURRENT
    assert isinstance(out[0], ScheduleRow) and out[0].to_json()["lastResult"] == "fail"
    assert "lastResult" not in out[2].to_json()


def test_group_skips_feed_matches_the_dispatcher_fold() -> None:
    # The route folds skip rows with the dispatcher's own group_skips.
    alpha = candybar("alpha", "*/5 * * * *")
    entry = SkipEntry(uuid4(), alpha.id, SKIP_OCCURRENCE, at(10, 5), None, None, DAY)
    rows = window_rows(
        [alpha], at(10), at(10, 10), now=at(9), skips=group_skips([entry])
    )
    assert [r.skipped for r in rows] == [False, True]


def test_default_clock_is_the_utc_wall_clock(rig: Rig) -> None:
    # No `now` injected: the production path. A minutely candybar's current
    # row must be the first minute after the real wall clock.
    rig.add("alpha", "* * * * *")

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    client = TestClient(create_app(session_factory=session_factory, sink=None))
    before = datetime.now(UTC)
    response = client.get(
        "/schedule",
        params={"from": z(before - timedelta(minutes=3)), "to": z(before + timedelta(minutes=3))},
    )
    after = datetime.now(UTC)
    assert response.status_code == 200, response.text
    current = [r["fireTime"] for r in response.json() if r["state"] == "current"]
    assert current in (
        [first_tick_after(before).isoformat()],
        [first_tick_after(after).isoformat()],  # the request straddled a minute
    )


def test_scheduled_key_rejects_a_malformed_tick_id() -> None:
    # The tick-id guard moved out of build_candybar_invoke into the shared helper.
    from candybar.events.builders import build_candybar_invoke

    cid = uuid4()
    assert scheduled_idempotency_key(cid, "tick:42") == f"candybar:{cid}:tick:tick:42"
    for bad in ("42", "tick:", "tick:abc", "tick:-1"):
        with pytest.raises(ValueError, match="invalid tick_id"):
            scheduled_idempotency_key(cid, bad)
        with pytest.raises(ValueError, match="invalid tick_id"):
            build_candybar_invoke(candybar_id=cid, name="alpha", trigger="scheduled", tick_id=bad)
