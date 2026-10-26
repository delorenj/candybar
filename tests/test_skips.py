"""Story 2.3 — skip a single occurrence (AD-13), unit level.

Every non-DB row of the spec's I/O matrix against the shared skip-aware
schedule helpers and the Dispatcher with in-memory schedules, skips and
cursor. The registry write path and the live proof (real roles, real bus,
real rows) are tests/test_skip_occurrence_integration.py.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from candybar.dispatcher import Dispatcher
from candybar.dispatcher.schedule import (
    NO_SKIPS,
    SkipSet,
    fires_between,
    group_skips,
    matches_tick,
    next_fire,
    previous_fire,
)
from candybar.dispatcher.worker import (
    MISFIRE_COALESCED,
    misfire_skipped_for,
    operator_skipped_for,
    plan_misfires,
)
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand, CandybarSkippedEvent
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry import SKIP_KINDS, SKIP_OCCURRENCE, occurrence_skip_id
from candybar.registry.store import ScheduleEntry, add_occurrence_skip

LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)
EVERY5 = "*/5 * * * *"


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
    fire_time: datetime | None
    kind: str = SKIP_OCCURRENCE
    day: date | None = None  # protocol parity with SkipRow (Story 2.4)


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

    def __init__(self, entries, rows=(), cursor=None, *, wire_skips=True, fail_skips=False):
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
            # Same contract as store.list_skips: start <= fire_time < end.
            return [r for r in self.rows if r.fire_time is not None and start <= r.fire_time < end]

        async def publish(event):
            assert parse_envelope(json.loads(event.model_dump_json())) == event
            self.sent.append(event)

        self.dispatcher = Dispatcher(
            schedules, publish, cursor=cursor, skips=skips if wire_skips else None
        )

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


def skip(job: ScheduleEntry, fire: datetime) -> Row:
    return Row(job.id, fire)


# ---------------------------------------------------------------------------
# Shared skip-aware helpers (the AC's "upcoming-fire computation")
# ---------------------------------------------------------------------------


def test_upcoming_fire_helpers_omit_the_skipped_fire():
    skips = SkipSet(occurrences=frozenset({at(10, 10)}))
    assert next_fire(EVERY5, at(10, 6), skips=skips) == at(10, 15)
    assert next_fire(EVERY5, at(10, 6)) == at(10, 10)  # unskipped baseline
    assert next_fire(EVERY5, at(10, 15), skips=skips) == at(10, 15)  # inclusive
    window = list(fires_between(EVERY5, at(10, 0), at(10, 30), skips=skips))
    assert window == [at(10, m) for m in (0, 5, 15, 20, 25)]
    assert at(10, 10) not in window and at(10, 15) in window
    assert previous_fire(EVERY5, at(10, 11), skips=skips) == at(10, 5)
    assert previous_fire(EVERY5, at(10, 11)) == at(10, 10)


def test_helpers_walk_past_consecutive_skips_and_stay_utc():
    skips = SkipSet(occurrences=frozenset({at(10, 10), at(10, 15), at(10, 20)}))
    assert next_fire(EVERY5, at(10, 6), skips=skips) == at(10, 25)
    assert previous_fire(EVERY5, at(10, 24), skips=skips) == at(10, 5)
    assert next_fire(EVERY5, at(10, 6), skips=skips).tzinfo is UTC
    with pytest.raises(ValueError, match="UTC"):
        next_fire(EVERY5, datetime(2026, 10, 26, 10, 6))
    with pytest.raises(ValueError, match="minute boundary"):
        next_fire(EVERY5, at(10, 6) + timedelta(seconds=1))


def test_never_firing_schedule_has_no_upcoming_fire():
    assert next_fire("0 0 30 2 *", at(10, 0)) is None
    assert previous_fire("0 0 30 2 *", at(10, 0), skips=SkipSet(frozenset({at(10, 0)}))) is None


def test_skipping_the_only_remaining_fire_reads_as_never_again():
    # A yearly job whose next fire is skipped falls through to the one after.
    yearly = "0 0 1 1 *"
    nye = datetime(2027, 1, 1, tzinfo=UTC)
    skips = SkipSet(occurrences=frozenset({nye}))
    assert next_fire(yearly, at(10, 0), skips=skips) == datetime(2028, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    "schedule",
    ["* * * * *", EVERY5, "0 9 * * 1-5", "45 11 26 * 6", "0 0 * * *", "59 23 * * *", "0 0 30 2 *"],
)
def test_next_fire_day_jump_equals_stepping_every_fire(schedule):
    # Story 5.3: a rest-of-day date jumps to the next UTC midnight instead of
    # stepping its fires. The answer must be the first uncovered fire, exactly.
    skip_sets = [
        SkipSet(rest_of_day=frozenset({date(2026, 10, 26), date(2026, 10, 27)})),  # consecutive
        SkipSet(  # both kinds on one date, plus the first fire after the skipped day
            occurrences=frozenset({at(10, 15), at(0, 0, 27), at(9, 0, 27)}),
            rest_of_day=frozenset({date(2026, 10, 26)}),
        ),
        SkipSet(rest_of_day=frozenset({date(2026, 10, 31)})),  # a Saturday: no weekday fire
        SkipSet(rest_of_day=frozenset({date(2026, 10, 30), date(2026, 11, 2)})),  # Fri + Mon
    ]
    for skips in skip_sets:
        for start in (at(0, 0), at(10, 13), at(23, 59), at(0, 0, 27), at(23, 59, 30)):
            stepped = next(
                (f for f in fires_between(schedule, start, start + timedelta(days=60))
                 if not skips.covers(f)),
                None,
            )
            assert next_fire(schedule, start, skips=skips) == stepped, (skips, start)


def test_group_skips_folds_rows_per_candybar_and_ignores_unknown_kinds():
    a, b = uuid4(), uuid4()
    rows = [
        Row(a, at(10, 10)),
        Row(a, at(10, 20)),
        Row(b, at(10, 10)),
        Row(b, at(10, 30), kind="something_newer"),  # a kind this daemon does not know
        Row(b, None),  # malformed occurrence row: no fire time
    ]
    grouped = group_skips(rows)
    assert grouped[a] == SkipSet(occurrences=frozenset({at(10, 10), at(10, 20)}))
    assert grouped[b] == SkipSet(occurrences=frozenset({at(10, 10)}))
    assert grouped[b].reason_for(at(10, 10)) == SKIP_OCCURRENCE
    assert grouped[b].reason_for(at(10, 30)) is None
    assert not NO_SKIPS and NO_SKIPS.reason_for(at(10, 10)) is None
    assert SKIP_OCCURRENCE in SKIP_KINDS


# ---------------------------------------------------------------------------
# Matrix: skipped tick fire / next fire / other candybar
# ---------------------------------------------------------------------------


def test_skipped_tick_fire_emits_skipped_instead_of_a_command():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [skip(job, at(10, 10))], cursor)
    msg = h.tick(at(10, 10))
    assert (msg.acks, msg.naks) == (1, 0)
    assert h.commands() == []
    (fact,) = h.skipped()
    assert fact.data.reason == "occurrence"
    assert fact.data.tick_id == tid(at(10, 10))
    assert fact.data.candybar_id == job.id and fact.data.name == "job"
    assert fact.subject == "bloodbank.evt.cron.candybar.skipped"
    assert cursor.advances == [at(10, 10)]  # the tick is fully handled


def test_subsequent_fires_of_the_same_candybar_dispatch_normally():
    job = entry()
    h = Harness([job], [skip(job, at(10, 10))], FakeCursor(at(10, 4)))
    for minute in (at(10, 5), at(10, 6), at(10, 10), at(10, 15)):
        assert h.tick(minute).acks == 1
    assert [c.data.tick_id for c in h.commands()] == [tid(at(10, 5)), tid(at(10, 15))]
    assert all(c.data.trigger == "scheduled" for c in h.commands())
    assert [s.data.tick_id for s in h.skipped()] == [tid(at(10, 10))]


def test_a_skip_on_one_candybar_never_suppresses_another():
    a, b = entry(name="a"), entry(name="b")
    h = Harness([a, b], [skip(a, at(10, 10))], FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    assert h.commands(a) == []
    assert [c.data.tick_id for c in h.commands(b)] == [tid(at(10, 10))]
    assert [s.data.candybar_id for s in h.skipped()] == [a.id]


def test_skip_that_matches_no_fire_is_inert():
    job = entry()
    h = Harness([job], [skip(job, at(10, 11))], FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    h.tick(at(10, 11))
    assert [c.data.tick_id for c in h.commands()] == [tid(at(10, 10))]
    assert h.skipped() == []


def test_dispatch_count_excludes_skipped_fires():
    a, b = entry(name="a"), entry(name="b")
    h = Harness([a, b], [skip(a, at(10, 10))], FakeCursor(at(10, 9)))
    tick = build_tick_fired(at(10, 10))
    assert asyncio.run(h.dispatcher.dispatch(tick)) == 1


def test_overrides_are_read_for_the_tick_minute_only_without_a_gap():
    job = entry()
    h = Harness([job], [skip(job, at(10, 10))], FakeCursor(at(10, 9)))
    h.tick(at(10, 10))
    assert h.windows == [(at(10, 10), at(10, 11))]


def test_backlog_tick_reads_no_overrides_and_publishes_nothing():
    job = entry()
    h = Harness([job], [skip(job, at(10, 10))], FakeCursor(at(10, 0)))
    msg = h.tick(at(10, 10), pending=3)
    assert msg.acks == 1 and h.sent == [] and h.windows == []


# ---------------------------------------------------------------------------
# Matrix: skips inside a misfire gap (2.2 coalescing is skip-aware)
# ---------------------------------------------------------------------------


def test_skipped_latest_missed_fire_hands_the_misfire_run_to_the_previous_fire():
    job = entry()
    h = Harness([job], [skip(job, at(10, 25))], FakeCursor(at(10, 15)))
    msg = h.tick(at(10, 28))
    assert msg.acks == 1
    (run,) = h.commands()
    assert (run.data.trigger, run.data.tick_id) == ("misfire", tid(at(10, 20)))
    (fact,) = h.skipped()
    assert (fact.data.reason, fact.data.tick_id) == ("occurrence", tid(at(10, 25)))
    assert h.windows == [(at(10, 16), at(10, 29))]  # read back to the cursor


def test_skip_on_an_earlier_missed_fire_is_recorded_with_its_own_reason_in_order():
    job = entry()
    h = Harness([job], [skip(job, at(10, 10))], FakeCursor(at(10, 0)))
    h.tick(at(10, 23))
    (run,) = h.commands()
    assert (run.data.trigger, run.data.tick_id) == ("misfire", tid(at(10, 20)))
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(10, 5)), MISFIRE_COALESCED),
        (tid(at(10, 10)), "occurrence"),
        (tid(at(10, 15)), MISFIRE_COALESCED),
    ]
    # Publish order: every skipped fact, then the misfire run.
    assert isinstance(h.sent[-1], CandybarInvokeCommand)


def test_gap_whose_only_missed_fire_is_skipped_runs_nothing():
    job = entry()
    h = Harness([job], [skip(job, at(10, 5))], FakeCursor(at(10, 0)))
    msg = h.tick(at(10, 7))
    assert msg.acks == 1 and h.commands() == []
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(10, 5)), "occurrence")
    ]


def test_skipped_tick_fire_after_a_gap_still_runs_the_misfire():
    job = entry()
    h = Harness([job], [skip(job, at(10, 25))], FakeCursor(at(10, 12)))
    h.tick(at(10, 25))
    assert [(c.data.trigger, c.data.tick_id) for c in h.commands()] == [
        ("misfire", tid(at(10, 20)))
    ]
    assert [(s.data.tick_id, s.data.reason) for s in h.skipped()] == [
        (tid(at(10, 15)), MISFIRE_COALESCED),
        (tid(at(10, 25)), "occurrence"),
    ]


def test_skip_older_than_the_catch_up_window_gets_no_fact():
    job = entry("0 * * * *")
    tick = at(12, 30)
    old = tick - timedelta(hours=26)
    h = Harness([job], [skip(job, old.replace(minute=0))], FakeCursor(tick - timedelta(hours=30)))
    h.tick(tick)
    (run,) = h.commands()
    assert run.data.tick_id == tid(at(12, 0))
    reasons = {s.data.reason for s in h.skipped()}
    assert reasons == {MISFIRE_COALESCED}  # the 26h-old skipped fire is past the cap
    assert len(h.skipped()) == 23


def test_planner_lists_overrides_separately_and_keeps_coalesced_unchanged():
    job = entry()
    skips = group_skips([skip(job, at(10, 10))])
    (plan,) = plan_misfires([job], at(10, 0), at(10, 23), skips=skips)
    assert plan.latest == at(10, 20)
    assert plan.skipped == [at(10, 5), at(10, 15)]
    assert plan.overridden == [at(10, 10)]
    (plain,) = plan_misfires([job], at(10, 0), at(10, 23))
    assert plain.skipped == [at(10, 5), at(10, 10), at(10, 15)] and plain.overridden == []


# ---------------------------------------------------------------------------
# Identity, redelivery, failure, regression
# ---------------------------------------------------------------------------


def test_occurrence_skipped_identity_is_deterministic_across_paths():
    job = entry()
    fire = at(10, 10)
    on_tick = operator_skipped_for(job, fire, "occurrence", build_tick_fired(fire))
    in_gap = operator_skipped_for(job, fire, "occurrence", build_tick_fired(at(10, 23)))
    assert on_tick.id == in_gap.id
    # Distinct from the coalesced fact for the same fire, and from other fires.
    assert on_tick.id != misfire_skipped_for(job, fire, build_tick_fired(at(10, 23))).id
    assert on_tick.id != operator_skipped_for(job, at(10, 15), "occurrence", build_tick_fired(fire)).id
    tick = build_tick_fired(fire)
    linked = operator_skipped_for(job, fire, "occurrence", tick)
    assert linked.causationid == tick.id and linked.correlationid == tick.correlationid
    assert linked.ordering_key == f"candybar:{job.id}"


def test_redelivered_tick_republishes_the_same_skipped_fact():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [skip(job, at(10, 10))], cursor)
    h.tick(at(10, 10))
    h.tick(at(10, 10))  # redelivery: tick <= cursor
    first, second = h.skipped()
    assert first.id == second.id
    assert h.commands() == []


def test_skips_read_failure_naks_without_publishing_and_redelivery_recovers():
    job = entry()
    cursor = FakeCursor(at(10, 9))
    h = Harness([job], [skip(job, at(10, 10))], cursor, fail_skips=True)
    first = h.tick(at(10, 10))
    assert (first.acks, first.naks) == (0, 1)
    assert h.sent == [] and cursor.advances == []
    retry = h.tick(at(10, 10))
    assert (retry.acks, retry.naks) == (1, 0)
    assert h.commands() == [] and len(h.skipped()) == 1


@pytest.mark.parametrize("cursor", [None, at(10, 9), at(10, 0)])
def test_without_a_skips_port_dispatch_is_exactly_story_2_2(cursor):
    job = entry()
    rows = [skip(job, at(10, 10))]
    unwired = Harness([job], rows, FakeCursor(cursor), wire_skips=False)
    empty = Harness([job], [], FakeCursor(cursor))
    unwired.tick(at(10, 10))
    empty.tick(at(10, 10))
    assert [e.id for e in unwired.sent] == [e.id for e in empty.sent]
    assert unwired.commands()
    assert all(s.data.reason != "occurrence" for s in unwired.skipped())


def test_skip_ids_are_deterministic_per_candybar_and_fire_minute():
    a, b = uuid4(), uuid4()
    assert occurrence_skip_id(a, at(10, 10)) == occurrence_skip_id(a, at(10, 10))
    assert occurrence_skip_id(a, at(10, 10)) != occurrence_skip_id(a, at(10, 15))
    assert occurrence_skip_id(a, at(10, 10)) != occurrence_skip_id(b, at(10, 10))
    # Fire times are UTC minutes (AD-13): another offset is refused, not guessed.
    with pytest.raises(ValueError, match="UTC"):
        occurrence_skip_id(a, at(10, 10).astimezone(timezone(timedelta(hours=2))))


@pytest.mark.parametrize(
    "bad",
    [datetime(2026, 10, 26, 10, 10), at(10, 10) + timedelta(seconds=30)],
    ids=["naive", "off-minute"],
)
def test_bad_fire_time_is_rejected_before_touching_the_registry(bad):
    class NoSession:
        def __getattr__(self, name):
            raise AssertionError("registry touched for an invalid fire_time")

    with pytest.raises(ValueError, match="fire_time"):
        asyncio.run(add_occurrence_skip(NoSession(), uuid4(), bad))
    with pytest.raises(ValueError, match="fire_time"):
        occurrence_skip_id(uuid4(), bad)


def test_skipped_tick_fire_is_still_a_matching_fire():
    # The skip suppresses dispatch, not the schedule: matches_tick is unchanged,
    # which is what the API's "is this a real fire time" 404 check (5.6) uses.
    assert matches_tick(EVERY5, at(10, 10))
