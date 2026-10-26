"""Per-candybar IANA timezone and the DST-correct fire-time helper (Story 7.1, AD-16).

`fire_times(schedule, tz, start_utc, end_utc)` is the only source of fire
instants. These tests pin its Vixie-cron DST semantics on the 2026-11-01
fall-back and the 2027-03-14 spring-forward (America/New_York), prove it
against an independent minute-by-minute reference across several zones, and
check every consumer that rides on it: the tick match, next/previous fire,
misfire planning, rest-of-day skips (a candybar-local date), the manifest and
API validation, and the dispatcher end to end.
"""

from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from croniter import croniter

from candybar.dispatcher import Dispatcher
from candybar.dispatcher.firetimes import (
    hour_is_wildcard,
    local_date,
    local_day_start,
    never_fires,
)
from candybar.dispatcher.schedule import (
    SkipSet,
    fire_times,
    fires_between,
    group_skips,
    matches_tick,
    next_fire,
    previous_fire,
    widen_skip_window,
)
from candybar.dispatcher.worker import plan_misfires
from candybar.events import build_tick_fired, parse_envelope
from candybar.events.envelope import CandybarInvokeCommand
from candybar.registry import CandybarManifest, ManifestError, load_manifest
from candybar.registry.models import SKIP_REST_OF_DAY
from candybar.registry.store import ScheduleEntry, SkipEntry
from candybar.timezones import TimezoneError, resolve_timezone

NY = ZoneInfo("America/New_York")
ONE_MINUTE = timedelta(minutes=1)


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def fires(schedule: str, tz, start: datetime, end: datetime) -> list[datetime]:
    return list(fire_times(schedule, tz, start, end))


# ---------------------------------------------------------------------------
# The AD-16 pins (the story's acceptance criteria, verbatim).

FALL_BACK_NIGHT = (utc(2026, 10, 31, 12), utc(2026, 11, 2, 12))
SPRING_FORWARD_DAYS = (utc(2027, 3, 13), utc(2027, 3, 15, 12))


def test_fixed_hour_in_the_repeated_hour_fires_once_at_the_first_occurrence() -> None:
    got = fires("40 1 * * *", NY, *FALL_BACK_NIGHT)
    # 01:40 EDT Nov 1 (05:40Z) once, never again at 01:40 EST (06:40Z); then the
    # next day's 01:40 EST (06:40Z) as usual.
    assert got == [utc(2026, 11, 1, 5, 40), utc(2026, 11, 2, 6, 40)]
    assert utc(2026, 11, 1, 6, 40) not in got


def test_fixed_hour_in_the_spring_forward_gap_fires_once_after_the_gap() -> None:
    got = fires("30 2 * * *", NY, *SPRING_FORWARD_DAYS)
    # 02:30 EST Mar 13 (07:30Z); the gap day has no 02:30, so it fires once at
    # the first existing minute after the gap, 03:00 EDT = 07:00Z; Mar 15 02:30
    # EDT is 06:30Z.
    assert got == [utc(2027, 3, 13, 7, 30), utc(2027, 3, 14, 7, 0), utc(2027, 3, 15, 6, 30)]


@pytest.mark.parametrize("window", [FALL_BACK_NIGHT, SPRING_FORWARD_DAYS])
def test_half_hourly_fires_at_every_real_half_hour_through_both_transitions(window) -> None:
    got = fires("*/30 * * * *", NY, *window)
    start, end = window
    assert got[0] == start and got[-1] == end - timedelta(minutes=30)
    assert all(b - a == timedelta(minutes=30) for a, b in itertools.pairwise(got))
    assert len(got) == int((end - start) / timedelta(minutes=30))


@pytest.mark.parametrize("window", [FALL_BACK_NIGHT, SPRING_FORWARD_DAYS])
def test_hourly_is_unaffected_real_hours_every_hour(window) -> None:
    got = fires("0 * * * *", NY, *window)
    start, end = window
    assert len(got) == int((end - start) / timedelta(hours=1))
    assert all(b - a == timedelta(hours=1) for a, b in itertools.pairwise(got))


def test_fall_back_hour_runs_a_wildcard_hour_expression_twice() -> None:
    got = fires("0 * * * *", NY, utc(2026, 11, 1, 4), utc(2026, 11, 1, 8))
    # 00:00 EDT, 01:00 EDT, 01:00 EST, 02:00 EST
    assert got == [utc(2026, 11, 1, 4), utc(2026, 11, 1, 5), utc(2026, 11, 1, 6), utc(2026, 11, 1, 7)]


def test_a_wildcard_hour_expression_does_not_catch_up_the_skipped_wall_minutes() -> None:
    got = fires("30 * * * *", NY, utc(2027, 3, 14, 5), utc(2027, 3, 14, 10))
    # 00:30 EST, 01:30 EST, [02:30 does not exist], 03:30 EDT, 04:30 EDT, 05:30 EDT
    assert got == [
        utc(2027, 3, 14, 5, 30),
        utc(2027, 3, 14, 6, 30),
        utc(2027, 3, 14, 7, 30),
        utc(2027, 3, 14, 8, 30),
        utc(2027, 3, 14, 9, 30),
    ]


def test_several_gap_wall_times_collapse_into_one_fire() -> None:
    # 02:00 and 02:30 both fall in the gap: ONE fire at the transition instant.
    assert fires("0,30 2 * * *", NY, *SPRING_FORWARD_DAYS)[2:3] == [utc(2027, 3, 14, 7, 0)]
    # ...and a genuine 03:00 fire coinciding with it is not doubled.
    assert fires("0 2,3 * * *", NY, utc(2027, 3, 14, 6), utc(2027, 3, 14, 9)) == [
        utc(2027, 3, 14, 7, 0)
    ]


def test_a_fixed_hour_range_across_the_gap_keeps_its_other_hours() -> None:
    got = fires("45 1-3 * * *", NY, utc(2027, 3, 14, 5), utc(2027, 3, 14, 10))
    # 01:45 EST, [02:45 gap -> 03:00 EDT], 03:45 EDT
    assert got == [utc(2027, 3, 14, 6, 45), utc(2027, 3, 14, 7, 0), utc(2027, 3, 14, 7, 45)]


def test_fall_back_expressions_outside_the_repeated_hour_fire_once() -> None:
    for sched, expected in (
        ("30 0 * * *", [utc(2026, 11, 1, 4, 30)]),  # 00:30 EDT
        ("30 2 * * *", [utc(2026, 11, 1, 7, 30)]),  # 02:30 EST
    ):
        assert fires(sched, NY, utc(2026, 11, 1, 3), utc(2026, 11, 1, 9)) == expected


def test_utc_candybars_are_plain_croniter_utc() -> None:
    got = fires("40 1 * * *", UTC, utc(2026, 11, 1), utc(2026, 11, 3))
    assert got == [utc(2026, 11, 1, 1, 40), utc(2026, 11, 2, 1, 40)]
    walk = croniter("*/7 3-5 * * 1-5", utc(2026, 10, 1))
    expected = []
    while (n := walk.get_next(datetime)) < utc(2026, 10, 15):
        expected.append(n)
    assert fires("*/7 3-5 * * 1-5", UTC, utc(2026, 10, 1), utc(2026, 10, 15)) == expected


def test_hour_wildcard_detection() -> None:
    assert hour_is_wildcard("*/30 * * * *") and hour_is_wildcard("0 * * * *")
    assert not hour_is_wildcard("0 */2 * * *") and not hour_is_wildcard("0 1 * * *")


# ---------------------------------------------------------------------------
# An independent reference: scan every real minute, apply the Vixie rules.

ZONES = ["America/New_York", "Europe/London", "Australia/Lord_Howe", "America/St_Johns", "Europe/Dublin"]
SCHEDULES = [
    "40 1 * * *",
    "30 2 * * *",
    "*/30 * * * *",
    "*/7 * * * *",
    "0 * * * *",
    "15 2 * * 1-5",
    "0,30 2 * * *",
    "0 2,3 * * *",
    "45 1-3 * * *",
    "0 */2 * * *",
    "59 1 * * *",
    "0 3 * * *",
    "10 0 * * *",
    "* * * * *",
]


def transitions(tz: ZoneInfo, start: datetime, end: datetime) -> list[datetime]:
    """The UTC minutes in [start, end) where the zone's offset changes (daily scan + bisect)."""
    out = []
    probe = start
    while probe < end:
        nxt = probe + timedelta(days=1)
        if probe.astimezone(tz).utcoffset() != nxt.astimezone(tz).utcoffset():
            lo, hi = probe, nxt
            while hi - lo > ONE_MINUTE:
                mid = lo + (hi - lo) // 2
                mid = mid.replace(second=0, microsecond=0)
                if mid.astimezone(tz).utcoffset() == probe.astimezone(tz).utcoffset():
                    lo = mid
                else:
                    hi = mid
            out.append(hi)
        probe = nxt
    return out


def reference_fires(schedule: str, tz: ZoneInfo, start: datetime, end: datetime) -> list[datetime]:
    """Minute-by-minute Vixie semantics, sharing nothing with the helper but croniter's naive walk."""
    wild = schedule.split()[1] in ("*", "*/1")
    walls: set[datetime] = set()
    walk = croniter(schedule, (start - timedelta(days=2)).replace(tzinfo=None))
    limit = (end + timedelta(days=2)).replace(tzinfo=None)
    while (w := walk.get_next(datetime)) < limit:
        walls.add(w)
    out = []
    minute = start
    while minute < end:
        local = minute.astimezone(tz)
        wall = local.replace(tzinfo=None)
        hit = wall in walls and (wild or local.fold == 0)
        before = (minute - ONE_MINUTE).astimezone(tz)
        if not hit and not wild and local.utcoffset() > before.utcoffset():  # spring forward at this minute
            gap = before.replace(tzinfo=None) + ONE_MINUTE
            while gap < wall:
                hit = hit or gap in walls
                gap += ONE_MINUTE
        if hit:
            out.append(minute)
        minute += ONE_MINUTE
    return out


@pytest.mark.parametrize("zone", ZONES)
def test_fire_times_equals_the_minute_by_minute_reference_around_every_transition(zone) -> None:
    tz = ZoneInfo(zone)
    found = transitions(tz, utc(2026, 8, 1), utc(2027, 5, 1))
    assert found, f"{zone} has no transition in the scan range"
    for moment in found:
        window = (moment - timedelta(hours=14), moment + timedelta(hours=14))
        for schedule in SCHEDULES:
            assert fires(schedule, tz, *window) == reference_fires(schedule, tz, *window), (
                zone,
                moment,
                schedule,
            )


@pytest.mark.parametrize("zone", ["America/New_York", "Australia/Lord_Howe"])
def test_the_tick_match_is_membership_in_the_one_minute_window(zone) -> None:
    """The dispatcher's match: tick in fire_times(tick, tick + 1m), every minute of a transition."""
    tz = ZoneInfo(zone)
    (moment, *_) = transitions(tz, utc(2026, 10, 1), utc(2027, 5, 1))
    start = moment - timedelta(hours=3)
    for schedule in ("40 1 * * *", "30 2 * * *", "*/30 * * * *", "0 * * * *", "0,30 2 * * *"):
        window = set(fires(schedule, tz, start, start + timedelta(hours=6)))
        for k in range(360):
            tick = start + timedelta(minutes=k)
            assert matches_tick(schedule, tick, tz) == (tick in window), (schedule, tick)


# ---------------------------------------------------------------------------
# The helpers built on it.


def test_fire_times_is_lazy_validates_eagerly_and_never_fires_is_empty() -> None:
    with pytest.raises(ValueError, match="UTC"):
        fire_times("* * * * *", NY, datetime(2026, 1, 1), utc(2026, 1, 2))
    with pytest.raises(ValueError, match="minute boundary"):
        fire_times("* * * * *", NY, utc(2026, 1, 1, 0, 0, 30), utc(2026, 1, 2))
    with pytest.raises(ValueError, match="five fields"):
        fire_times("* * * * * *", NY, utc(2026, 1, 1), utc(2026, 1, 2))
    assert fires("0 0 30 2 *", NY, utc(2026, 1, 1), utc(2030, 1, 1)) == []
    assert never_fires("0 0 30 2 *") and not never_fires("0 0 29 2 *")
    assert fires("* * * * *", NY, utc(2026, 1, 1), utc(2026, 1, 1)) == []
    # A month of minutely fires is never materialized to take its first.
    first = next(fire_times("* * * * *", NY, utc(2026, 1, 1), utc(2026, 2, 1)))
    assert first == utc(2026, 1, 1)


def test_next_fire_crosses_the_transitions_in_real_time() -> None:
    assert next_fire("40 1 * * *", utc(2026, 11, 1, 5, 41), tz=NY) == utc(2026, 11, 2, 6, 40)
    assert next_fire("30 2 * * *", utc(2027, 3, 14, 6), tz=NY) == utc(2027, 3, 14, 7)
    assert next_fire("0 0 29 2 *", utc(2026, 1, 1), tz=NY) == utc(2028, 2, 29, 5)
    assert next_fire("0 0 30 2 *", utc(2026, 1, 1), tz=NY) is None


def test_previous_fire_crosses_the_transitions_in_real_time() -> None:
    assert previous_fire("40 1 * * *", utc(2026, 11, 2, 6, 40), tz=NY) == utc(2026, 11, 1, 5, 40)
    assert previous_fire("30 2 * * *", utc(2027, 3, 15, 6, 30), tz=NY) == utc(2027, 3, 14, 7)
    assert previous_fire("0 0 29 2 *", utc(2026, 1, 1), tz=NY) == utc(2024, 2, 29, 5)
    assert previous_fire("0 0 30 2 *", utc(2026, 1, 1), tz=NY) is None


def test_skip_walks_honor_the_candybars_timezone() -> None:
    skips = SkipSet(occurrences=frozenset({utc(2026, 11, 1, 5, 40)}), tz=NY)
    assert next_fire("40 1 * * *", utc(2026, 11, 1), tz=NY, skips=skips) == utc(2026, 11, 2, 6, 40)
    # The fall-back fire is skipped: the previous one is the night before.
    assert previous_fire("40 1 * * *", utc(2026, 11, 2, 6, 40), tz=NY, skips=skips) == utc(
        2026, 10, 31, 5, 40
    )
    assert list(fires_between("40 1 * * *", *FALL_BACK_NIGHT, tz=NY, skips=skips)) == [
        utc(2026, 11, 2, 6, 40)
    ]


def test_local_day_boundaries_across_both_transitions() -> None:
    assert local_day_start(date(2026, 11, 1), NY) == utc(2026, 11, 1, 4)  # 00:00 EDT
    assert local_day_start(date(2026, 11, 2), NY) == utc(2026, 11, 2, 5)  # 00:00 EST: 25h day
    assert local_day_start(date(2027, 3, 14), NY) == utc(2027, 3, 14, 5)  # 00:00 EST
    assert local_day_start(date(2027, 3, 15), NY) == utc(2027, 3, 15, 4)  # 00:00 EDT: 23h day
    assert local_date(utc(2026, 11, 1, 3, 59), NY) == date(2026, 10, 31)
    assert local_date(utc(2026, 11, 1, 4), NY) == date(2026, 11, 1)


def test_a_local_midnight_inside_a_gap_starts_the_day_at_the_transition() -> None:
    # America/Sao_Paulo sprang forward at 00:00 on 2018-11-04: 00:00-00:59 never existed.
    sao_paulo = ZoneInfo("America/Sao_Paulo")
    assert local_day_start(date(2018, 11, 4), sao_paulo) == utc(2018, 11, 4, 3)  # 01:00 -02
    assert local_day_start(date(2018, 11, 5), sao_paulo) == utc(2018, 11, 5, 2)
    skips = SkipSet(rest_of_day=frozenset({date(2018, 11, 4)}), tz=sao_paulo)
    assert skips.covers(utc(2018, 11, 4, 3)) and not skips.covers(utc(2018, 11, 4, 2, 59))
    assert next_fire("0 * * * *", utc(2018, 11, 4, 3), tz=sao_paulo, skips=skips) == utc(2018, 11, 5, 2)


@pytest.mark.parametrize(
    ("day", "first", "after_last"),
    [
        (date(2026, 11, 1), utc(2026, 11, 1, 4), utc(2026, 11, 2, 5)),
        (date(2027, 3, 14), utc(2027, 3, 14, 5), utc(2027, 3, 15, 4)),
    ],
)
def test_rest_of_day_is_a_candybar_local_date_across_both_transitions(day, first, after_last) -> None:
    skips = SkipSet(rest_of_day=frozenset({day}), tz=NY)
    assert not skips.covers(first - ONE_MINUTE)  # 23:59 the evening before
    assert skips.covers(first)
    assert skips.covers(after_last - ONE_MINUTE)  # 23:59 of the day itself
    assert not skips.covers(after_last)
    # The same date read as UTC covers a different 24 hours.
    assert SkipSet(rest_of_day=frozenset({day})).covers(first - ONE_MINUTE)
    # next_fire jumps the whole local day: the first fire after it, in real time.
    assert next_fire("*/30 * * * *", first, tz=NY, skips=skips) == after_last


def test_group_skips_hands_each_candybar_its_zone() -> None:
    a, b = uuid4(), uuid4()
    rows = [
        SkipEntry(uuid4(), a, SKIP_REST_OF_DAY, None, date(2026, 11, 1), None, utc(2026, 10, 30)),
        SkipEntry(uuid4(), b, SKIP_REST_OF_DAY, None, date(2026, 11, 1), None, utc(2026, 10, 30)),
    ]
    sets = group_skips(rows, {a: NY})
    assert sets[a].tz == NY and sets[b].tz == UTC
    instant = utc(2026, 11, 1, 3, 59)  # Oct 31 evening in New York, Nov 1 in UTC
    assert not sets[a].covers(instant) and sets[b].covers(instant)


def test_widen_skip_window_reaches_a_local_date_a_day_either_way() -> None:
    assert widen_skip_window(utc(2026, 11, 1, 10), utc(2026, 11, 1, 11)) == (
        utc(2026, 10, 31, 10),
        utc(2026, 11, 2, 11),
    )
    assert widen_skip_window(utc(2026, 11, 1, 10), None) == (utc(2026, 10, 31, 10), None)


# ---------------------------------------------------------------------------
# Misfire planning (AD-6) uses the same helper.


def test_misfire_planning_reads_the_schedule_in_the_candybars_timezone() -> None:
    entry = ScheduleEntry(uuid4(), "ifvault", "40 1 * * *", utc(2026, 1, 1), "America/New_York")
    # A gap straddling the fall-back night: the one fire at 05:40Z is missed.
    (plan,) = plan_misfires([entry], utc(2026, 11, 1, 5), utc(2026, 11, 1, 8))
    assert plan.latest == utc(2026, 11, 1, 5, 40) and plan.skipped == []
    # No second (06:40Z) fire to coalesce.
    assert plan_misfires([entry], utc(2026, 11, 1, 5, 41), utc(2026, 11, 1, 8)) == []


# ---------------------------------------------------------------------------
# Timezone names: registration, the manifest, the registry entry.


def test_resolve_timezone_accepts_iana_names_only() -> None:
    assert resolve_timezone("America/New_York") == NY
    assert resolve_timezone("UTC").key == "UTC"
    for bad in ("", " UTC", "Mars/Olympus", "../etc/passwd", "localtime", "posixrules", "EST5EDT/x"):
        with pytest.raises(TimezoneError):
            resolve_timezone(bad)
    with pytest.raises(TimezoneError):
        resolve_timezone(None)  # type: ignore[arg-type]


def test_manifest_timezone_defaults_to_utc_and_is_validated() -> None:
    base = {"name": "t", "schedule": "40 1 * * *", "entrypoint": "/bin/true"}
    assert CandybarManifest(**base).timezone == "UTC"
    assert CandybarManifest(**base, timezone="America/New_York").timezone == "America/New_York"
    for bad in ("Nowhere/City", "", "est", "America/New York"):
        with pytest.raises(ValueError, match="timezone"):
            CandybarManifest(**base, timezone=bad)
    with pytest.raises(ValueError, match="timezone"):
        CandybarManifest(**base, timezone="UTC\x00")


def test_candybar_toml_carries_a_timezone(tmp_path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    (tmp_path / "candybar.toml").write_text(
        '[candybar]\nname = "ifvault"\nschedule = "40 1 * * *"\ntimezone = "America/New_York"\n'
        'entrypoint = "run.sh"\n'
    )
    assert load_manifest(tmp_path).timezone == "America/New_York"
    (tmp_path / "candybar.toml").write_text(
        '[candybar]\nname = "ifvault"\nschedule = "40 1 * * *"\ntimezone = "Not/AZone"\n'
        'entrypoint = "run.sh"\n'
    )
    with pytest.raises(ManifestError, match="timezone"):
        load_manifest(tmp_path)


def test_schedule_entry_resolves_its_zone() -> None:
    assert ScheduleEntry(uuid4(), "a", "* * * * *").tz.key == "UTC"
    assert ScheduleEntry(uuid4(), "a", "* * * * *", None, "Asia/Tokyo").tz.key == "Asia/Tokyo"
    with pytest.raises(TimezoneError):
        ScheduleEntry(uuid4(), "a", "* * * * *", None, "Gone/Zone").tz  # noqa: B018


# ---------------------------------------------------------------------------
# The dispatcher end to end across the transition nights.


class Sink:
    def __init__(self) -> None:
        self.sent: list = []

    async def publish(self, event) -> None:
        self.sent.append(parse_envelope(event.model_dump(mode="json")))


def dispatch_every_minute(entries, start: datetime, end: datetime, skips=None) -> dict[str, list[datetime]]:
    sink = Sink()

    async def schedules():
        return entries

    async def run() -> None:
        dispatcher = Dispatcher(schedules, sink.publish, skips=skips)
        minute = start
        while minute < end:
            await dispatcher.dispatch(build_tick_fired(minute))
            minute += ONE_MINUTE

    asyncio.run(run())
    out: dict[str, list[datetime]] = {}
    for event in sink.sent:
        if isinstance(event, CandybarInvokeCommand):
            fire = datetime.fromtimestamp(int(event.data.tick_id.split(":")[1]) * 60, UTC)
            out.setdefault(event.data.name, []).append(fire)
    return out


def test_dispatcher_fires_each_local_time_job_once_through_the_fall_back_night() -> None:
    entries = [
        ScheduleEntry(uuid4(), "ifvault", "40 1 * * *", None, "America/New_York"),
        ScheduleEntry(uuid4(), "half", "*/30 * * * *", None, "America/New_York"),
        ScheduleEntry(uuid4(), "utc-job", "40 1 * * *", None, "UTC"),
    ]
    got = dispatch_every_minute(entries, utc(2026, 11, 1, 3), utc(2026, 11, 1, 9))
    assert got["ifvault"] == [utc(2026, 11, 1, 5, 40)]  # once, not 06:40Z
    assert got["half"] == [utc(2026, 11, 1, 3) + timedelta(minutes=30 * i) for i in range(12)]
    assert "utc-job" not in got  # 01:40Z is before the scanned hours


def test_dispatcher_fires_the_gap_job_once_after_the_gap() -> None:
    entries = [ScheduleEntry(uuid4(), "ledger", "30 2 * * *", None, "America/New_York")]
    got = dispatch_every_minute(entries, utc(2027, 3, 14, 5), utc(2027, 3, 14, 12))
    assert got["ledger"] == [utc(2027, 3, 14, 7)]


def test_dispatcher_honors_a_local_rest_of_day_skip_and_widens_the_skip_read() -> None:
    entry = ScheduleEntry(uuid4(), "hourly", "0 * * * *", None, "America/New_York")
    skip = SkipEntry(uuid4(), entry.id, SKIP_REST_OF_DAY, None, date(2026, 11, 1), None, utc(2026, 10, 30))
    windows = []

    async def skips(start, end):
        windows.append((start, end))
        return [skip]

    # Nov 1 local runs 04:00Z (00:00 EDT) .. 05:00Z next day (00:00 EST).
    got = dispatch_every_minute([entry], utc(2026, 11, 1, 2), utc(2026, 11, 2, 7), skips=skips)
    assert got["hourly"] == [
        *(utc(2026, 11, 1, 2) + timedelta(hours=i) for i in range(2)),  # 22:00, 23:00 Oct 31
        utc(2026, 11, 2, 5),  # 00:00 EST Nov 2
        utc(2026, 11, 2, 6),
    ]
    assert all(end - start >= timedelta(days=2) for start, end in windows)


def test_dispatcher_leaves_out_a_candybar_whose_stored_zone_is_unknown() -> None:
    entries = [
        ScheduleEntry(uuid4(), "broken", "* * * * *", None, "Gone/Zone"),
        ScheduleEntry(uuid4(), "fine", "* * * * *", None, "UTC"),
    ]
    got = dispatch_every_minute(entries, utc(2026, 11, 1), utc(2026, 11, 1, 0, 3))
    assert "broken" not in got and len(got["fine"]) == 3
