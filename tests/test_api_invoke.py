"""Unit tests for POST /candybars/{id}/invoke (Story 1.8, AD-12).

Store lookups are monkeypatched at the app module and the bus is a fake sink;
the live API → JetStream → executor → Postgres path is
tests/test_manual_invoke_integration.py.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import nats.errors
import pytest
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import create_app
from candybar.dispatcher.worker import command_for
from candybar.events import build_tick_fired
from candybar.events.envelope import CandybarInvokeCommand
from candybar.events.parse import parse_envelope
from candybar.registry.store import ScheduleEntry

KNOWN_ID = UUID("01900000-0000-7000-8000-0000000000aa")


class FakeSink:
    def __init__(self, *, error: BaseException | None = None) -> None:
        self.published: list[Any] = []
        self.wire: list[bytes] = []
        self._error = error

    async def publish(self, event: Any) -> None:
        if self._error is not None:
            raise self._error
        self.published.append(event)
        self.wire.append(event.model_dump_json().encode())


@pytest.fixture()
def rig(monkeypatch: pytest.MonkeyPatch) -> Any:
    class Rig:
        def __init__(self) -> None:
            self.rows: dict[UUID, Any] = {
                KNOWN_ID: SimpleNamespace(id=KNOWN_ID, name="nightly-report")
            }
            self.sink = FakeSink()
            self.lookups: list[UUID] = []

        def client(self, *, raise_server_exceptions: bool = True) -> TestClient:
            @asynccontextmanager
            async def session_factory() -> AsyncIterator[Any]:
                yield object()

            return TestClient(
                create_app(session_factory=session_factory, sink=self.sink),
                raise_server_exceptions=raise_server_exceptions,
            )

    rig = Rig()

    async def fake_get_candybar(session: Any, candybar_id: UUID) -> Any:
        rig.lookups.append(candybar_id)
        return rig.rows.get(candybar_id)

    monkeypatch.setattr(app_module, "get_candybar", fake_get_candybar)
    return rig


def _invoke(client: TestClient, candybar_id: object) -> Any:
    return client.post(f"/candybars/{candybar_id}/invoke")


def test_invoke_known_id_202_and_one_manual_command(rig: Any) -> None:
    resp = _invoke(rig.client(), KNOWN_ID)
    assert resp.status_code == 202
    body = resp.json()
    assert body["candybarId"] == str(KNOWN_ID)
    assert body["name"] == "nightly-report"
    assert body["trigger"] == "manual"

    assert len(rig.sink.wire) == 1
    cmd = parse_envelope(json.loads(rig.sink.wire[0]))
    assert isinstance(cmd, CandybarInvokeCommand)
    assert cmd.subject == "bloodbank.cmd.cron.candybar.invoke"
    assert cmd.type == "bloodbank.cron.candybar.invoke"
    assert cmd.delivery == "single_consumer"
    assert cmd.data.trigger == "manual"
    assert cmd.data.tick_id is None
    assert cmd.data.candybar_id == KNOWN_ID
    assert cmd.data.name == "nightly-report"
    # The response names exactly the command that went on the bus.
    assert body["invocationId"] == str(cmd.data.invocation_id)
    assert body["commandId"] == str(cmd.command_id)
    assert body["idempotencyKey"] == cmd.idempotency_key
    assert cmd.idempotency_key == f"candybar:{KNOWN_ID}:manual:{cmd.command_id}"
    # Root-issued; envelope id == command id so a re-sent publish dedupes.
    assert cmd.correlationid == cmd.command_id
    assert cmd.id == cmd.command_id


def test_repeat_invokes_are_distinct_invocations(rig: Any) -> None:
    client = rig.client()
    first = _invoke(client, KNOWN_ID).json()
    second = _invoke(client, KNOWN_ID).json()
    assert len(rig.sink.published) == 2
    for key in ("invocationId", "commandId", "idempotencyKey"):
        assert first[key] != second[key]
    assert rig.sink.published[0].id != rig.sink.published[1].id


def test_manual_command_rides_the_scheduled_command_shape(rig: Any) -> None:
    """AD-12: same subject/type/delivery/schema as the dispatcher's command,
    so the one executor durable consumes both identically."""
    _invoke(rig.client(), KNOWN_ID)
    manual = rig.sink.published[0]
    tick = build_tick_fired(minute=datetime(2026, 10, 26, 12, 0, tzinfo=UTC))
    scheduled = command_for(ScheduleEntry(id=KNOWN_ID, name="nightly-report", schedule="* * * * *"), tick)
    for field in ("subject", "type", "kind", "delivery", "dataschema", "schemaref"):
        assert getattr(manual, field) == getattr(scheduled, field), field
    assert scheduled.data.trigger == "scheduled" and manual.data.trigger == "manual"


def test_unknown_id_404_problem_json_nothing_published(rig: Any) -> None:
    missing = uuid4()
    resp = _invoke(rig.client(), missing)
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    problem = resp.json()
    assert problem["status"] == 404 and problem["title"] == "Candybar not found"
    assert str(missing) in problem["detail"]
    assert rig.sink.published == []
    assert rig.lookups == [missing]


def test_malformed_id_422_problem_json_no_lookup(rig: Any) -> None:
    resp = _invoke(rig.client(), "not-a-uuid")
    assert resp.status_code == 422
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.sink.published == []
    assert rig.lookups == []


@pytest.mark.parametrize(
    "error",
    [nats.errors.Error("bus gone"), OSError("connection reset"), TimeoutError()],
    ids=["nats", "os", "timeout"],
)
def test_bus_down_503_problem_json(rig: Any, error: BaseException) -> None:
    rig.sink = FakeSink(error=error)
    resp = _invoke(rig.client(), KNOWN_ID)
    assert resp.status_code == 503
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["title"] == "Event bus unavailable"


def test_db_down_500_problem_json_nothing_published(rig: Any, monkeypatch: Any) -> None:
    async def boom(session: Any, candybar_id: UUID) -> Any:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app_module, "get_candybar", boom)
    resp = _invoke(rig.client(raise_server_exceptions=False), KNOWN_ID)
    assert resp.status_code == 500
    assert resp.headers["content-type"] == "application/problem+json"
    assert rig.sink.published == []


def test_unknown_route_404_problem_json(rig: Any) -> None:
    resp = rig.client().get("/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["status"] == 404


def test_wrong_method_405_problem_json_keeps_allow(rig: Any) -> None:
    resp = rig.client().get(f"/candybars/{KNOWN_ID}/invoke")
    assert resp.status_code == 405
    assert resp.headers["content-type"] == "application/problem+json"
    assert "POST" in resp.headers.get("allow", "")
    assert rig.sink.published == []
