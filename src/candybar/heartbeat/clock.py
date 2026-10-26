"""Minute-boundary clock math for the heartbeat (AD-4) — pure functions, no I/O.

All math is epoch-based in UTC: a tick boundary is a whole multiple of 60
seconds since the epoch, which is exactly what `tick:<unix_epoch_minute>`
identity keys on.
"""

from __future__ import annotations

from datetime import UTC, datetime

SECONDS_PER_MINUTE = 60


def next_minute_boundary(now: datetime) -> datetime:
    """Return the first UTC minute boundary strictly after `now`.

    Epoch-based, so DST and leap-zone quirks cannot apply (UTC has none).
    If `now` sits exactly on a boundary, the FOLLOWING one is returned: the
    current minute's tick is fired by the pass that captured it, never
    re-fired by a boundary-exact wake.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    epoch_minute = int(now.timestamp()) // SECONDS_PER_MINUTE
    return datetime.fromtimestamp((epoch_minute + 1) * SECONDS_PER_MINUTE, tz=UTC)


def seconds_until(target: datetime, now: datetime) -> float:
    """Fractional seconds from `now` to `target`, clamped at zero (skew-safe)."""
    return max(0.0, (target - now).total_seconds())
