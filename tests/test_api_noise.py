"""Story 5.5: the noise collection endpoints, conformant to holocene-view-contract.md.

`PUT`/`DELETE /candybars/{id}/noise` add or remove one candybar; `PUT
/noise/batch` `{ids}` replaces the whole collection atomically. Each changed
candybar gets one `bloodbank.cron.candybar.updated` fact (`changes: {noise}`)
before its row is written, and `/schedule` rows then read the stored flag.
candybar stores only the explicit collection: no `<10m` auto-classification.

The in-memory registry rig is Story 5.1's (tests/test_api_definitions.py);
the live-Postgres proof through the same routes is tests/test_noise_integration.py.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import nats.errors
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import candybar.api.app as app_module
from candybar.api.app import NOISE_BATCH_MAX, NoiseBatchRequest, create_app
from contract_shapes import (
    SINGLE_KEYS,
    assert_contract_shape,
    assert_schedule_row,
    noise_batch_fields,
    noise_routes,
)
from test_api_definitions import T0, Rig, Row, rig  # noqa: F401  (fixture)

PROBLEM = "application/problem+json"
UPDATED = "bloodbank.cron.candybar.updated"
NOW = T0 + timedelta(minutes=30)


@pytest.fixture()
def noise(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> Rig:  # noqa: F811
    """Story 5.1's rig plus the batch locker and the /schedule reads."""
    rig.locked: list[set[UUID]] = []  # type: ignore[attr-defined]

    async def lock_noise_batch(session: Any, ids: Any) -> list[Row]:
        wanted = set(ids)
        rig.locked.append(wanted)  # type: ignore[attr-defined]
        rows = [r for r in rig.rows.values() if r.noise or r.id in wanted]
        return sorted(rows, key=lambda r: r.id)

    async def list_skips(session: Any, start: Any, end: Any = None) -> list[Any]:
        return []

    async def fire_results(session: Any, keys: Any) -> dict[str, str]:
        return {}

    monkeypatch.setattr(app_module, "lock_noise_batch", lock_noise_batch)
    monkeypatch.setattr(app_module, "list_skips", list_skips)
    monkeypatch.setattr(app_module, "fire_results", fire_results)
    return rig


def client(rig: Rig, *, raise_server_exceptions: bool = True) -> TestClient:  # noqa: F811
    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    app = create_app(session_factory=session_factory, sink=rig.sink, now=lambda: NOW)
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def noise_of(rig: Rig) -> dict[str, bool]:  # noqa: F811
    return {r.name: r.noise for r in rig.rows.values()}


def changes_of(rig: Rig) -> dict[str, dict[str, Any]]:  # noqa: F811
    by_id = {r.id: r.name for r in rig.rows.values()}
    out: dict[str, dict[str, Any]] = {}
    for event in rig.sink.published:
        assert event.type == UPDATED
        assert event.ordering_key == f"candybar:{event.data.candybar_id}"
        assert event.data.name == by_id[event.data.candybar_id]
        out[event.data.name] = event.data.changes
    assert len(out) == len(rig.sink.published), "one fact per candybar"
    return out


# ---------------------------------------------------------------------------
# Contract conformance.


def test_app_serves_exactly_the_contract_noise_routes() -> None:
    assert noise_routes() == {
        ("PUT", "/candybars/{id}/noise"),
        ("DELETE", "/candybars/{id}/noise"),
        ("PUT", "/noise/batch"),
    }

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    app = create_app(session_factory=session_factory, sink=None)
    served = {
        (method, re.sub(r"\{[a-z_]+\}", "{id}", route.path))
        for route in app.routes
        if isinstance(route, APIRoute) and "noise" in route.path
        for method in route.methods
    }
    assert served == noise_routes()


def test_batch_body_is_the_contract_shape() -> None:
    fields = noise_batch_fields()
    assert fields == {"ids": "list"}
    assert set(NoiseBatchRequest.model_fields) == set(fields)
    assert NoiseBatchRequest.model_fields["ids"].annotation == list[UUID]


# ---------------------------------------------------------------------------
# One candybar.


def test_put_adds_one_and_emits_one_fact(noise: Rig) -> None:
    row = noise.add("chatty", total_invoked=4, total_successful=3)
    other = noise.add("quiet")
    resp = client(noise).put(f"/candybars/{row.id}/noise")
    assert resp.status_code == 200
    body = resp.json()
    assert_contract_shape(body, SINGLE_KEYS)
    assert body["id"] == str(row.id) and body["noise"] is True and body["totalInvoked"] == 4
    assert changes_of(noise) == {"chatty": {"noise": True}}
    assert noise.rows[row.id].noise is True and noise.rows[other.id].noise is False
    assert noise.writes == 1


def test_delete_removes_one_and_emits_one_fact(noise: Rig) -> None:
    row = noise.add("chatty", noise=True)
    resp = client(noise).delete(f"/candybars/{row.id}/noise")
    assert resp.status_code == 200
    assert_contract_shape(resp.json(), SINGLE_KEYS)
    assert resp.json()["noise"] is False
    assert changes_of(noise) == {"chatty": {"noise": False}}
    assert noise.rows[row.id].noise is False and noise.writes == 1


@pytest.mark.parametrize(("method", "stored"), [("put", True), ("delete", False)])
def test_single_call_matching_the_stored_flag_is_a_no_op(
    noise: Rig, method: str, stored: bool
) -> None:
    row = noise.add("steady", noise=stored)
    resp = getattr(client(noise), method)(f"/candybars/{row.id}/noise")
    assert resp.status_code == 200
    assert_contract_shape(resp.json(), SINGLE_KEYS)
    assert resp.json()["noise"] is stored
    assert noise.sink.published == [] and noise.writes == 0


@pytest.mark.parametrize("method", ["put", "delete"])
def test_single_unknown_id_is_404_no_fact(noise: Rig, method: str) -> None:
    resp = getattr(client(noise), method)(f"/candybars/{uuid4()}/noise")
    assert resp.status_code == 404 and resp.headers["content-type"] == PROBLEM
    assert noise.sink.published == [] and noise.writes == 0


@pytest.mark.parametrize("method", ["put", "delete"])
def test_single_malformed_id_is_422(noise: Rig, method: str) -> None:
    resp = getattr(client(noise), method)("/candybars/not-a-uuid/noise")
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM


@pytest.mark.parametrize(("method", "stored"), [("put", False), ("delete", True)])
def test_single_bus_down_is_503_row_unchanged(noise: Rig, method: str, stored: bool) -> None:
    row = noise.add("offline", noise=stored)
    noise.sink.fail = True
    resp = getattr(client(noise), method)(f"/candybars/{row.id}/noise")
    assert resp.status_code == 503 and resp.headers["content-type"] == PROBLEM
    assert noise.rows[row.id].noise is stored and noise.writes == 0


def test_single_database_failure_is_500_problem(noise: Rig, monkeypatch: Any) -> None:
    async def boom(session: Any, candybar_id: UUID) -> Row:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app_module, "lock_candybar", boom)
    resp = client(noise, raise_server_exceptions=False).put(f"/candybars/{uuid4()}/noise")
    assert resp.status_code == 500 and resp.headers["content-type"] == PROBLEM


# ---------------------------------------------------------------------------
# The batch: replace the collection.


def test_batch_replaces_the_collection(noise: Rig) -> None:
    a = noise.add("a", noise=True)
    b = noise.add("b", noise=True)
    c = noise.add("c")
    d = noise.add("d")
    resp = client(noise).put("/noise/batch", json={"ids": [str(c.id), str(b.id), str(c.id)]})
    assert resp.status_code == 200
    # The resulting collection, oldest candybar first (b registered before c).
    assert resp.json() == {"ids": [str(b.id), str(c.id)]}
    # Only the rows whose flag actually flips get a fact and a write.
    assert changes_of(noise) == {"a": {"noise": False}, "c": {"noise": True}}
    assert noise_of(noise) == {"a": False, "b": True, "c": True, "d": False}
    assert noise.writes == 2
    # The lock set is the ids plus the current noise rows (a, b), deduplicated.
    assert noise.locked == [{b.id, c.id}]  # type: ignore[attr-defined]
    assert {d.id, a.id}.isdisjoint(noise.locked[0])  # type: ignore[attr-defined]


def test_batch_empty_list_clears_the_collection(noise: Rig) -> None:
    noise.add("a", noise=True)
    noise.add("b", noise=True)
    noise.add("c")
    resp = client(noise).put("/noise/batch", json={"ids": []})
    assert resp.status_code == 200 and resp.json() == {"ids": []}
    assert changes_of(noise) == {"a": {"noise": False}, "b": {"noise": False}}
    assert noise_of(noise) == {"a": False, "b": False, "c": False}


def test_batch_equal_to_the_collection_is_a_no_op(noise: Rig) -> None:
    a = noise.add("a", noise=True)
    noise.add("b")
    resp = client(noise).put("/noise/batch", json={"ids": [str(a.id)]})
    assert resp.status_code == 200 and resp.json() == {"ids": [str(a.id)]}
    assert noise.sink.published == [] and noise.writes == 0


def test_batch_with_an_unknown_id_changes_nothing(noise: Rig) -> None:
    a = noise.add("a")
    b = noise.add("b", noise=True)
    ghost = uuid4()
    resp = client(noise).put("/noise/batch", json={"ids": [str(a.id), str(ghost)]})
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert resp.json()["title"] == "Unknown candybar ids"
    assert str(ghost) in resp.json()["detail"] and str(a.id) not in resp.json()["detail"]
    assert noise.sink.published == [] and noise.writes == 0
    assert noise.rows[a.id].noise is False and noise.rows[b.id].noise is True


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"ids": None},
        {"ids": "not-a-list"},
        {"ids": ["not-a-uuid"]},
        {"ids": [None]},
        {"ids": [], "noise": True},
    ],
)
def test_batch_bad_body_is_422_no_fact(noise: Rig, body: dict[str, Any]) -> None:
    row = noise.add("a", noise=True)
    resp = client(noise).put("/noise/batch", json=body)
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert noise.sink.published == [] and noise.writes == 0 and noise.rows[row.id].noise


def test_batch_over_the_limit_is_422(noise: Rig) -> None:
    ids = [str(uuid4()) for _ in range(NOISE_BATCH_MAX + 1)]
    resp = client(noise).put("/noise/batch", json={"ids": ids})
    assert resp.status_code == 422 and resp.headers["content-type"] == PROBLEM
    assert noise.locked == []  # type: ignore[attr-defined]  # refused before any read


def test_batch_at_the_limit_is_accepted(noise: Rig) -> None:
    row = noise.add("a")
    ids = [str(row.id)] * NOISE_BATCH_MAX  # duplicates count toward the cap, then dedupe
    resp = client(noise).put("/noise/batch", json={"ids": ids})
    assert resp.status_code == 200 and resp.json() == {"ids": [str(row.id)]}


def test_batch_bus_failure_part_way_writes_nothing(noise: Rig, monkeypatch: Any) -> None:
    noise.add("a", noise=True)
    b = noise.add("b")
    c = noise.add("c")
    real_publish = noise.sink.publish
    calls = {"n": 0}

    async def flaky(event: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise nats.errors.TimeoutError
        await real_publish(event)

    monkeypatch.setattr(noise.sink, "publish", flaky)
    resp = client(noise).put("/noise/batch", json={"ids": [str(b.id), str(c.id)]})
    assert resp.status_code == 503 and resp.headers["content-type"] == PROBLEM
    assert "retry" in resp.json()["detail"]
    assert calls["n"] == 2 and len(noise.sink.published) == 1
    assert noise.writes == 0
    assert noise_of(noise) == {"a": True, "b": False, "c": False}


def test_batch_database_failure_is_500_problem(noise: Rig, monkeypatch: Any) -> None:
    async def boom(session: Any, ids: Any) -> list[Row]:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(app_module, "lock_noise_batch", boom)
    resp = client(noise, raise_server_exceptions=False).put("/noise/batch", json={"ids": []})
    assert resp.status_code == 500 and resp.headers["content-type"] == PROBLEM


@pytest.mark.parametrize(
    ("method", "path_of", "body"),
    [
        ("put", lambda r: f"/candybars/{r.id}/noise", None),
        ("put", lambda r: "/noise/batch", lambda r: {"ids": [str(r.id)]}),
    ],
)
def test_every_fact_is_published_before_any_row_write(
    noise: Rig, monkeypatch: Any, method: str, path_of: Any, body: Any
) -> None:
    """Spine state rule: facts first, then the rows, in the same flow."""
    row = noise.add("ordered")
    noise.add("was-noise", noise=True)  # the batch also flips this one
    order: list[str] = []
    real_publish = noise.sink.publish
    real_update = app_module.update_candybar

    async def publish(event: Any) -> None:
        order.append("publish")
        await real_publish(event)

    async def update(session: Any, r: Any, changes: dict[str, Any]) -> Any:
        assert changes.keys() == {"noise"}
        order.append("write")
        return await real_update(session, r, changes)

    monkeypatch.setattr(noise.sink, "publish", publish)
    monkeypatch.setattr(app_module, "update_candybar", update)
    kwargs = {"json": body(row)} if body else {}
    assert getattr(client(noise), method)(path_of(row), **kwargs).status_code == 200
    expected = ["publish", "write"] if body is None else ["publish", "publish", "write", "write"]
    assert order == expected


# ---------------------------------------------------------------------------
# What reads see afterwards.


def _schedule_noise(c: TestClient) -> dict[str, set[bool]]:
    start = (T0 + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    end = (T0 + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    resp = c.get("/schedule", params={"from": start, "to": end})
    assert resp.status_code == 200
    seen: dict[str, set[bool]] = {}
    for item in resp.json():
        assert_schedule_row(item)
        seen.setdefault(item["name"], set()).add(item["noise"])
    return seen


def test_schedule_rows_reflect_every_noise_call(noise: Rig) -> None:
    a = noise.add("alpha")
    b = noise.add("beta")
    c = client(noise)
    assert _schedule_noise(c) == {"alpha": {False}, "beta": {False}}

    assert c.put(f"/candybars/{a.id}/noise").status_code == 200
    assert _schedule_noise(c) == {"alpha": {True}, "beta": {False}}
    assert {i["name"]: i["noise"] for i in c.get("/candybars").json()} == {
        "alpha": True, "beta": False,
    }

    assert c.put("/noise/batch", json={"ids": [str(b.id)]}).status_code == 200
    assert _schedule_noise(c) == {"alpha": {False}, "beta": {True}}

    assert c.delete(f"/candybars/{b.id}/noise").status_code == 200
    assert _schedule_noise(c) == {"alpha": {False}, "beta": {False}}


def test_no_auto_noise_a_minutely_job_stays_out_until_added(noise: Rig) -> None:
    """The `<10m` classification is the view's: candybar stores only explicit calls."""
    c = client(noise)
    created = c.post(
        "/candybars", json={"name": "every-minute", "schedule": "* * * * *",
                            "entrypoint": "/bin/true"},
    )
    assert created.status_code == 201 and created.json()["noise"] is False
    # The rig stamps registration with the real clock; pin it before the window.
    noise.rows[UUID(created.json()["id"])].created_at = T0
    assert _schedule_noise(c) == {"every-minute": {False}}
    assert c.get("/candybars").json()[0]["noise"] is False

    hourly = noise.add("hourly", schedule="0 * * * *")
    assert c.put(f"/candybars/{hourly.id}/noise").json()["noise"] is True
    assert _schedule_noise(c) == {"every-minute": {False}, "hourly": {True}}


def test_noise_facts_carry_only_the_noise_change(noise: Rig) -> None:
    row = noise.add("job", schedule="*/5 * * * *", description="kept", tags=["t"])
    client(noise).put(f"/candybars/{row.id}/noise")
    (event,) = noise.sink.published
    assert event.data.changes == {"noise": True}
    assert isinstance(event.time, datetime)


async def asgi_put(app: Any, path: str, body: bytes) -> int:
    """One PUT through the ASGI app on the CURRENT loop (the hub's), returning the status.

    TestClient runs the app on its own thread and loop; the live hub and its
    reader must share one loop with the route, so the request is driven here.
    """
    sent = False
    status: list[int] = []

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            status.append(message["status"])

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "PUT",
        "scheme": "http", "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1), "server": ("candybar", 80),
    }
    await app(scope, receive, send)
    return status[0]


def test_the_routes_facts_reach_the_stream_before_the_rows_commit(
    noise: Rig, monkeypatch: Any
) -> None:
    """The facts the routes publish, fed to the live hub mid-request, patch the view.

    The sink hands each fact to the hub before the route writes the row (the
    spine's event-first order), so the hub reads the OLD flag from the
    registry and must overlay the fact: `definition.changed` still reports
    the new value.
    """
    import asyncio
    import json

    import candybar.api.stream as stream_module
    from candybar.api.stream import EVENT_DEFINITION_CHANGED, ScheduleStream, parse_fact
    from test_schedule_stream import Client, dispatched

    a = noise.add("alpha")
    b = noise.add("beta", noise=True)
    for name in ("get_candybar", "list_candybars", "list_skips", "fire_results"):
        monkeypatch.setattr(stream_module, name, getattr(app_module, name))

    @asynccontextmanager
    async def session_factory() -> AsyncIterator[Any]:
        yield object()

    async def scenario() -> dict[str, dict[str, Any]]:
        hub = ScheduleStream(session_factory=session_factory, now=lambda: NOW)
        await hub.start()
        stale: list[bool] = []

        class HubSink:
            async def publish(self, event: Any) -> None:
                fact = parse_fact(event.model_dump_json().encode())
                stale.append(noise.rows[fact.data.candybar_id].noise != fact.data.changes["noise"])
                await hub.handle(fact)

        app = create_app(session_factory=session_factory, sink=HubSink(), now=lambda: NOW,
                         stream=hub)
        viewer = Client(hub)
        await viewer.read()
        status = await asgi_put(app, "/noise/batch", json.dumps({"ids": [str(a.id)]}).encode())
        assert status == 200
        assert stale == [True, True]  # each fact reached the hub before its row write
        frames = dispatched(await viewer.read())
        await viewer.close()
        return {
            f["data"]["candybarId"]: f["data"]
            for f in frames
            if f["event"] == EVENT_DEFINITION_CHANGED
        }

    changed = asyncio.run(scenario())
    assert set(changed) == {str(a.id), str(b.id)}
    assert changed[str(a.id)]["changes"] == {"noise": True}
    assert changed[str(a.id)]["definition"]["noise"] is True
    assert changed[str(b.id)]["changes"] == {"noise": False}
    assert changed[str(b.id)]["definition"]["noise"] is False
    assert noise_of(noise) == {"alpha": True, "beta": False}
