"""Story 2.2 — misfire coalesce-to-latest (AD-6), unit level.

Every row of the spec's I/O matrix against the pure planner and the
Dispatcher with an in-memory cursor; the live proof (real roles, real bus,
real rows) is tests/test_misfire_integration.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from candybar.dispatcher import Dispatcher, command_for
from candybar.dispatcher.schedule import ceil_minute, fires_between, previous_fire
from candybar.dispatcher.worker import (
    CATCH_UP_WINDOW,
    MISFIRE_COALESCED,
    _num_pending,
    misfire_command_for,
    misfire_skipped_for,
    plan_misfires,
)
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand, CandybarSkippedEvent
from candybar.events.vocabulary import tick_id_for_minute
from candybar.registry.store import ScheduleEntry

T = datetime(2026, 10, 26, 10, 23, tzinfo=UTC)
CURSOR = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
LONG_AGO = datetime(2026, 1, 1, tzinfo=UTC)


def at(hour: int, minute: int, day: int = 26) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=UTC)


def tid(minute: datetime) -> str:
    return tick_id_for_minute(int(minute.timestamp()) // 60)


def entry(schedule: str, name: str = "job", created_at: datetime | None = LONG_AGO) -> ScheduleEntry:
    return ScheduleEntry(uuid4(), name, schedule, created_at)


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
    def __init__(self, minute: datetime | None, *, fail_advance: bool = False):
        self.minute = minute
        self.fail_advance = fail_advance
        self.advances: list[datetime] = []

    async def load(self):
        return self.minute

    async def advance(self, minute):
        if self.fail_advance:
            raise ConnectionError("registry down")
        self.advances.append(minute)
        self.minute = minute if self.minute is None else max(self.minute, minute)


class Harness:
    def __init__(self, entries, cursor, *, fail_on=None, window=CATCH_UP_WINDOW):
        self.entries = entries
        self.cursor = cursor
        self.sent: list = []
        self.fail_on = fail_on

        async def schedules():
            return list(self.entries)

        async def publish(event):
            if self.fail_on is not None and self.fail_on(event):
                self.fail_on = None  # one-shot
                raise ConnectionError("bus unavailable")
            # Every published envelope must survive the wire contract.
            assert parse_envelope(json.loads(event.model_dump_json())) == event
            self.sent.append(event)

        self.dispatcher = Dispatcher(schedules, publish, cursor=cursor, catch_up_window=window)

    def tick(self, minute: datetime, pending: int | None = None) -> Message:
        msg = Message(build_tick_fired(minute).model_dump_json().encode(), pending)
        asyncio.run(self.dispatcher.handle_message(msg))
        return msg

    def commands(self, trigger: str | None = None) -> list[CandybarInvokeCommand]:
        return [
            e for e in self.sent
            if isinstance(e, CandybarInvokeCommand) and (trigger is None or e.data.trigger == trigger)
        ]

    def skipped(self) -> list[CandybarSkippedEvent]:
        return [e for e in self.sent if isinstance(e, CandybarSkippedEvent)]


# ---------------------------------------------------------------------------
# Pure fire-time helpers (UTC, strict/half-open bounds)
# ---------------------------------------------------------------------------


def test_previous_fire_is_strictly_before():
    assert previous_fire("*/5 * * * *", at(10, 20)) == at(10, 15)
    assert previous_fire("*/5 * * * *", at(10, 21)) == at(10, 20)
    assert previous_fire("0 * * * *", at(10, 0)) == at(9, 0)
    with pytest.raises(ValueError, match="UTC"):
        previous_fire("* * * * *", datetime(2026, 10, 26, 10, 0))
    with pytest.raises(ValueError, match="five fields"):
        previous_fire("* * * * * *", at(10, 0))


def test_fires_between_is_half_open():
    assert list(fires_between("*/5 * * * *", at(10, 5), at(10, 20))) == [
        at(10, 5), at(10, 10), at(10, 15)
    ]
    assert list(fires_between("*/5 * * * *", at(10, 6), at(10, 10))) == []
    assert list(fires_between("*/5 * * * *", at(10, 20), at(10, 20))) == []
    with pytest.raises(ValueError, match="minute boundary"):
        list(fires_between("* * * * *", at(10, 0) + timedelta(seconds=1), at(10, 5)))


def test_never_firing_schedule_has_no_fires():
    # Valid syntax (registration accepts it), but February 30th never comes.
    assert previous_fire("0 0 30 2 *", at(10, 0)) is None
    assert list(fires_between("0 0 30 2 *", at(10, 0), at(11, 0))) == []


def test_never_firing_candybar_cannot_wedge_the_gap_tick():
    never = entry("0 0 30 2 *", "never")
    job = entry("*/5 * * * *", "job")
    fake = FakeCursor(CURSOR)
    h = Harness([never, job], fake)
    msg = h.tick(T)
    assert (msg.acks, msg.naks) == (1, 0)  # not a nak loop on every later tick
    assert [c.data.name for c in h.commands("misfire")] == ["job"]
    assert fake.minute == T


def test_ceil_minute():
    assert ceil_minute(at(10, 12)) == at(10, 12)
    assert ceil_minute(at(10, 12) + timedelta(seconds=30)) == at(10, 13)
    assert ceil_minute(at(10, 12) + timedelta(microseconds=1)) == at(10, 13)


# ---------------------------------------------------------------------------
# Matrix: N>1 missed, N=1 missed
# ---------------------------------------------------------------------------


def test_n_missed_runs_only_latest_as_misfire_and_skips_the_rest():
    job = entry("*/5 * * * *")
    h = Harness([job], FakeCursor(CURSOR))
    msg = h.tick(T)  # missed: 10:05 10:10 10:15 10:20
    assert (msg.acks, msg.naks, msg.terms) == (1, 0, 0)
    misfires = h.commands("misfire")
    assert len(misfires) == 1 and not h.commands("scheduled")  # 10:23 is no */5 fire
    run = misfires[0]
    assert run.data.tick_id == tid(at(10, 20))
    assert run.idempotency_key == f"candybar:{job.id}:tick:{tid(at(10, 20))}"
    assert run.subject == "bloodbank.cmd.cron.candybar.invoke"
    skipped = h.skipped()
    assert [s.data.tick_id for s in skipped] == [tid(at(10, m)) for m in (5, 10, 15)]
    assert {s.data.reason for s in skipped} == {MISFIRE_COALESCED} == {"misfire_coalesced"}
    assert all(s.subject == "bloodbank.evt.cron.candybar.skipped" for s in skipped)
    assert all(s.ordering_key == f"candybar:{job.id}" for s in skipped)
    # Facts land oldest-first, then the one catch-up run.
    assert h.sent[-1] is run
    assert h.cursor.minute == T


def test_single_missed_fire_runs_as_misfire_without_skips():
    job = entry("*/5 * * * *")
    h = Harness([job], FakeCursor(CURSOR))
    h.tick(at(10, 7))
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(10, 5))]
    assert h.skipped() == []


def test_tick_own_fire_still_dispatches_scheduled_alongside_the_misfire():
    job = entry("*/5 * * * *")
    h = Harness([job], FakeCursor(CURSOR))
    h.tick(at(10, 25))  # missed 10:05..10:20; 10:25 is the tick's own fire
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(10, 20))]
    assert [c.data.tick_id for c in h.commands("scheduled")] == [tid(at(10, 25))]
    assert len(h.skipped()) == 3


# ---------------------------------------------------------------------------
# Matrix: downtime > 24h (cap), latest older than 24h
# ---------------------------------------------------------------------------


def test_downtime_beyond_24h_still_runs_only_latest_and_caps_skips(caplog):
    job = entry("0 * * * *")
    tick = at(10, 30)
    h = Harness([job], FakeCursor(tick - timedelta(hours=30)))
    with caplog.at_level(logging.WARNING):
        h.tick(tick)
    misfires = h.commands("misfire")
    assert [c.data.tick_id for c in misfires] == [tid(at(10, 0))]
    skipped = h.skipped()
    floor = tick - CATCH_UP_WINDOW  # 10:30 the day before
    fires = [datetime.fromtimestamp(int(s.data.tick_id[5:]) * 60, UTC) for s in skipped]
    assert fires and min(fires) >= floor
    assert fires == [at(h_, 0, 25) for h_ in range(11, 24)] + [at(h_, 0) for h_ in range(10)]
    assert len(skipped) == 23  # hourly fires in [T-24h, latest), not the 30h of downtime
    assert "exceeds the" in caplog.text and "catch-up window" in caplog.text


def test_latest_missed_fire_older_than_24h_still_runs():
    weekly = entry("0 3 * * 1")  # Mondays 03:00; 2026-10-26 is a Monday
    tick = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)  # Thursday
    h = Harness([weekly], FakeCursor(tick - timedelta(days=5)))
    h.tick(tick)
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(3, 0))]
    assert h.skipped() == []


# ---------------------------------------------------------------------------
# Matrix: no missed fires, created during downtime
# ---------------------------------------------------------------------------


def test_candybars_without_missed_fires_dispatch_normally():
    yearly = entry("0 0 1 1 *", "yearly")
    every = entry("* * * * *", "every")
    hourly = entry("0 * * * *", "hourly")  # no hourly fire in 10:00 < t < 10:23
    h = Harness([yearly, every, hourly], FakeCursor(CURSOR))
    h.tick(T)
    by_name = {c.data.name: c for c in h.commands("misfire")}
    assert set(by_name) == {"every"}  # only the minutely job missed anything
    assert {c.data.name for c in h.commands("scheduled")} == {"every"}
    assert all(s.data.name == "every" for s in h.skipped())
    assert len(h.skipped()) == 21  # 10:01..10:21; 10:22 runs; 10:23 scheduled


def test_fires_before_registration_are_not_missed():
    job = entry("*/5 * * * *", created_at=at(10, 12) + timedelta(seconds=30))
    h = Harness([job], FakeCursor(CURSOR))
    h.tick(T)
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(10, 20))]
    assert [s.data.tick_id for s in h.skipped()] == [tid(at(10, 15))]


def test_registered_after_the_latest_missed_fire_has_nothing_missed():
    job = entry("*/5 * * * *", created_at=at(10, 21))
    h = Harness([job], FakeCursor(CURSOR))
    h.tick(T)
    assert h.commands() == [] and h.skipped() == []


def test_unknown_created_at_uses_only_the_cursor_floor():
    job = entry("*/5 * * * *", created_at=None)
    plans = plan_misfires([job], CURSOR, T)
    assert plans[0].latest == at(10, 20)
    assert plans[0].skipped == [at(10, 5), at(10, 10), at(10, 15)]


# ---------------------------------------------------------------------------
# Matrix: contiguous / old / first tick — exactly Story 2.1 behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cursor", "minute", "pending", "advanced"),
    [
        (at(10, 22), T, 0, [T]),  # contiguous, nothing queued behind it
        # Not ahead of the cursor: queued ticks are irrelevant, never deferred.
        (at(10, 30), T, 3, []),  # old tick (redelivery / replay): cursor never rewinds
        (T, T, 3, []),  # same-minute redelivery
        (None, T, 3, [T]),  # fresh install: no misfire, cursor initialised
    ],
)
def test_no_gap_dispatches_exactly_as_before(cursor, minute, pending, advanced):
    job = entry("* * * * *")
    fake = FakeCursor(cursor)
    h = Harness([job], fake)
    msg = h.tick(minute, pending=pending)
    assert msg.acks == 1
    assert [c.data.trigger for c in h.sent] == ["scheduled"]
    assert h.sent[0] == command_for(job, build_tick_fired(minute))
    assert fake.advances == advanced


def test_no_cursor_wired_is_story_2_1_behaviour():
    job = entry("* * * * *")

    sent = []

    async def schedules():
        return [job]

    async def publish(event):
        sent.append(event)

    msg = Message(build_tick_fired(T).model_dump_json().encode(), pending=5)
    asyncio.run(Dispatcher(schedules, publish).handle_message(msg))
    assert msg.acks == 1 and [c.data.trigger for c in sent] == ["scheduled"]


# ---------------------------------------------------------------------------
# Matrix: backlog ticks (dispatcher-only downtime behind a live heartbeat)
# ---------------------------------------------------------------------------


def test_backlog_ticks_defer_and_the_newest_coalesces_the_whole_gap():
    job = entry("*/5 * * * *")
    fake = FakeCursor(CURSOR)
    h = Harness([job], fake)
    backlog = [CURSOR + timedelta(minutes=m) for m in range(1, 24)]  # 10:01 .. 10:23
    msgs = [h.tick(m, pending=len(backlog) - i - 1) for i, m in enumerate(backlog)]
    assert all((m.acks, m.naks) == (1, 0) for m in msgs)
    # Nothing went out as scheduled for the 22 backlog minutes...
    assert h.commands("scheduled") == []
    assert fake.advances == [T]  # ...and the cursor only moved at the newest tick.
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(10, 20))]
    assert [s.data.tick_id for s in h.skipped()] == [tid(at(10, m)) for m in (5, 10, 15)]


def test_deferred_minute_is_recovered_by_the_next_newer_tick():
    """Out-of-order replay of an older minute after a deferral: the deferred
    minute is not lost — the next newer tick's gap covers it."""
    job = entry("* * * * *")
    fake = FakeCursor(at(10, 0))
    h = Harness([job], fake)
    h.tick(at(10, 2), pending=1)  # deferred (an older replay is queued behind it)
    h.tick(at(10, 1), pending=0)  # contiguous: scheduled, cursor 10:01
    h.tick(at(10, 3), pending=0)  # gap covers 10:02 -> misfire
    assert [c.data.tick_id for c in h.commands("scheduled")] == [tid(at(10, 1)), tid(at(10, 3))]
    assert [c.data.tick_id for c in h.commands("misfire")] == [tid(at(10, 2))]


def test_num_pending_is_defensive():
    class Raising:
        @property
        def metadata(self):
            raise RuntimeError("not a JetStream message")

    assert _num_pending(object()) == 0
    assert _num_pending(Raising()) == 0
    assert _num_pending(Message(b"", pending=0)) == 0
    assert _num_pending(Message(b"", pending=7)) == 7

    from nats.aio.msg import Msg

    real = Msg(None, subject="x", reply="$JS.ACK.BLOODBANK_EVENTS.d.1.5.5.1700000000000000000.4")
    assert _num_pending(real) == 4
    assert _num_pending(Msg(None, subject="x", reply="")) == 0


# ---------------------------------------------------------------------------
# Matrix: publish / cursor failure — nak, stable ids, cursor untouched
# ---------------------------------------------------------------------------


def test_publish_failure_naks_and_redelivery_rebuilds_identical_ids():
    job = entry("*/5 * * * *")
    fake = FakeCursor(CURSOR)
    h = Harness([job], fake, fail_on=lambda e: isinstance(e, CandybarInvokeCommand))
    first = h.tick(T)
    assert (first.acks, first.naks) == (0, 1)
    assert fake.advances == [] and fake.minute == CURSOR
    before = list(h.sent)  # the skipped facts that landed pre-failure
    retry = h.tick(T)
    assert (retry.acks, retry.naks) == (1, 0)
    again = h.sent[len(before):]
    assert [e.id for e in again[: len(before)]] == [e.id for e in before]
    assert fake.minute == T


def test_cursor_failure_naks_after_publishing():
    job = entry("*/5 * * * *")
    h = Harness([job], FakeCursor(CURSOR, fail_advance=True))
    msg = h.tick(T)
    assert (msg.acks, msg.naks) == (0, 1)


def test_misfire_identity_is_deterministic_and_shares_the_scheduled_key():
    job = entry("*/5 * * * *")
    fire = at(10, 20)
    via_t23 = misfire_command_for(job, fire, build_tick_fired(T))
    via_t30 = misfire_command_for(job, fire, build_tick_fired(at(10, 30)))
    assert via_t23.id == via_t30.id == via_t23.command_id
    assert via_t23.data.invocation_id == via_t30.data.invocation_id
    scheduled = command_for(job, build_tick_fired(fire))
    # One fire time, one execution whatever the trigger (AD-5 key) ...
    assert via_t23.idempotency_key == scheduled.idempotency_key
    # ... but a distinct command identity on the wire.
    assert via_t23.id != scheduled.id
    assert via_t23.data.invocation_id != scheduled.data.invocation_id
    assert via_t23.data.trigger == "misfire" and via_t23.correlationid == via_t23.command_id
    s1 = misfire_skipped_for(job, at(10, 5), build_tick_fired(T))
    s2 = misfire_skipped_for(job, at(10, 5), build_tick_fired(at(10, 30)))
    assert s1.id == s2.id
    tick = build_tick_fired(T)
    linked = misfire_skipped_for(job, at(10, 5), tick)
    assert linked.causationid == tick.id and linked.correlationid == tick.correlationid
