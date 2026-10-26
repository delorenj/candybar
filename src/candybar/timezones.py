"""IANA timezone names for candybars (Epic 7, AD-16).

A candybar's cron is evaluated as local wall time in its `timezone`; ticks,
events, idempotency keys and every stored/API timestamp stay UTC. This module
is the one place a timezone NAME becomes a `ZoneInfo`, so registration, the
dispatcher and the read API agree on what is valid.

Pure and import-light (stdlib only): the registry store, the manifest
validator and the fire-time helper all import it.
"""

from __future__ import annotations

import functools
from zoneinfo import ZoneInfo, available_timezones

DEFAULT_TIMEZONE = "UTC"

# Listed by the tz database but not zones: `localtime` follows whatever the
# host is set to (a candybar's schedule must not move when the host does) and
# `Factory` is the placeholder zone.
_NOT_A_ZONE = frozenset({"localtime", "Factory"})


class TimezoneError(ValueError):
    """A timezone name that is not an IANA zone known to this host."""


@functools.cache
def _known() -> frozenset[str]:
    return frozenset(available_timezones())


@functools.cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def resolve_timezone(name: str) -> ZoneInfo:
    """The `ZoneInfo` for an IANA name (`America/New_York`, `UTC`, ...).

    Only names the system tz database lists are accepted: a path-like or
    host-relative name (`localtime`, `../x`, `posixrules`) is refused, not loaded.
    Raises TimezoneError.
    """
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise TimezoneError(f"invalid timezone {name!r}: must be an IANA zone name")
    if name not in _known() or name in _NOT_A_ZONE:
        raise TimezoneError(f"unknown timezone {name!r}: must be an IANA zone name such as 'America/New_York'")
    return _zone(name)
