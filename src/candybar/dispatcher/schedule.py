"""Pure UTC schedule evaluation shared by the dispatcher and future read API."""

from __future__ import annotations

from datetime import UTC, datetime

from croniter import croniter


def matches_tick(schedule: str, minute: datetime) -> bool:
    """Match a standard five-field cron expression against a UTC tick minute."""
    if minute.tzinfo is None or minute.utcoffset() != UTC.utcoffset(None):
        raise ValueError("tick minute must be UTC")
    if minute.second or minute.microsecond:
        raise ValueError("tick minute must be on a minute boundary")
    if len(schedule.split()) != 5:
        raise ValueError("schedule must have five fields")
    return croniter.match(schedule, minute.astimezone(UTC))
