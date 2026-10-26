"""UUIDv7 generation, stdlib-only.

Python 3.13 (the CI floor) has no `uuid.uuid7`, so the registry mints its own
time-ordered ids: 48-bit unix epoch milliseconds, version 7, IETF variant,
random remainder (RFC 9562 §5.7). Ids sort roughly by creation time, which
keeps Postgres B-tree inserts append-mostly.
"""

from __future__ import annotations

import secrets
import time
from uuid import UUID


def uuid7(*, ms: int | None = None, rand: int | None = None) -> UUID:
    """Mint a UUIDv7. `ms`/`rand` are injectable for deterministic tests."""
    if ms is None:
        ms = time.time_ns() // 1_000_000
    if ms < 0 or ms >= 1 << 48:
        raise ValueError(f"ms must fit in 48 bits, got {ms}")
    if rand is None:
        rand = secrets.randbits(74)
    if rand < 0 or rand >= 1 << 74:
        raise ValueError(f"rand must fit in 74 bits, got {rand}")
    value = (ms & 0xFFFFFFFFFFFF) << 80
    value |= 0x7 << 76  # version 7
    value |= ((rand >> 62) & 0xFFF) << 64  # 12-bit rand_a
    value |= 0x2 << 62  # IETF variant (10xx)
    value |= rand & 0x3FFFFFFFFFFFFFFF  # 62-bit rand_b
    return UUID(int=value)
