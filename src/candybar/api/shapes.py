"""Contract JSON shapes shared by the REST routes and the live stream.

`candybar_row_json` is holocene-view-contract.md's definition shape (Story
5.1). It lives here, not in `app.py`, so the stream hub (Story 5.4) can
build `definition.changed` payloads in the same shape without importing the
app.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def utc_iso(value: datetime | None) -> str | None:
    """ISO 8601 in UTC (AD-13). timestamptz comes back in the DB session's
    zone (the live server's is America/New_York), so convert explicitly."""
    return value.astimezone(UTC).isoformat() if value is not None else None


def candybar_row_json(row: Any) -> dict[str, Any]:
    """The holocene-view-contract definition shape, exactly (GET /candybars items)."""
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "tags": list(row.tags),
        "schedule": row.schedule,
        "timezone": row.timezone,
        "noise": row.noise,
        "createdAt": utc_iso(row.created_at),
    }
