"""Schedule presets, a plain-English description and the next fire times.

Pure helpers for the create wizard's schedule step. The fire times come from
`dispatcher.firetimes.fire_times`, the one source of fire instants, so the
preview can never disagree with what the dispatcher will do.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta

from candybar.dispatcher.firetimes import END_OF_TIME, fire_times, never_fires
from candybar.timezones import resolve_timezone

DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

# (value, label): the wizard's preset selector.
PRESETS = (
    ("every-5", "Every 5 minutes"),
    ("every-15", "Every 15 minutes"),
    ("hourly", "Hourly"),
    ("daily", "Daily at HH:MM"),
    ("weekdays", "Weekdays at HH:MM"),
    ("weekly", "Weekly on DAY at HH:MM"),
    ("monthly", "Monthly on day N at HH:MM"),
    ("custom", "Custom cron"),
)
PRESET_NEEDS = {
    "every-5": (),
    "every-15": (),
    "hourly": (),
    "daily": ("time",),
    "weekdays": ("time",),
    "weekly": ("time", "dow"),
    "monthly": ("time", "dom"),
    "custom": (),
}

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def parse_time(text: str) -> tuple[int, int]:
    """`HH:MM` -> (hour, minute). Raises ValueError."""
    match = _TIME_RE.match(text.strip())
    if not match:
        raise ValueError(f"time must look like HH:MM, got {text!r}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"time out of range: {text!r}")
    return hour, minute


def build_cron(preset: str, *, time: str = "09:00", dow: str = "1", dom: str = "1") -> str:
    """The cron expression a preset and its inputs stand for. Raises ValueError."""
    if preset == "every-5":
        return "*/5 * * * *"
    if preset == "every-15":
        return "*/15 * * * *"
    if preset == "hourly":
        return "0 * * * *"
    hour, minute = parse_time(time)
    if preset == "daily":
        return f"{minute} {hour} * * *"
    if preset == "weekdays":
        return f"{minute} {hour} * * 1-5"
    if preset == "weekly":
        if str(dow) not in {str(i) for i in range(7)}:
            raise ValueError(f"day of week must be 0-6, got {dow!r}")
        return f"{minute} {hour} * * {dow}"
    if preset == "monthly":
        try:
            day = int(dom)
        except ValueError as exc:
            raise ValueError(f"day of month must be a number, got {dom!r}") from exc
        if not 1 <= day <= 31:
            raise ValueError(f"day of month must be 1-31, got {dom!r}")
        return f"{minute} {hour} {day} * *"
    raise ValueError(f"no builder for preset {preset!r}")


def _num(field: str, low: int, high: int) -> int | None:
    return int(field) if field.isdigit() and low <= int(field) <= high else None


def _ordinal(n: int) -> str:
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_schedule(cron: str) -> str:
    """Plain English for the common cron shapes; the raw cron for anything else."""
    fields = cron.split()
    if len(fields) != 5:
        return cron
    minute, hour, dom, month, dow = fields
    if month != "*":
        return cron
    if (minute, hour, dom, dow) == ("*", "*", "*", "*"):
        return "Every minute"
    step = re.fullmatch(r"\*/(\d+)", minute)
    if step and (hour, dom, dow) == ("*", "*", "*"):
        n = int(step.group(1))
        if n == 1:
            return "Every minute"
        # `*/7` restarts at :00 every hour (…:49, :56, :00): only a divisor of 60 is "every N".
        return f"Every {n} minutes" if 1 < n < 60 and 60 % n == 0 else cron
    m = _num(minute, 0, 59)
    if m is None:
        return cron
    if (hour, dom, dow) == ("*", "*", "*"):
        return "Every hour, on the hour" if m == 0 else f"Every hour at {m} minutes past"
    hstep = re.fullmatch(r"\*/(\d+)", hour)
    if hstep and (dom, dow) == ("*", "*") and 1 < int(hstep.group(1)) < 24 and 24 % int(hstep.group(1)) == 0:
        return f"Every {int(hstep.group(1))} hours at {m:02d} minutes past"
    h = _num(hour, 0, 23)
    if h is None:
        return cron
    at = f"{h:02d}:{m:02d}"
    if dom == "*" and dow == "*":
        return f"Every day at {at}"
    if dom == "*" and dow in ("1-5", "MON-FRI"):
        return f"Weekdays at {at}"
    if dom == "*" and dow in ("0,6", "6,0", "6-7", "6,7", "7,6"):
        return f"Weekends at {at}"
    if dom == "*":
        d = _num(dow, 0, 7)
        if d is not None:
            return f"Every {DAYS[d % 7]} at {at}"
        return cron
    day = _num(dom, 1, 31)
    if day is not None and dow == "*":
        if day > 28:
            return f"On the {_ordinal(day)} of each month that has one, at {at}"
        return f"On the {_ordinal(day)} of every month at {at}"
    return cron


def next_fires(
    cron: str, tz_name: str, *, count: int = 5, now: datetime | None = None
) -> list[datetime]:
    """The next `count` fire instants (UTC) after `now`, within nine years.

    Raises ValueError on a malformed cron or TimezoneError on a bad zone.
    """
    tz = resolve_timezone(tz_name)
    now = now or datetime.now(UTC)
    start = now.astimezone(UTC).replace(second=0, microsecond=0) + timedelta(minutes=1)
    if never_fires(cron):
        return []
    end = min(start + timedelta(days=366 * 9), END_OF_TIME)
    return list(itertools.islice(fire_times(cron, tz, start, end), count))
