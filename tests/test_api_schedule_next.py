"""Story 5.3: `GET /schedule/next` — the single current row, the stream's poll fallback.

The registry reads are monkeypatched at the app module with test_api_schedule's
in-memory rig. Every row checked here is checked against the contract row
parsed from holocene-view-contract.md, and against the `current` row
`GET /schedule` returns for a window containing it. The bounded-work claim
("without a full window scan") is proven three ways: a croniter step count,
a route that never reaches the window walk, and the stated latency budget.
The live-Postgres proof (two statements, no `invocations`) is
tests/test_schedule_next_integration.py.
"""

from __future__ import annotations

import random
import statistics
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
import candybar.api.schedule as schedule_module
import candybar.dispatcher.schedule as dispatcher_schedule
from candybar.api.app import create_app
from candybar.api.schedule import (
    STATE_CURRENT,
    WindowCandybar,
    first_tick_after,
    global_next,
    next_row,
    window_rows,
)
from candybar.dispatcher.schedule import ceil_minute, fires_between, group_skips
from candybar.registry import SKIP_OCCURRENCE, SKIP_REST_OF_DAY, SkipEntry
from contract_shapes import NEXT_ROUTE, assert_schedule_row, contract_route_line
from test_api_schedule import DAY, Rig, at, skips_in, z

PROBLEM = "application/problem+json"
# The stated latency budget (spec): median of 5 requests for 200 candybars.
RIG_BUDGET_SECONDS = 0.050
BUDGET_CANDYBARS = 200
TODAY = date(2026, 10, 26)


def skips_from(entries: list[SkipEntry], start: datetime, end: datetime | None) -> list[SkipEntry]:
    """The store's list_skips semantics, including the open-ended `end=None` read."""
    if end is not None:
        return skips_in(entries, start, end)
    return [
        s
        for s in entries
        if (s.kind == SKIP_OCCURRENCE and s.fire_time >= start)
        or (s.kind == SKIP_REST_OF_DAY and s.day >= start.date())
    ]


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig()

    async def list_candybars(session: Any) -> list[Any]:
        if rig.db_down:
            raise OSError("connection refused")
        return sorted(rig.rows, key=lambda r: (r.created_at, r.id))

    async def list_skips(
        session: Any, start: datetime, end: datetime | None = None
    ) -> list[SkipEntry]:
        rig.skip_reads.append((start, end))
        return skips_from(rig.skips, start, end)

    async def fire_results(session: Any, keys: list[str]) -> dict[str, str]:
        rig.result_reads.append(list(keys))
        return {k: rig.results[k] for k in keys if k in rig.results}

    monkeypatch.setattr(app_module, "list_candybars", list_candybars)
    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "fire_results", fire_results)
    return rig


def get_next(rig: Rig) -> dict[str, Any]:
    response = rig.client().get("/schedule/next")
    assert response.status_code == 200, response.text
    body = response.json()
    assert_schedule_row(body)
    # A fire at or after `cut` has not run and is never a skipped one.
    assert body["state"] == STATE_CURRENT and body["skipped"] is False
    assert "lastResult" not in body
    return body


def window_current(rig: Rig, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """The `current` rows `GET /schedule` returns for `[start, end)`."""
    return [r for r in rig.get(z(start), z(end)) if r["state"] == STATE_CURRENT]


def fire_of(body: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(body["fireTime"])


def test_contract_names_the_route_as_the_current_row() -> None:
    assert "current next-to-fire row" in contract_route_line(NEXT_ROUTE)


# ---------------------------------------------------------------------------
# Matrix rows. Each one also checks the row IS /schedule's current row.


def test_next_fire_is_the_name_tie_broken_current_row(rig: Rig) -> None:
    rig.add("bravo", "*/15 * * * *", created_at=DAY - timedelta(days=60))
    alpha = rig.add("alpha", "*/5 * * * *", noise=True)
    body = get_next(rig)
    assert body == {
        "candybarId": str(alpha.id),
        "name": "alpha",  # name tie-break over bravo@10:15
        "schedule": "*/5 * * * *",
        "fireTime": "2026-10-26T10:15:00+00:00",
        "state": "current",
        "skipped": False,
        "noise": True,
    }
    assert window_current(rig, at(10), at(10, 30)) == [body]


def test_a_skipped_next_fire_passes_to_the_next_candybar(rig: Rig) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    bravo = rig.add("bravo", "*/15 * * * *")
    rig.skip(alpha, at(10, 15))
    body = get_next(rig)
    assert (body["name"], fire_of(body)) == ("bravo", at(10, 15))
    assert body["candybarId"] == str(bravo.id)
    assert window_current(rig, at(10), at(10, 30)) == [body]


def test_a_day_skip_jumps_to_the_next_utc_day(rig: Rig) -> None:
    alpha = rig.add("alpha", "* * * * *")
    rig.skip_day(alpha, TODAY)
    rig.add("bravo", "0 * * * *")
    body = get_next(rig)
    assert (body["name"], fire_of(body)) == ("bravo", at(11))
    assert window_current(rig, at(10), at(12)) == [body]


def test_a_day_skipped_minutely_candybar_comes_back_at_midnight(rig: Rig) -> None:
    alpha = rig.add("alpha", "* * * * *")
    rig.skip_day(alpha, TODAY)
    rig.add("charlie", "0 3 * * *")  # today's 03:00 is past; next is tomorrow 03:00
    body = get_next(rig)
    assert (body["name"], fire_of(body)) == ("alpha", at(0, day=27))
    assert window_current(rig, at(23), at(1, day=27)) == [body]


def test_on_the_minute_that_fire_is_past(rig: Rig) -> None:
    rig.add("alpha", "* * * * *")
    rig.now = at(10, 15)
    assert fire_of(get_next(rig)) == at(10, 16)  # 10:15 is firing now
    rig.rows[0].schedule = "*/5 * * * *"
    assert fire_of(get_next(rig)) == at(10, 20)
    assert window_current(rig, at(10), at(10, 30)) == [get_next(rig)]


def test_a_registration_after_cut_floors_the_next_fire(rig: Rig) -> None:
    # The API clock lags the registering clock: created 10:20:10, now 10:12.
    rig.add("alpha", "* * * * *", created_at=at(10, 20, 10))
    rig.now = at(10, 12)
    assert fire_of(get_next(rig)) == at(10, 21)
    assert window_current(rig, at(10), at(10, 30)) == [get_next(rig)]


def test_only_never_firing_schedules_is_a_404_problem(rig: Rig) -> None:
    rig.add("feb30", "0 0 30 2 *")
    response = rig.client().get("/schedule/next")
    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM
    assert response.json()["title"] == "No upcoming fire"
    assert window_current(rig, at(10), at(11)) == []


def test_empty_registry_is_a_404_problem(rig: Rig) -> None:
    response = rig.client().get("/schedule/next")
    assert response.status_code == 404 and response.headers["content-type"] == PROBLEM
    assert response.json()["status"] == 404


def test_db_down_is_a_500_problem(rig: Rig) -> None:
    rig.db_down = True
    response = rig.client().get("/schedule/next")
    assert response.status_code == 500 and response.headers["content-type"] == PROBLEM


# ---------------------------------------------------------------------------
# Bounded: the read shape, no window walk, the croniter step count, the budget.


def test_the_read_is_future_skips_only_and_no_window_walk(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha = rig.add("alpha", "*/5 * * * *")
    rig.skip(alpha, at(9, 0))  # history: not read
    rig.ran(alpha, at(10, 10), "success")  # results: never looked up

    def walk(*_args: Any, **_kw: Any) -> Any:
        raise AssertionError("/schedule/next must not walk a window")

    monkeypatch.setattr(schedule_module, "fires_between", walk)
    monkeypatch.setattr(app_module, "window_rows", walk)
    assert fire_of(get_next(rig)) == at(10, 15)
    assert rig.skip_reads == [(at(10, 13), None)]  # [cut, ∞)
    assert rig.result_reads == []


def test_croniter_steps_are_bounded_by_the_skips_not_by_the_fires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = dispatcher_schedule.croniter
    steps = {"seed": 0, "next": 0}

    class Counting:
        match = staticmethod(real.match)

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            steps["seed"] += 1
            self._it = real(*args, **kwargs)

        def get_next(self, *args: Any, **kwargs: Any) -> Any:
            steps["next"] += 1
            return self._it.get_next(*args, **kwargs)

    monkeypatch.setattr(dispatcher_schedule, "croniter", Counting)
    created = DAY - timedelta(days=1)
    candybars, entries, bound = [], [], 0
    for i in range(100):  # minutely: today day-skipped, tomorrow 00:00-00:02 skipped
        c = WindowCandybar(uuid4(), f"minutely-{i:03}", "* * * * *", False, created)
        candybars.append(c)
        entries.append(SkipEntry(uuid4(), c.id, SKIP_REST_OF_DAY, None, TODAY, None, DAY))
        for minute in range(3):
            fire = at(0, minute, day=27)
            entries.append(SkipEntry(uuid4(), c.id, SKIP_OCCURRENCE, fire, None, None, DAY))
        bound += 1 + 1 + 3  # one step, plus one per skipped day and per occurrence passed
    for i in range(99):  # hourly, its 11:00 skipped
        c = WindowCandybar(uuid4(), f"hourly-{i:03}", "0 * * * *", False, created)
        candybars.append(c)
        entries.append(SkipEntry(uuid4(), c.id, SKIP_OCCURRENCE, at(11), None, None, DAY))
        bound += 1 + 1
    candybars.append(WindowCandybar(uuid4(), "feb30", "0 0 30 2 *", False, created))
    bound += 1

    row = next_row(candybars, now=at(10, 12, 30), skips=group_skips(entries))
    assert row is not None and (row.name, row.fire_time) == ("hourly-000", at(12))
    assert steps["next"] <= bound == 699
    # One window walk alone steps every minute of the skipped day per minutely
    # candybar: 100 x 827 remaining minutes of today.
    assert steps["next"] < 100 * 827
    assert steps["seed"] <= len(candybars) + 100  # a re-seed per skipped day


def _budget_registry(rig: Rig) -> None:
    schedules = ["*/5 * * * *", "0 * * * *", "7,22 10-11 * * *", "30 9 * * 1-5", "45 11 26 * 6"]
    for i in range(BUDGET_CANDYBARS - 1):
        if i % 4 == 0:
            row = rig.add(f"minutely-{i:03}", "* * * * *")
            rig.skip_day(row, TODAY)
            rig.skip(row, at(0, 0, day=27))
        else:
            row = rig.add(f"job-{i:03}", schedules[i % len(schedules)])
            rig.skip(row, at(10, 15))
    rig.add("feb30", "0 0 30 2 *")


def test_the_route_answers_within_the_stated_latency_budget(rig: Rig) -> None:
    _budget_registry(rig)
    client = rig.client()
    assert client.get("/schedule/next").status_code == 200  # warm-up
    timings = []
    for _ in range(5):
        started = time.perf_counter()
        response = client.get("/schedule/next")
        timings.append(time.perf_counter() - started)
        assert response.status_code == 200, response.text
    assert_schedule_row(response.json())
    assert statistics.median(timings) < RIG_BUDGET_SECONDS, timings


# ---------------------------------------------------------------------------
# One definition of current: randomized equivalence with the window and brute force.


POOL = [
    "* * * * *",
    "*/5 * * * *",
    "*/7 * * * *",
    "0 * * * *",
    "7,22 10-11 * * *",
    "0 3 * * *",
    "30 9 * * 1-5",
    "45 11 26 * 6",
    "0 0 1 1 *",
    "0 0 30 2 *",
]


def brute_next(
    candybars: list[WindowCandybar], cut: datetime, entries: list[SkipEntry]
) -> tuple[datetime, str] | None:
    """The definition, by enumeration: first uncovered fire per candybar, min by (fire, name)."""
    skips = group_skips(entries)
    best: tuple[datetime, str] | None = None
    for c in candybars:
        covered = skips.get(c.id)
        lo = max(cut, ceil_minute(c.created_at))
        for fire in fires_between(c.schedule, lo, cut + timedelta(days=400)):
            if covered is None or not covered.covers(fire):
                if best is None or (fire, c.name) < best:
                    best = (fire, c.name)
                break
    return best


def test_next_row_equals_the_window_current_and_the_brute_force_definition() -> None:
    rng = random.Random(53)
    for trial in range(80):
        now = at(0) + timedelta(seconds=rng.randrange(2 * 86400))
        cut = first_tick_after(now)
        candybars, entries = [], []
        for i in range(rng.randint(1, 5)):
            created = now + timedelta(minutes=rng.choice([-10_000, -90, 0, 7, 45]))
            c = WindowCandybar(uuid4(), f"c{i}", rng.choice(POOL), False, created)
            candybars.append(c)
            # Skip a few of its own upcoming fires, and sometimes whole days.
            upcoming = list(fires_between(c.schedule, cut, cut + timedelta(hours=3)))[:4]
            for fire in rng.sample(upcoming, k=min(len(upcoming), rng.randint(0, 3))):
                entries.append(SkipEntry(uuid4(), c.id, SKIP_OCCURRENCE, fire, None, None, DAY))
            for offset in rng.sample([0, 1, 2], k=rng.randint(0, 2)):
                day = cut.date() + timedelta(days=offset)
                entries.append(SkipEntry(uuid4(), c.id, SKIP_REST_OF_DAY, None, day, None, DAY))

        row = next_row(candybars, now=now, skips=group_skips(skips_from(entries, cut, None)))
        expected = brute_next(candybars, cut, entries)
        assert (None if row is None else (row.fire_time, row.name)) == expected, trial
        found = global_next(candybars, now=now, skips=group_skips(skips_from(entries, cut, None)))
        assert (found is None and row is None) or (found[0], found[1].name) == expected

        # The window marks the same row current (a window-bounded skip read).
        start = cut + timedelta(minutes=rng.choice([-120, -1, 0, 30, 180]))
        end = start + timedelta(hours=6)
        lo = min(start, cut)
        rows = window_rows(
            candybars, start, end, now=now,
            skips=group_skips(skips_in(entries, lo, end)), limit=10**6,
        )
        current = [r for r in rows if r.state == STATE_CURRENT]
        if row is not None and start <= row.fire_time < end:
            assert current == [row], trial
        else:
            assert current == [], trial


def test_default_clock_is_the_utc_wall_clock(rig: Rig) -> None:
    rig.add("alpha", "* * * * *")

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    client = TestClient(create_app(session_factory=session_factory, sink=None))
    before = datetime.now(UTC)
    response = client.get("/schedule/next")
    after = datetime.now(UTC)
    assert response.status_code == 200, response.text
    assert response.json()["fireTime"] in (
        first_tick_after(before).isoformat(),
        first_tick_after(after).isoformat(),  # the request straddled a minute
    )
