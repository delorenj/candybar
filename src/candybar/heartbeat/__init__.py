"""Heartbeat role: minute-boundary tick publisher — the only timer in the system (AD-1, AD-4).

`clock` holds the pure boundary math, `publisher` the tick loop (injectable
clock/sleep for tests), and `role` the production NATS wiring. Nothing outside
this package may sleep-until-a-time to trigger work.
"""

from .clock import next_minute_boundary, seconds_until
from .publisher import PUBLISH_MAX_ATTEMPTS, Heartbeat, NatsTickSink, TickSink
from .role import run_heartbeat

__all__ = [
    "PUBLISH_MAX_ATTEMPTS",
    "Heartbeat",
    "NatsTickSink",
    "TickSink",
    "next_minute_boundary",
    "run_heartbeat",
    "seconds_until",
]
