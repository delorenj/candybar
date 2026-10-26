"""Story 5.1: definitions CRUD over REST, conformant to holocene-view-contract.md.

`GET /candybars`, `GET /candybars/{id}`, `POST /candybars` (single shape) and
`PATCH /candybars/{id}` (emits `bloodbank.cron.candybar.updated`). Store
functions are monkeypatched at the app module with an in-memory registry
(the pattern of test_api_register.py); the live-Postgres round trip through
the same routes is in tests/test_registry_integration.py.

The contract test derives the field lists from the contract FILE, so an edit
to the contract that the API does not follow fails here (Epic 6 handoff rule:
a contract change updates the contract and the API in the same commit).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import nats.errors
import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.events.parse import parse_envelope
from candybar.registry import UPDATABLE_FIELDS, DuplicateNameError
from contract_shapes import (
    EXTRA_SINGLE_FIELDS,
    FIELD_CHECKS,
    LIST_FIELDS,
    LIST_KEYS,
    SINGLE_KEYS,
    assert_contract_shape,
)

PROBLEM = "application/problem+json"


def test_contract_parse_found_the_documented_fields() -> None:
    assert LIST_KEYS == {"id", "name", "description", "tags", "schedule", "noise", "createdAt"}
    assert dict(LIST_FIELDS)["tags"] == "list" and dict(LIST_FIELDS)["noise"] == "bool"
    assert {n for n, _ in EXTRA_SINGLE_FIELDS} == {"totalInvoked", "totalSuccessful"}
    assert set(FIELD_CHECKS) == SINGLE_KEYS


# ---------------------------------------------------------------------------
# In-memory registry rig.


@dataclass
class Row:
    id: UUID
    name: str
    description: str
    tags: list[str]
    schedule: str
    entrypoint: str
    noise: bool
    created_at: datetime
    updated_at: datetime
    total_invoked: int = 0
    total_successful: int = 0


class FakeSink:
    def __init__(self) -> None:
        self.published: list[Any] = []
        self.fail = False

    async def publish(self, event: Any) -> None:
        if self.fail:
            raise nats.errors.Error("simulated bus failure")
        self.published.append(parse_envelope(json.loads(event.model_dump_json())))


T0 = datetime(2026, 10, 26, 6, 0, tzinfo=UTC)


@dataclass
class Rig:
    rows: dict[UUID, Row] = field(default_factory=dict)
    sink: FakeSink = field(default_factory=FakeSink)
    rename_race: bool = False
    writes: int = 0

    def add(self, name: str, **kw: Any) -> Row:
        row = Row(
            id=kw.pop("id", uuid4()),
            name=name,
            description=kw.pop("description", ""),
            tags=kw.pop("tags", []),
            schedule=kw.pop("schedule", "*/5 * * * *"),
            entrypoint=kw.pop("entrypoint", "/bin/true"),
            noise=kw.pop("noise", False),
            created_at=kw.pop("created_at", T0 + timedelta(minutes=len(self.rows))),
            updated_at=T0,
            **kw,
        )
        self.rows[row.id] = row
        return row

    def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
        @asynccontextmanager
        async def session_factory() -> AsyncIterator[Any]:
            yield object()

        return TestClient(
            create_app(session_factory=session_factory, sink=self.sink),
            raise_server_exceptions=raise_server_exceptions,
        )


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    rig = Rig()

    async def list_candybars(session: Any) -> list[Row]:
        return sorted(rig.rows.values(), key=lambda r: (r.created_at, r.id))

    async def get_candybar(session: Any, candybar_id: UUID) -> Row | None:
        return rig.rows.get(candybar_id)

    async def name_taken(session: Any, name: str) -> bool:
        return any(r.name == name for r in rig.rows.values())

    async def update_candybar(session: Any, row: Row, changes: dict[str, Any]) -> Row:
        assert set(changes) <= set(UPDATABLE_FIELDS)
        if rig.rename_race and "name" in changes:
            raise DuplicateNameError(changes["name"])
        for key, value in changes.items():
            setattr(row, key, value)
        rig.writes += 1
        return row

    async def insert_candybar(session: Any, manifest: Any, *, candybar_id: Any = None) -> Row:
        return rig.add(
            manifest.name, id=candybar_id, description=manifest.description,
            tags=list(manifest.tags), schedule=manifest.schedule,
            entrypoint=manifest.entrypoint, noise=manifest.noise,
            created_at=datetime.now(UTC),
        )

    monkeypatch.setattr(app_module, "list_candybars", list_candybars)
    monkeypatch.setattr(app_module, "get_candybar", get_candybar)
    monkeypatch.setattr(app_module, "lock_candybar", get_candybar)
    monkeypatch.setattr(app_module, "name_taken", name_taken)
    monkeypatch.setattr(app_module, "update_candybar", update_candybar)
    monkeypatch.setattr(app_module, "insert_candybar", insert_candybar)
    return rig


# ---------------------------------------------------------------------------
# Reads.


def test_list_returns_contract_shape_in_created_order(rig: Rig) -> None:
    a = rig.add("alpha", tags=["x", "y"], noise=True)
    b = rig.add("beta", description="second")
    resp = rig.client().get("/candybars")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)  # the contract's "list", not a wrapper object
    assert [item["id"] for item in body] == [str(a.id), str(b.id)]
    for item in body:
        assert_contract_shape(item, LIST_KEYS)
    assert body[0]["tags"] == ["x", "y"] and body[0]["noise"] is True
    assert body[0]["createdAt"] == T0.isoformat()


def test_list_empty_registry_is_empty_array(rig: Rig) -> None:
    resp = rig.client().get("/candybars")
    assert resp.status_code == 200 and resp.json() == []


def test_get_one_adds_registry_counters(rig: Rig) -> None:
    row = rig.add("counted", total_invoked=5, total_successful=4)
    resp = rig.client().get(f"/candybars/{row.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert_contract_shape(body, SINGLE_KEYS)
    assert body["totalInvoked"] == 5 and body["totalSuccessful"] == 4


def test_created_at_is_utc_whatever_the_session_zone(rig: Rig) -> None:
    eastern = timezone(timedelta(hours=-4))
    row = rig.add("zoned", created_at=datetime(2026, 10, 26, 2, 0, tzinfo=eastern))
    body = rig.client().get(f"/candybars/{row.id}").json()
    assert body["createdAt"] == "2026-10-26T06:00:00+00:00"


def test_get_unknown_is_404_problem(rig: Rig) -> None:
    resp = rig.client().get(f"/candybars/{uuid4()}")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == PROBLEM


def test_get_malformed_id_is_422_problem(rig: Rig) -> None:
    resp = rig.client().get("/candybars/nope")
    assert resp.status_code == 422
    assert resp.headers["content-type"] == PROBLEM


def test_list_database_failure_is_500_problem(rig: Rig, monkeypatch: Any) -> None:
    async def boom(session: Any) -> list[Row]:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app_module, "list_candybars", boom)
    resp = rig.client(raise_server_exceptions=False).get("/candybars")
    assert resp.status_code == 500 and resp.headers["content-type"] == PROBLEM


# ---------------------------------------------------------------------------
# Create (the 1.4 path, now answering the contract's single shape).


CREATE_BODY = {"name": "fresh", "description": "d", "tags": ["t"], "schedule": "0 * * * *",
               "entrypoint": "/bin/true", "noise": True}


def test_create_answers_single_contract_shape(rig: Rig) -> None:
    resp = rig.client().post("/candybars", json=CREATE_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert_contract_shape(body, SINGLE_KEYS)
    assert body["totalInvoked"] == 0 and body["totalSuccessful"] == 0
    assert body["noise"] is True
    # The created definition reads back identically.
    assert rig.client().get(f"/candybars/{body['id']}").json() == body
    assert [e.type for e in rig.sink.published] == ["bloodbank.cron.candybar.created"]


@pytest.mark.parametrize("tags", [[""], ["ok", "  "]])
def test_create_blank_tag_is_422_not_500(rig: Rig, tags: list[str]) -> None:
    resp = rig.client().post("/candybars", json={**CREATE_BODY, "tags": tags})
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert rig.rows == {} and rig.sink.published == []


@pytest.mark.parametrize("key", ["id", "createdAt", "totalInvoked"])
def test_create_refuses_read_only_fields(rig: Rig, key: str) -> None:
    resp = rig.client().post("/candybars", json={**CREATE_BODY, key: "x"})
    assert resp.status_code == 422 and rig.rows == {}


# ---------------------------------------------------------------------------
# Update (PATCH) — emits bloodbank.cron.candybar.updated.


def _only_event(rig: Rig) -> Any:
    assert len(rig.sink.published) == 1
    return rig.sink.published[0]


def test_patch_schedule_emits_updated_with_changed_fields_only(rig: Rig) -> None:
    row = rig.add("job", schedule="*/5 * * * *", total_invoked=3, total_successful=3)
    resp = rig.client().patch(
        f"/candybars/{row.id}", json={"schedule": "0 * * * *", "description": ""}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert_contract_shape(body, SINGLE_KEYS)
    assert body["schedule"] == "0 * * * *" and body["totalInvoked"] == 3
    event = _only_event(rig)
    assert event.type == "bloodbank.cron.candybar.updated"
    assert event.subject == "bloodbank.evt.cron.candybar.updated"
    assert event.ordering_key == f"candybar:{row.id}"
    assert event.data.candybar_id == row.id and event.data.name == "job"
    # description was sent but equal to the stored value: not a change.
    assert event.data.changes == {"schedule": "0 * * * *"}
    assert rig.rows[row.id].schedule == "0 * * * *" and rig.writes == 1


def test_patch_every_writable_field(rig: Rig, tmp_path: Path) -> None:
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/sh\n")
    row = rig.add("old-name")
    patch = {"name": "new-name", "description": "now described", "tags": ["a", "b"],
             "schedule": "15 3 * * *", "entrypoint": str(script), "noise": True}
    resp = rig.client().patch(f"/candybars/{row.id}", json=patch)
    assert resp.status_code == 200
    event = _only_event(rig)
    assert event.data.changes == patch
    assert event.data.name == "new-name"  # the post-update name
    body = resp.json()
    assert body["name"] == "new-name" and body["tags"] == ["a", "b"] and body["noise"] is True
    assert rig.rows[row.id].entrypoint == str(script)


def test_patch_with_stored_values_is_a_no_op(rig: Rig) -> None:
    row = rig.add("same", tags=["x"], noise=False)
    resp = rig.client().patch(f"/candybars/{row.id}", json={"tags": ["x"], "noise": False})
    assert resp.status_code == 200
    assert_contract_shape(resp.json(), SINGLE_KEYS)
    assert rig.sink.published == [] and rig.writes == 0


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"description": None},
        {"noise": None},
        {"id": str(uuid4())},
        {"createdAt": "2026-10-26T00:00:00Z"},
        {"totalInvoked": 9},
        {"schedul": "* * * * *"},
    ],
)
def test_patch_bad_body_is_422_no_event(rig: Rig, patch: dict[str, Any]) -> None:
    row = rig.add("guarded")
    resp = rig.client().patch(f"/candybars/{row.id}", json=patch)
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert rig.sink.published == [] and rig.writes == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"schedule": "not a cron"},
        {"schedule": "0 * * * * *"},
        {"name": "Bad_Name"},
        {"tags": [""]},
        {"entrypoint": ""},
        {"entrypoint": "/nonexistent/run.sh"},
        # Relative: refused even though it exists from pytest's CWD (repo root).
        {"entrypoint": "pyproject.toml"},
    ],
)
def test_patch_invalid_merged_definition_is_422_no_event(
    rig: Rig, patch: dict[str, Any]
) -> None:
    row = rig.add("strict")
    resp = rig.client().patch(f"/candybars/{row.id}", json=patch)
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert rig.sink.published == [] and rig.writes == 0
    assert rig.rows[row.id].schedule == "*/5 * * * *" and rig.rows[row.id].name == "strict"


def test_patch_rename_to_taken_name_is_409_no_event(rig: Rig) -> None:
    rig.add("taken")
    row = rig.add("mine")
    resp = rig.client().patch(f"/candybars/{row.id}", json={"name": "taken"})
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM
    assert rig.sink.published == [] and rig.rows[row.id].name == "mine"


def test_patch_rename_race_maps_to_409(rig: Rig) -> None:
    row = rig.add("mine")
    rig.rename_race = True  # name_taken passes, the flush hits the unique constraint
    resp = rig.client().patch(f"/candybars/{row.id}", json={"name": "contested"})
    assert resp.status_code == 409 and resp.headers["content-type"] == PROBLEM
    assert rig.rows[row.id].name == "mine"
    assert len(rig.sink.published) == 1  # event-first: the fact may already be out


def test_patch_unknown_id_is_404_no_event(rig: Rig) -> None:
    resp = rig.client().patch(f"/candybars/{uuid4()}", json={"noise": True})
    assert resp.status_code == 404 and resp.headers["content-type"] == PROBLEM
    assert rig.sink.published == []


def test_patch_malformed_id_is_422(rig: Rig) -> None:
    resp = rig.client().patch("/candybars/nope", json={"noise": True})
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM


def test_patch_bus_down_is_503_row_unchanged(rig: Rig) -> None:
    row = rig.add("offline")
    rig.sink.fail = True
    resp = rig.client().patch(f"/candybars/{row.id}", json={"schedule": "0 0 * * *"})
    assert resp.status_code == 503 and resp.headers["content-type"] == PROBLEM
    assert rig.rows[row.id].schedule == "*/5 * * * *" and rig.writes == 0


def test_patch_publishes_before_the_row_write(rig: Rig, monkeypatch: Any) -> None:
    """Spine state rule: the fact goes out first, then the row in the same flow."""
    order: list[str] = []
    row = rig.add("ordered")
    real_publish = rig.sink.publish
    real_update = app_module.update_candybar

    async def publish(event: Any) -> None:
        order.append("publish")
        await real_publish(event)

    async def update(session: Any, r: Any, changes: dict[str, Any]) -> Any:
        order.append("write")
        return await real_update(session, r, changes)

    monkeypatch.setattr(rig.sink, "publish", publish)
    monkeypatch.setattr(app_module, "update_candybar", update)
    assert rig.client().patch(f"/candybars/{row.id}", json={"noise": True}).status_code == 200
    assert order == ["publish", "write"]


def test_method_not_allowed_on_definitions_is_problem_json(rig: Rig) -> None:
    resp = rig.client().delete(f"/candybars/{uuid4()}")
    assert resp.status_code == 405 and resp.headers["content-type"] == PROBLEM
