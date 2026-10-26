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
