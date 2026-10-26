"""Contract field lists parsed from holocene-view-contract.md (Story 5.1).

Shared by the unit contract test (tests/test_api_definitions.py) and the live
round trip (tests/test_registry_integration.py). The key SETS are read from
the contract file, so a contract edit the API does not follow fails both.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

CONTRACT = (
    Path(__file__).resolve().parent.parent
    / "_bmad-output"
    / "specs"
    / "spec-cron-framework"
    / "holocene-view-contract.md"
)




def _brace_fields(route: str) -> list[tuple[str, str | None]]:
    """The `{ ... }` field list on the contract bullet for `route`, as (name, kind)."""
    text = CONTRACT.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.lstrip("- ").startswith(f"`{route}` →"))
    inner = re.search(r"`\{([^}]*)\}`", line)
    assert inner, f"no {{...}} field list on the contract line for {route}"
    out: list[tuple[str, str | None]] = []
    for part in inner.group(1).split(","):
        m = re.match(r"\s*([A-Za-z]+)(\[\])?\s*(?:\([^)]*\))?\s*(?::\s*(\w+))?\s*$", part)
        assert m, f"unparseable contract field {part!r} for {route}"
        name, is_list, annotation = m.groups()
        out.append((name, "list" if is_list else annotation))
    return out


LIST_FIELDS = _brace_fields("GET /candybars")
EXTRA_SINGLE_FIELDS = _brace_fields("GET /candybars/{id}")
LIST_KEYS = {name for name, _ in LIST_FIELDS}
SINGLE_KEYS = LIST_KEYS | {name for name, _ in EXTRA_SINGLE_FIELDS}


def _utc_iso(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    parsed = datetime.fromisoformat(value)
    return parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0)


def _uuid(value: Any) -> bool:
    return isinstance(value, str) and str(UUID(value)) == value


# Value checks per contract field. The key SET is the contract's; this map
# must cover exactly it, so a field added to the contract fails loudly here.
FIELD_CHECKS: dict[str, Any] = {
    "id": _uuid,
    "name": lambda v: isinstance(v, str) and v != "",
    "description": lambda v: isinstance(v, str),
    "tags": lambda v: isinstance(v, list) and all(isinstance(t, str) for t in v),
    "schedule": lambda v: isinstance(v, str) and len(v.split()) == 5,
    "noise": lambda v: isinstance(v, bool),
    "createdAt": _utc_iso,
    "totalInvoked": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
    "totalSuccessful": lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0,
}


def assert_contract_shape(body: dict[str, Any], keys: set[str]) -> None:
    assert set(body) == keys, f"keys differ from the contract: {set(body) ^ keys}"
    for key in keys:
        assert FIELD_CHECKS[key](body[key]), f"{key}={body[key]!r} violates the contract"


# ---------------------------------------------------------------------------
# Schedule window rows (Story 5.2). The row bullet carries optional fields
# (`lastResult?`) and quoted unions (`state: "past" | ...`); `noise` is added
# to the row by the noise-collection section ("rows include `noise: bool`").

SCHEDULE_ROUTE = "GET /schedule?from=<ts>&to=<ts>"


def _row_fields(route: str) -> dict[str, tuple[bool, Any]]:
    """name -> (optional, kind): kind is a type word or the frozenset of a quoted union."""
    text = CONTRACT.read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.lstrip("- ").startswith(f"`{route}` →"))
    inner = re.search(r"`\{([^}]*)\}`", line)
    assert inner, f"no {{...}} field list on the contract line for {route}"
    out: dict[str, tuple[bool, Any]] = {}
    for part in inner.group(1).split(","):
        m = re.match(r"\s*([A-Za-z]+)(\?)?\s*(?::\s*(.+?))?\s*$", part)
        assert m, f"unparseable contract field {part!r} for {route}"
        name, optional, annotation = m.groups()
        kind: Any = annotation
        if annotation and '"' in annotation:
            kind = frozenset(re.findall(r'"([^"]+)"', annotation))
        out[name] = (optional is not None, kind)
    # The noise section extends the row: "`GET /schedule` rows include `noise: bool`".
    noise_line = next(ln for ln in text.splitlines() if "`GET /schedule` rows include" in ln)
    extra = re.search(r"rows include `(\w+): (\w+)`", noise_line)
    assert extra, "no `name: type` on the contract's schedule-rows noise line"
    out[extra.group(1)] = (False, extra.group(2))
    return out


SCHEDULE_ROW_FIELDS = _row_fields(SCHEDULE_ROUTE)
SCHEDULE_REQUIRED_KEYS = {n for n, (opt, _) in SCHEDULE_ROW_FIELDS.items() if not opt}
SCHEDULE_OPTIONAL_KEYS = {n for n, (opt, _) in SCHEDULE_ROW_FIELDS.items() if opt}
STATE_VALUES: frozenset[str] = SCHEDULE_ROW_FIELDS["state"][1]
RESULT_VALUES: frozenset[str] = SCHEDULE_ROW_FIELDS["lastResult"][1]


def _utc_minute(value: Any) -> bool:
    if not _utc_iso(value):
        return False
    parsed = datetime.fromisoformat(value)
    return parsed.second == 0 and parsed.microsecond == 0


SCHEDULE_FIELD_CHECKS: dict[str, Any] = {
    "candybarId": _uuid,
    "name": lambda v: isinstance(v, str) and v != "",
    "schedule": lambda v: isinstance(v, str) and len(v.split()) == 5,
    "fireTime": _utc_minute,
    "state": lambda v: v in STATE_VALUES,
    "lastResult": lambda v: v in RESULT_VALUES,
    "skipped": lambda v: isinstance(v, bool),
    "noise": lambda v: isinstance(v, bool),
}


def assert_schedule_row(body: dict[str, Any]) -> None:
    keys = set(body)
    missing = SCHEDULE_REQUIRED_KEYS - keys
    extra = keys - SCHEDULE_REQUIRED_KEYS - SCHEDULE_OPTIONAL_KEYS
    assert not missing and not extra, f"row keys differ from the contract: {missing=} {extra=}"
    for key in keys:
        assert SCHEDULE_FIELD_CHECKS[key](body[key]), f"{key}={body[key]!r} violates the contract"


NEXT_ROUTE = "GET /schedule/next"


def contract_route_line(route: str) -> str:
    """The contract bullet that defines `route` (Story 5.3: `/schedule/next`)."""
    text = CONTRACT.read_text(encoding="utf-8")
    return next(ln for ln in text.splitlines() if ln.lstrip("- ").startswith(f"`{route}` →"))
