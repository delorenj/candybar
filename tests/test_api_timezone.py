"""Story 7.1 (AD-16): the per-candybar timezone through the HTTP API.

Registration and PATCH validate and carry `timezone` (and emit it on the
`created` / `updated` facts); the definitions shape gains it; and the skip and
schedule routes read dates and fires in the candybar's own zone — across the
2026-11-01 fall-back, where a fixed-hour fire happens once and the repeated
hour's second occurrence is not a fire at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi.testclient import TestClient

from candybar.registry import SKIP_REST_OF_DAY
from test_api_definitions import CREATE_BODY, Rig, rig  # noqa: F401  (fixture)
from test_api_skips import client, skip_facts, skips_rig  # noqa: F401  (fixture)

NEW_YORK = "America/New_York"
PROBLEM = "application/problem+json"
EARLY = datetime(2026, 1, 1, tzinfo=UTC)  # registration far before the fall-back night


def iso(*args: int) -> str:
    return datetime(*args, tzinfo=UTC).isoformat()


# ---------------------------------------------------------------------------
# Registration, PATCH and the definitions shape.


def test_register_carries_the_timezone_on_the_row_the_answer_and_the_fact(rig: Rig) -> None:  # noqa: F811
    resp = rig.client().post("/candybars", json={**CREATE_BODY, "timezone": NEW_YORK})
    assert resp.status_code == 201
    body = resp.json()
    assert body["timezone"] == NEW_YORK
    assert rig.rows[next(iter(rig.rows))].timezone == NEW_YORK
    (created,) = rig.sink.published
    assert created.data.timezone == NEW_YORK
    assert rig.client().get(f"/candybars/{body['id']}").json()["timezone"] == NEW_YORK
    [listed] = rig.client().get("/candybars").json()
    assert listed["timezone"] == NEW_YORK


def test_register_defaults_to_utc(rig: Rig) -> None:  # noqa: F811
    body = rig.client().post("/candybars", json=CREATE_BODY).json()
    assert body["timezone"] == "UTC"
    assert rig.sink.published[0].data.timezone == "UTC"


def test_register_an_unknown_timezone_is_422_nothing_written(rig: Rig) -> None:  # noqa: F811
    for bad in ("Mars/Olympus", "", "localtime", "America/New York"):
        resp = rig.client().post("/candybars", json={**CREATE_BODY, "timezone": bad})
        assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM, bad
        assert "timezone" in resp.json()["detail"]
    assert rig.rows == {} and rig.sink.published == []


def test_patch_timezone_emits_updated_with_only_the_timezone(rig: Rig) -> None:  # noqa: F811
    row = rig.add("ifvault", schedule="40 1 * * *")
    resp = rig.client().patch(f"/candybars/{row.id}", json={"timezone": NEW_YORK})
    assert resp.status_code == 200 and resp.json()["timezone"] == NEW_YORK
    (event,) = rig.sink.published
    assert event.type == "bloodbank.cron.candybar.updated"
    assert event.data.changes == {"timezone": NEW_YORK}
    assert rig.rows[row.id].timezone == NEW_YORK


def test_patch_timezone_to_the_stored_value_is_a_no_op(rig: Rig) -> None:  # noqa: F811
    row = rig.add("ifvault", schedule="40 1 * * *", timezone=NEW_YORK)
    assert rig.client().patch(f"/candybars/{row.id}", json={"timezone": NEW_YORK}).status_code == 200
    assert rig.sink.published == [] and rig.writes == 0


def test_patch_an_unknown_timezone_is_422_and_unchanged(rig: Rig) -> None:  # noqa: F811
    row = rig.add("ifvault", schedule="40 1 * * *")
    resp = rig.client().patch(f"/candybars/{row.id}", json={"timezone": "Nope/Nada"})
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert rig.rows[row.id].timezone == "UTC" and rig.sink.published == []


# ---------------------------------------------------------------------------
# Skips and the schedule window, read in the candybar's zone.


def _ifvault(rig: Rig) -> Any:  # noqa: F811
    return rig.add("ifvault", schedule="40 1 * * *", timezone=NEW_YORK, created_at=EARLY)


def test_the_repeated_hours_second_occurrence_is_not_a_fire(skips_rig: Rig) -> None:  # noqa: F811
    row = _ifvault(skips_rig)
    api: TestClient = client(skips_rig)
    # 01:40 EDT (05:40Z) is the fire; 01:40 EST (06:40Z) is not one.
    second = api.post(f"/candybars/{row.id}/skip", json={"fireTime": iso(2026, 11, 1, 6, 40)})
    assert second.status_code == 404 and "never fires" in second.json()["detail"]
    first = api.post(f"/candybars/{row.id}/skip", json={"fireTime": iso(2026, 11, 1, 5, 40)})
    assert first.status_code == 200 and first.json()["skipped"] is True


def test_skip_rest_of_day_is_the_candybar_local_date(skips_rig: Rig) -> None:  # noqa: F811
    row = _ifvault(skips_rig)
    api = client(skips_rig)
    # Local Nov 1 for New York: the one 01:40 EDT fire, which UTC dates as Nov 1 too;
    # Oct 31's fire is 05:40Z Oct 31 and Nov 2's is 06:40Z Nov 2.
    resp = api.post(f"/candybars/{row.id}/skip-rest-of-day", json={"date": "2026-11-01"})
    assert resp.status_code == 200 and resp.json() == {"skipped": True, "date": "2026-11-01"}
    (fact,) = skip_facts(skips_rig)
    assert fact.data.reason == SKIP_REST_OF_DAY
    # The fact names the day's first fire, 05:40Z, as its tick.
    assert fact.data.tick_id == f"tick:{int(datetime(2026, 11, 1, 5, 40, tzinfo=UTC).timestamp()) // 60}"
    rows = api.get("/schedule", params={"from": iso(2026, 10, 31, 12), "to": iso(2026, 11, 3)}).json()
    assert [(r["fireTime"], r["skipped"]) for r in rows] == [
        (iso(2026, 11, 1, 5, 40), True),  # local Nov 1: skipped
        (iso(2026, 11, 2, 6, 40), False),  # 01:40 EST Nov 2: a different local date
    ]


def test_a_local_date_the_cron_never_fires_on_is_404(skips_rig: Rig) -> None:  # noqa: F811
    row = skips_rig.add(
        "yearly", schedule="0 3 1 12 *", timezone=NEW_YORK, created_at=EARLY
    )
    resp = client(skips_rig).post(f"/candybars/{row.id}/skip-rest-of-day", json={"date": "2026-11-01"})
    assert resp.status_code == 404


def test_the_schedule_window_shows_the_gap_fire_after_the_gap(skips_rig: Rig) -> None:  # noqa: F811
    skips_rig.add("ledger", schedule="30 2 * * *", timezone=NEW_YORK, created_at=EARLY)
    rows = client(skips_rig).get(
        "/schedule", params={"from": iso(2027, 3, 13), "to": iso(2027, 3, 16)}
    ).json()
    assert [r["fireTime"] for r in rows] == [
        iso(2027, 3, 13, 7, 30),
        iso(2027, 3, 14, 7, 0),  # 02:30 does not exist: once, at 03:00 EDT
        iso(2027, 3, 15, 6, 30),
    ]
    assert {r["schedule"] for r in rows} == {"30 2 * * *"}  # stored exactly as written
