"""Unit tests for POST /candybars via FastAPI TestClient with fake store/sink.

Every matrix row that needs neither a live Postgres nor a live bus is pinned
here; the live end-to-end round-trip is tests/test_registry_integration.py.
Store functions are monkeypatched at the app module — the endpoint's flow
(validate → dup-check → publish → insert) is what is under test.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import nats.errors
import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.events.parse import parse_envelope
from candybar.registry import DuplicateNameError


@dataclass
class FakeRow:
    id: UUID
    name: str
    description: str
    tags: list[str]
    schedule: str
    entrypoint: str
    noise: bool
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    total_invoked: int = 0
    total_successful: int = 0
    timezone: str = "UTC"
    runner: dict | None = None


class FakeSink:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[bytes] = []
        self._fail = fail

    async def publish(self, event: Any) -> None:
        if self._fail:
            raise nats.errors.Error("simulated bus failure")
        self.published.append(event.model_dump_json().encode())


VALID_BODY = {
    "name": "demo",
    "description": "a demo",
    "tags": ["x"],
    "schedule": "*/5 * * * *",
    "entrypoint": "/bin/true",
}


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Wire create_app with in-memory store fakes; returns (names, rows, sink_holder)."""

    class Rig:
        def __init__(self) -> None:
            self.names: set[str] = set()
            self.rows: list[FakeRow] = []
            self.sink: FakeSink = FakeSink()
            self.fail_insert: bool = False

        def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
            @asynccontextmanager
            async def session_factory() -> AsyncIterator[Any]:
                yield object()

            return TestClient(
                create_app(session_factory=session_factory, sink=self.sink),
                raise_server_exceptions=raise_server_exceptions,
            )

    rig = Rig()

    async def fake_name_taken(session: Any, name: str) -> bool:
        return name in rig.names

    async def fake_insert(session: Any, manifest: Any, *, candybar_id: Any = None) -> FakeRow:
        if rig.fail_insert:
            raise DuplicateNameError(manifest.name)
        row = FakeRow(
            id=candybar_id,
            name=manifest.name,
            description=manifest.description,
            tags=list(manifest.tags),
            schedule=manifest.schedule,
            entrypoint=manifest.entrypoint,
            noise=manifest.noise,
        )
        rig.names.add(row.name)
        rig.rows.append(row)
        return row

    monkeypatch.setattr(app_module, "name_taken", fake_name_taken)
    monkeypatch.setattr(app_module, "insert_candybar", fake_insert)
    return rig


def test_register_happy_path_201_row_and_event(rig: Any) -> None:
    resp = rig.client().post("/candybars", json=VALID_BODY)
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "demo"
    UUID(body["id"])  # parses as a uuid
    assert body["noise"] is False
    assert "createdAt" in body
    # Story 5.1: the 201 body is the contract's single-definition shape —
    # counters start at zero and the executor-internal entrypoint is not echoed.
    assert body["totalInvoked"] == 0 and body["totalSuccessful"] == 0
    assert "entrypoint" not in body

    # Row was inserted with the same id the event carries.
    assert len(rig.rows) == 1
    assert str(rig.rows[0].id) == body["id"]

    # Exactly one well-formed candybar.created envelope went on the bus.
    assert len(rig.sink.published) == 1
    event = parse_envelope(json.loads(rig.sink.published[0]))
    assert event.type == "bloodbank.cron.candybar.created"
    assert event.ordering_key == f"candybar:{body['id']}"
    assert str(event.data.candybar_id) == body["id"]
    assert event.data.schedule == "*/5 * * * *"


def test_register_invalid_cron_422_no_row_no_event(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "schedule": "not a cron at all"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.rows == [] and rig.sink.published == []


def test_register_six_field_cron_422(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "schedule": "0 * * * * *"})
    assert resp.status_code == 422
    assert "5-field" in resp.json()["detail"]
    assert rig.rows == [] and rig.sink.published == []


def test_register_invalid_name_422_no_row_no_event(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "name": "Bad_Name"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.rows == [] and rig.sink.published == []


def test_register_duplicate_name_409_no_second_event(rig: Any) -> None:
    rig.names.add("demo")
    resp = rig.client().post("/candybars", json=VALID_BODY)
    assert resp.status_code == 409
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.rows == [] and rig.sink.published == []


def test_register_unique_race_maps_to_409(rig: Any) -> None:
    # name_taken passes but the insert hits the unique constraint.
    rig.fail_insert = True
    resp = rig.client().post("/candybars", json=VALID_BODY)
    assert resp.status_code == 409
    assert rig.rows == []  # rolled back
    assert len(rig.sink.published) == 1  # event-first ordering means it may already be out


def test_register_bus_down_503_no_row(rig: Any) -> None:
    rig.sink = FakeSink(fail=True)
    resp = rig.client().post("/candybars", json=VALID_BODY)
    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.rows == []


def test_register_missing_fields_422_problem_json(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={"name": "demo"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"


def test_register_unknown_field_rejected(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "schedul": "* * * * *"})
    assert resp.status_code == 422
    assert rig.rows == []


def test_register_nonexistent_entrypoint_422_no_row_no_event(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "entrypoint": "/nonexistent/x.sh"})
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert "entrypoint" in resp.json()["detail"]
    assert rig.rows == [] and rig.sink.published == []


def test_register_relative_entrypoint_422_even_when_it_exists_from_cwd(
    rig: Any, tmp_path: Any, monkeypatch: Any
) -> None:
    """A relative entrypoint has no manifest dir to resolve against on the API.

    It must be refused even when it happens to exist relative to the api
    process's CWD: the executor would exec it from the entrypoint's own
    parent dir (or via PATH), i.e. a different file or none at all.
    """
    script = tmp_path / "jobs" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.chdir(tmp_path)
    for relative in ("jobs/run.sh", "./jobs/run.sh"):
        resp = rig.client().post("/candybars", json={**VALID_BODY, "entrypoint": relative})
        assert resp.status_code == 422, relative
        assert resp.headers["content-type"] == "application/problem+json"
        assert "absolute" in resp.json()["detail"]
    assert rig.rows == [] and rig.sink.published == []


def test_register_empty_entrypoint_422(rig: Any) -> None:
    resp = rig.client().post("/candybars", json={**VALID_BODY, "entrypoint": ""})
    assert resp.status_code == 422
    assert rig.rows == [] and rig.sink.published == []


def test_register_unexpected_error_is_problem_json_500(rig: Any, monkeypatch: Any) -> None:
    """DB down (or any unexpected failure) still answers RFC 7807, never starlette's plain 500.

    raise_server_exceptions=False: starlette's ServerErrorMiddleware re-raises
    after responding so servers log the error; the TestClient flag mirrors that.
    """

    async def boom(session: Any, name: str) -> bool:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app_module, "name_taken", boom)
    resp = rig.client(raise_server_exceptions=False).post("/candybars", json=VALID_BODY)
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.rows == [] and rig.sink.published == []
