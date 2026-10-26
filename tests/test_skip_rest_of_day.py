"""Story 2.4 — skip the rest of a day (AD-13), unit level.

Every non-DB row of the spec's I/O matrix against the shared skip-aware
schedule helpers and the Dispatcher with in-memory schedules, skips and
cursor. The registry write path and the live proof (real roles, real bus,
real rows) are tests/test_skip_rest_of_day_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from candybar.dispatcher import Dispatcher
from candybar.dispatcher.schedule import (
    NO_SKIPS,
    SkipSet,
    fires_between,
    group_skips,
    next_fire,
    previous_fire,
)
from candybar.dispatcher.worker import (
    operator_skipped_for,
    plan_misfires,
)
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand, CandybarSkippedEvent
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry import (
    SKIP_KINDS,
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    rest_of_day_skip_id,
)
from candybar.registry.store import ScheduleEntry, add_rest_of_day_skip

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
EVERY5 = "*/5 * * * *"
HOURLY = "0 * * * *"
OCT25 = date(2026, 10, 25)
OCT26 = date(2026, 10, 26)
OCT27 = date(2026, 10, 27)


def at(hour: int, minute: int, day: int = 26) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def entry(schedule: str = EVERY5, name: str = "job") -> ScheduleEntry:
    return ScheduleEntry(uuid4(), name, schedule, LONG_AGO)


@dataclass(frozen=True)
class Row:
    """A registry skip row as the dispatcher sees it (SkipEntry's fields)."""

    candybar_id: UUID
    fire_time: datetime | None = None
    kind: str = SKIP_REST_OF_DAY
    day: date | None = None


class Message:
    def __init__(self, payload: bytes, pending: int | None = None):
        self.data = payload
        self.acks = self.naks = self.terms = 0
        if pending is not None:
            self.metadata = type("Meta", (), {"num_pending": pending})()

    async def ack(self):
        self.acks += 1

    async def nak(self, **_kwargs):
        self.naks += 1

    async def term(self):
        self.terms += 1


class FakeCursor:
    def __init__(self, minute: datetime | None):
        self.minute = minute
        self.advances: list[datetime] = []

    async def load(self):
        return self.minute

    async def advance(self, minute):
        self.advances.append(minute)
        self.minute = minute if self.minute is None else max(self.minute, minute)


class Harness:
    """Dispatcher over in-memory registry state; `rows` are the `skips` table."""

    def __init__(self, entries, rows=(), cursor=None, *, fail_skips=False):
        self.entries = list(entries)
        self.rows = list(rows)
        self.cursor = cursor
        self.sent: list = []
        self.windows: list[tuple[datetime, datetime]] = []
        self.fail_skips = fail_skips

        async def schedules():
            return list(self.entries)

        async def skips(start, end):
            self.windows.append((start, end))
            if self.fail_skips:
                self.fail_skips = False  # one-shot
                raise ConnectionError("registry down")
            # Same contract as store.list_skips: occurrence rows with
            # start <= fire_time < end, plus rest-of-day rows whose date
            # intersects the window.
            last_date = (end - timedelta(minutes=1)).date()
            return [
                r
                for r in self.rows
                if (r.kind == SKIP_OCCURRENCE and r.fire_time is not None
                    and start <= r.fire_time < end)
                or (r.kind == SKIP_REST_OF_DAY and r.day is not None
                    and start.date() <= r.day <= last_date)
            ]

        async def publish(event):
            assert parse_envelope(json.loads(event.model_dump_json())) == event
            self.sent.append(event)

        self.dispatcher = Dispatcher(schedules, publish, cursor=cursor, skips=skips)

    def tick(self, minute: datetime, pending: int | None = None) -> Message:
        msg = Message(build_tick_fired(minute).model_dump_json().encode(), pending)
        asyncio.run(self.dispatcher.handle_message(msg))
        return msg

    def commands(self, candybar: ScheduleEntry | None = None) -> list[CandybarInvokeCommand]:
        return [
            e for e in self.sent
            if isinstance(e, CandybarInvokeCommand)
            and (candybar is None or e.data.candybar_id == candybar.id)
        ]

    def skipped(self) -> list[CandybarSkippedEvent]:
        return [e for e in self.sent if isinstance(e, CandybarSkippedEvent)]


def day_skip(job: ScheduleEntry, day: date) -> Row:
    return Row(job.id, kind=SKIP_REST_OF_DAY, day=day)


# ---------------------------------------------------------------------------
# SkipSet / group_skips / shared helpers
# ---------------------------------------------------------------------------


def test_skip_set_reasons_and_truthiness():
    skips = SkipSet(
        occurrences=frozenset({at(10, 10)}), rest_of_day=frozenset({OCT27})
    )
    # Both kinds cover 10:10 on the 26th? No: occurrence only. Occurrence wins
    # where both apply (pinned in the dispatcher test below).
    assert skips.reason_for(at(10, 10)) == SKIP_OCCURRENCE
    assert skips.reason_for(at(10, 15)) is None
    assert skips.reason_for(at(0, 0, 27)) == SKIP_REST_OF_DAY
    assert skips.reason_for(at(23, 59, 27)) == SKIP_REST_OF_DAY
    assert skips.reason_for(at(0, 0, 25)) is None
    assert skips and skips.covers(at(12, 0, 27))
    assert not NO_SKIPS and not SkipSet(occurrences=frozenset({at(10, 10)})).rest_of_day
    assert bool(SkipSet(rest_of_day=frozenset({OCT26})))  # days alone are truthy
    assert SKIP_REST_OF_DAY in SKIP_KINDS


def test_group_skips_folds_day_rows_and_ignores_malformed_and_unknown():
    a, b = uuid4(), uuid4()
    rows = [
        Row(a, day=OCT26),
        Row(a, day=OCT27),
        Row(b, fire_time=at(10, 10), kind=SKIP_OCCURRENCE),
        Row(b, kind=SKIP_REST_OF_DAY, day=None),  # malformed: no date
        Row(a, kind="something_newer", day=OCT26, fire_time=at(1, 0)),  # unknown kind
    ]
    grouped = group_skips(rows)
    assert grouped[a] == SkipSet(rest_of_day=frozenset({OCT26, OCT27}))
    assert grouped[b] == SkipSet(occurrences=frozenset({at(10, 10)}))
    assert grouped[a].reason_for(at(1, 0)) == SKIP_REST_OF_DAY  # unknown-kind row ignored


def test_helpers_omit_a_whole_skipped_day():
    skips = SkipSet(rest_of_day=frozenset({OCT26}))
    assert next_fire(EVERY5, at(23, 55, 25)) == at(23, 55, 25)  # day before unaffected
    assert next_fire(EVERY5, at(23, 56, 25), skips=skips) == at(0, 0, 27)
    assert next_fire(EVERY5, at(12, 0), skips=skips) == at(0, 0, 27)
    assert next_fire(EVERY5, at(0, 0, 27), skips=skips) == at(0, 0, 27)  # next day fine
    window = list(fires_between(EVERY5, at(23, 50, 25), at(0, 10, 27), skips=skips))
    assert window == [at(23, 50, 25), at(23, 55, 25), at(0, 0, 27), at(0, 5, 27)]
    assert all(f.date() != OCT26 for f in window)
    assert previous_fire(EVERY5, at(0, 5, 27), skips=skips) == at(0, 0, 27)
    # From midnight on the 27th the walk crosses the whole skipped day.
    assert previous_fire(EVERY5, at(0, 0, 27), skips=skips) == at(23, 55, 25)
    assert previous_fire(EVERY5, at(0, 5, 27)) == at(0, 0, 27)  # unskipped baseline


def test_previous_fire_walking_a_fully_skipped_day_terminates():
    # Worst case: every-minute schedule, one full day skipped (1440 fires).
    every_minute = "* * * * *"
    skips = SkipSet(rest_of_day=frozenset({OCT26}))
    assert previous_fire(every_minute, at(0, 0, 27), skips=skips) == at(23, 59, 25)
    # Consecutive stored days are walked too.
    two_days = SkipSet(rest_of_day=frozenset({OCT25, OCT26}))
    assert next_fire(HOURLY, at(0, 0, 25), skips=two_days) == at(0, 0, 27)


# ---------------------------------------------------------------------------
# Matrix: tick path — suppression until the next UTC midnight
# ---------------------------------------------------------------------------


def test_day_skipped_tick_fire_emits_skipped_instead_of_a_command():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [day_skip(job, OCT26)], cursor)
    msg = h.tick(at(10, 10))
    assert (msg.acks, msg.naks) == (1, 0)
    assert h.commands() == []
    (fact,) = h.skipped()
    assert fact.data.reason == "rest_of_day"
    assert fact.data.tick_id == tid(at(10, 10))
    assert fact.data.candybar_id == job.id and fact.data.name == "job"
    assert cursor.advances == [at(10, 10)]


def test_every_matching_fire_of_the_skipped_day_is_recorded_on_its_own_tick():
    job = entry()
    h = Harness([job], [day_skip(job, OCT26)], FakeCursor(at(23, 54, 25)))
    for minute in (at(23, 55, 25), at(0, 0), at(0, 5), at(0, 10)):
        assert h.tick(minute).acks == 1
    assert [c.data.tick_id for c in h.commands()] == [tid(at(23, 55, 25))]
    # 00:00 and 00:05 on the 26th suppressed; 00:10 on the 26th too — all of
    # them on their own ticks, then the 27th runs. Split the assertion:
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(0, 0)), "rest_of_day"),
        (tid(at(0, 5)), "rest_of_day"),
        (tid(at(0, 10)), "rest_of_day"),
    ]


def test_the_following_utc_day_dispatches_normally():
    job = entry()
    h = Harness([job], [day_skip(job, OCT26)], FakeCursor(at(23, 59)))
    assert h.tick(at(0, 0, 27)).acks == 1
    (command,) = h.commands()
    assert (command.data.trigger, command.data.tick_id) == ("scheduled", tid(at(0, 0, 27)))
    assert h.skipped() == []


def test_the_day_before_the_skip_dispatches_normally():
    job = entry()
    h = Harness([job], [day_skip(job, OCT26)], FakeCursor(at(23, 54, 25)))
    assert h.tick(at(23, 55, 25)).acks == 1
    assert [c.data.tick_id for c in h.commands()] == [tid(at(23, 55, 25))]
    assert h.skipped() == []


def test_a_day_skip_on_one_candybar_never_suppresses_another():
    a, b = entry(name="a"), entry(name="b")
    h = Harness([a, b], [day_skip(a, OCT26)], FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    assert h.commands(a) == []
    assert [c.data.tick_id for c in h.commands(b)] == [tid(at(10, 10))]
    assert [s.data.candybar_id for s in h.skipped()] == [a.id]


def test_a_skip_for_another_date_is_inert():
    job = entry()
    h = Harness([job], [day_skip(job, OCT25)], FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    assert [c.data.tick_id for c in h.commands()] == [tid(at(10, 10))]
    assert h.skipped() == []
    # The day row WAS read (the window covers the 26th, the row is the 25th —
    # no; a 25th row does not intersect the 26th window). Window contract:
    assert h.windows == [(at(10, 10), at(10, 11))]


def test_dispatch_count_excludes_day_skipped_fires():
    a, b = entry(name="a"), entry(name="b")
    h = Harness([a, b], [day_skip(a, OCT26)], FakeCursor(at(10, 9)))
    tick = build_tick_fired(at(10, 10))
    assert asyncio.run(h.dispatcher.dispatch(tick)) == 1


# ---------------------------------------------------------------------------
# Matrix: day skips inside a misfire gap
# ---------------------------------------------------------------------------


def test_gap_covering_a_fully_skipped_day_runs_the_next_days_fire_as_misfire():
    job = entry(HOURLY)
    h = Harness([job], [day_skip(job, OCT26)], FakeCursor(at(0, 5)))
    msg = h.tick(at(0, 30, 27))
    assert msg.acks == 1
    (run,) = h.commands()
    assert (run.data.trigger, run.data.tick_id) == ("misfire", tid(at(0, 0, 27)))
    facts = h.skipped()
    # The 23 overridden fires of the 26th, oldest first, before the run.
    assert [(s.data.tick_id, s.data.reason) for s in facts] == [
        (tid(at(hour, 0)), "rest_of_day") for hour in range(1, 24)
    ]
    assert isinstance(h.sent[-1], CandybarInvokeCommand)
    assert h.windows == [(at(0, 6), at(0, 31, 27))]  # read back to the cursor


def test_gap_entirely_inside_the_skipped_day_runs_nothing():
    job = entry(HOURLY)
    h = Harness([job], [day_skip(job, OCT26)], FakeCursor(at(0, 5)))
    msg = h.tick(at(2, 30))
    assert msg.acks == 1 and h.commands() == []
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(1, 0)), "rest_of_day"),
        (tid(at(2, 0)), "rest_of_day"),
    ]


def test_planner_lists_day_overrides_separately_from_coalesced():
    job = entry(HOURLY)
    skips = group_skips([day_skip(job, OCT26)])
    (plan,) = plan_misfires([job], at(20, 0, 25), at(2, 30), skips=skips)
    # Missed fires: 21:00..23:00 (25th) and 00:00..02:00 (26th, day-skipped).
    # The latest NON-skipped missed fire is 23:00 on the 25th: it runs, the
    # earlier 25th fires coalesce, and the 26th's fires are overrides.
    assert plan.latest == at(23, 0, 25)
    assert plan.skipped == [at(21, 0, 25), at(22, 0, 25)]
    assert plan.overridden == [at(0, 0), at(1, 0), at(2, 0)]
    (plain,) = plan_misfires([job], at(20, 0, 25), at(2, 30))
    assert plain.latest == at(2, 0) and plain.overridden == []


# ---------------------------------------------------------------------------
# Precedence, identity, redelivery, failure
# ---------------------------------------------------------------------------


def test_occurrence_skip_wins_when_both_kinds_cover_one_fire():
    job = entry()
    rows = [day_skip(job, OCT26), Row(job.id, fire_time=at(10, 10), kind=SKIP_OCCURRENCE)]
    h = Harness([job], rows, FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    assert h.commands() == []
    (fact,) = h.skipped()
    assert fact.data.reason == "occurrence"
    assert fact.data.tick_id == tid(at(10, 10))


def test_rest_of_day_skipped_identity_is_deterministic_and_kind_distinct():
    job = entry()
    fire = at(10, 10)
    on_tick = operator_skipped_for(job, fire, "rest_of_day", build_tick_fired(fire))
    in_gap = operator_skipped_for(job, fire, "rest_of_day", build_tick_fired(at(10, 23)))
    assert on_tick.id == in_gap.id
    # Same fire, different kind: a different fact id.
    occurrence = operator_skipped_for(job, fire, "occurrence", build_tick_fired(fire))
    assert on_tick.id != occurrence.id
    assert on_tick.data.reason == "rest_of_day"
    tick = build_tick_fired(fire)
    linked = operator_skipped_for(job, fire, "rest_of_day", tick)
    assert linked.causationid == tick.id and linked.correlationid == tick.correlationid


def test_redelivered_tick_republishes_the_same_rest_of_day_fact():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [day_skip(job, OCT26)], cursor)
    h.tick(at(10, 10))
    h.tick(at(10, 10))  # redelivery: tick <= cursor
    first, second = h.skipped()
    assert first.id == second.id
    assert h.commands() == []


def test_skips_read_failure_naks_without_publishing_and_redelivery_recovers():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [day_skip(job, OCT26)], cursor, fail_skips=True)
    first = h.tick(at(10, 10))
    assert (first.acks, first.naks) == (0, 1)
    assert h.sent == [] and cursor.advances == []
    retry = h.tick(at(10, 10))
    assert (retry.acks, retry.naks) == (1, 0)
    assert h.commands() == [] and len(h.skipped()) == 1


# ---------------------------------------------------------------------------
# Store input validation (no session) and row-id determinism
# ---------------------------------------------------------------------------


def test_rest_of_day_skip_ids_are_deterministic_per_candybar_and_date():
    a, b = uuid4(), uuid4()
    assert rest_of_day_skip_id(a, OCT26) == rest_of_day_skip_id(a, OCT26)
    assert rest_of_day_skip_id(a, OCT26) != rest_of_day_skip_id(a, OCT27)
    assert rest_of_day_skip_id(a, OCT26) != rest_of_day_skip_id(b, OCT26)


@pytest.mark.parametrize(
    "bad",
    [datetime(2026, 10, 26, tzinfo=UTC), "2026-10-26", None, 20261026],
    ids=["datetime", "str", "none", "int"],
)
def test_a_non_date_day_is_rejected_before_touching_the_registry(bad):
    class NoSession:
        def __getattr__(self, name):
            raise AssertionError("registry touched for an invalid day")

    with pytest.raises(ValueError, match="day"):
        asyncio.run(add_rest_of_day_skip(NoSession(), uuid4(), bad))
    with pytest.raises(ValueError, match="day"):
        rest_of_day_skip_id(uuid4(), bad)


def test_consecutive_day_rows_are_each_honored():
    # Daily-at-midnight schedule, both the 26th and the 27th day-skipped.
    # (Ticks a day apart are gaps for the cursor, but a daily schedule has no
    # missed fires strictly between two midnight ticks, so each tick takes the
    # plain tick path: skipped on the covered dates, scheduled after.)
    job = entry("0 0 * * *")
    rows = [day_skip(job, OCT26), day_skip(job, OCT27)]
    h = Harness([job], rows, FakeCursor(at(23, 59, 25)))
    for minute in (at(0, 0), at(0, 0, 27), at(0, 0, 28)):
        assert h.tick(minute).acks == 1
    assert [c.data.tick_id for c in h.commands()] == [tid(at(0, 0, 28))]
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(0, 0)), "rest_of_day"),
        (tid(at(0, 0, 27)), "rest_of_day"),
    ]
