"""Pure schedule helpers behind the create wizard."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from candybar.tui.schedule import (
    PRESETS,
    build_cron,
    describe_schedule,
    next_fires,
    parse_time,
)


@pytest.mark.parametrize(
    ("cron", "text"),
    [
        ("* * * * *", "Every minute"),
        ("*/5 * * * *", "Every 5 minutes"),
        ("*/15 * * * *", "Every 15 minutes"),
        ("0 * * * *", "Every hour, on the hour"),
        ("30 * * * *", "Every hour at 30 minutes past"),
        ("0 */6 * * *", "Every 6 hours at 00 minutes past"),
        ("5 9 * * *", "Every day at 09:05"),
        ("0 17 * * 1-5", "Weekdays at 17:00"),
        ("0 8 * * 0,6", "Weekends at 08:00"),
        ("30 7 * * 1", "Every Monday at 07:30"),
        ("30 7 * * 0", "Every Sunday at 07:30"),
        ("0 0 1 * *", "On the 1st of every month at 00:00"),
        ("0 12 22 * *", "On the 22nd of every month at 12:00"),
        ("0 12 13 * *", "On the 13th of every month at 12:00"),
    ],
)
def test_describe_common_shapes(cron: str, text: str) -> None:
    assert describe_schedule(cron) == text


@pytest.mark.parametrize("cron", ["1-5 * * * *", "0 0 * 6 *", "0 9 1 * 1", "not cron", "0 9 * *", "*/90 * * * *"])
def test_describe_falls_back_to_the_raw_cron(cron: str) -> None:
    assert describe_schedule(cron) == cron


@pytest.mark.parametrize(
    ("preset", "kwargs", "cron"),
    [
        ("every-5", {}, "*/5 * * * *"),
        ("every-15", {}, "*/15 * * * *"),
        ("hourly", {}, "0 * * * *"),
        ("daily", {"time": "09:30"}, "30 9 * * *"),
        ("weekdays", {"time": "7:05"}, "5 7 * * 1-5"),
        ("weekly", {"time": "18:00", "dow": "5"}, "0 18 * * 5"),
        ("monthly", {"time": "03:00", "dom": "15"}, "0 3 15 * *"),
    ],
)
def test_build_cron(preset: str, kwargs: dict[str, str], cron: str) -> None:
    assert build_cron(preset, **kwargs) == cron


@pytest.mark.parametrize(
    ("preset", "kwargs"),
    [
        ("daily", {"time": "25:00"}),
        ("daily", {"time": "nine"}),
        ("weekly", {"dow": "9"}),
        ("monthly", {"dom": "32"}),
        ("monthly", {"dom": "x"}),
        ("custom", {}),
    ],
)
def test_build_cron_rejects_bad_input(preset: str, kwargs: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        build_cron(preset, **kwargs)


def test_every_preset_is_listed_once() -> None:
    values = [value for value, _ in PRESETS]
    assert len(values) == len(set(values)) == 8


def test_parse_time() -> None:
    assert parse_time(" 7:05 ") == (7, 5)


def test_next_fires_in_zone_and_utc() -> None:
    now = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
    fires = next_fires("0 9 * * *", "America/New_York", count=3, now=now)
    assert [f.isoformat() for f in fires] == [
        "2026-01-05T14:00:00+00:00",
        "2026-01-06T14:00:00+00:00",
        "2026-01-07T14:00:00+00:00",
    ]


def test_next_fires_excludes_the_current_minute() -> None:
    now = datetime(2026, 1, 5, 12, 0, 30, tzinfo=UTC)
    assert next_fires("* * * * *", "UTC", count=1, now=now)[0] == datetime(2026, 1, 5, 12, 1, tzinfo=UTC)


def test_next_fires_never_firing_cron_is_empty() -> None:
    assert next_fires("0 0 30 2 *", "UTC") == []
