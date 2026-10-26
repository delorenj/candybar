"""Live-Postgres proof of the noise collection endpoints (Story 5.5).

Env-gated: CANDYBAR_TEST_PG=1 with DATABASE_URL at a THROWAWAY database
(`migrated_db` downgrades the schema to base afterwards). The real routes run
on real Postgres through `session_scope`: single add/remove, batch replace
and clear, an unknown id refusing the whole batch, a write failure mid-batch
rolling every row back, `/schedule` rows reading the stored flag, and
`lock_noise_batch` holding exactly the rows a batch can change.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from candybar.registry import (
    CandybarManifest,
    create_registry_engine,
    get_candybar,
    insert_candybar,
    lock_candybar,
    lock_noise_batch,
    session_scope,
    update_candybar,
    uuid7,
)
from test_registry_integration import migrated_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)


def _stored_noise(dsn: str, ids: dict[str, str]) -> dict[str, bool]:
    async def read() -> dict[str, bool]:
        engine = create_registry_engine(dsn)
        try:
            async with session_scope(engine) as session:
                return {
                    name: (await get_candybar(session, UUID(cid))).noise
                    for name, cid in ids.items()
                }
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_noise_routes_round_trip_on_postgres(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    import candybar.api.app as app_module
    from candybar.api.app import create_app
    from candybar.events.parse import parse_envelope
    from contract_shapes import SINGLE_KEYS, assert_contract_shape, assert_schedule_row

    class Sink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(parse_envelope(json.loads(event.model_dump_json())))

        def changes(self) -> dict[UUID, dict[str, Any]]:
            out = {e.data.candybar_id: e.data.changes for e in self.events}
            assert len(out) == len(self.events), "one fact per candybar"
            assert {e.type for e in self.events} <= {"bloodbank.cron.candybar.updated"}
            return out

    sink = Sink()
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(create_app(session_factory=lambda: session_scope(engine), sink=sink))

    ids: dict[str, str] = {}
    for name, schedule in [("noise-a", "*/5 * * * *"), ("noise-b", "0 * * * *"),
                           ("noise-c", "* * * * *")]:
        resp = client.post(
            "/candybars", json={"name": name, "schedule": schedule, "entrypoint": "/bin/true"}
        )
        assert resp.status_code == 201 and resp.json()["noise"] is False  # no auto-noise
        ids[name] = resp.json()["id"]
    a, b, c = (UUID(ids[n]) for n in ("noise-a", "noise-b", "noise-c"))

    def schedule_noise() -> dict[str, set[bool]]:
        start = (datetime.now(UTC) + timedelta(minutes=2)).replace(second=0, microsecond=0)
        params = {"from": start.isoformat().replace("+00:00", "Z"),
                  "to": (start + timedelta(hours=1)).isoformat().replace("+00:00", "Z")}
        resp = client.get("/schedule", params=params)
        assert resp.status_code == 200
        seen: dict[str, set[bool]] = {}
        for item in resp.json():
            assert_schedule_row(item)
            seen.setdefault(item["name"], set()).add(item["noise"])
        return seen

    # Single add: one fact, the row committed, /schedule reflects it.
    sink.events.clear()
    added = client.put(f"/candybars/{a}/noise")
    assert added.status_code == 200
    assert_contract_shape(added.json(), SINGLE_KEYS)
    assert added.json()["noise"] is True
    assert sink.changes() == {a: {"noise": True}}
    assert _stored_noise(migrated_db, ids) == {"noise-a": True, "noise-b": False, "noise-c": False}
    assert schedule_noise() == {"noise-a": {True}, "noise-b": {False}, "noise-c": {False}}

    sink.events.clear()
    assert client.put(f"/candybars/{a}/noise").status_code == 200
    assert sink.events == []  # already noise: no fact

    # Batch replace: a leaves, b and c join, in registry order.
    sink.events.clear()
    batch = client.put("/noise/batch", json={"ids": [ids["noise-c"], ids["noise-b"]]})
    assert batch.status_code == 200
    assert batch.json() == {"ids": [ids["noise-b"], ids["noise-c"]]}
    assert sink.changes() == {a: {"noise": False}, b: {"noise": True}, c: {"noise": True}}
    assert _stored_noise(migrated_db, ids) == {"noise-a": False, "noise-b": True, "noise-c": True}
    assert schedule_noise() == {"noise-a": {False}, "noise-b": {True}, "noise-c": {True}}

    # Single remove.
    sink.events.clear()
    removed = client.delete(f"/candybars/{c}/noise")
    assert removed.status_code == 200 and removed.json()["noise"] is False
    assert sink.changes() == {c: {"noise": False}}

    # An unknown id refuses the whole batch: nothing published, nothing written.
    sink.events.clear()
    ghost = uuid7()
    refused = client.put("/noise/batch", json={"ids": [ids["noise-a"], str(ghost)]})
    assert refused.status_code == 422 and str(ghost) in refused.json()["detail"]
    assert sink.events == []
    assert _stored_noise(migrated_db, ids) == {"noise-a": False, "noise-b": True, "noise-c": False}

    # A write failing part-way rolls back every row of the batch.
    real_update = app_module.update_candybar
    calls = {"n": 0}

    async def failing_second_write(session: Any, row: Any, changes: dict[str, Any]) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("simulated write failure")
        return await real_update(session, row, changes)

    monkeypatch.setattr(app_module, "update_candybar", failing_second_write)
    failing = TestClient(
        create_app(session_factory=lambda: session_scope(engine), sink=sink),
        raise_server_exceptions=False,
    )
    sink.events.clear()
    broken = failing.put("/noise/batch", json={"ids": [ids["noise-a"], ids["noise-c"]]})
    assert broken.status_code == 500
    assert calls["n"] == 2  # the first row WAS written in the transaction, then rolled back
    assert _stored_noise(migrated_db, ids) == {"noise-a": False, "noise-b": True, "noise-c": False}
    monkeypatch.setattr(app_module, "update_candybar", real_update)

    # Clear.
    sink.events.clear()
    cleared = client.put("/noise/batch", json={"ids": []})
    assert cleared.status_code == 200 and cleared.json() == {"ids": []}
    assert sink.changes() == {b: {"noise": False}}
    assert _stored_noise(migrated_db, ids) == {"noise-a": False, "noise-b": False, "noise-c": False}
    assert client.put(f"/candybars/{uuid7()}/noise").status_code == 404

    asyncio.run(engine.dispose())


def test_lock_noise_batch_holds_exactly_the_rows_a_batch_can_change(
    migrated_db: str,  # noqa: F811
) -> None:
    async def scenario() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            # Insert in DESCENDING id order, so heap (scan) order is the reverse
            # of id order: only the statement's ORDER BY can return id order.
            names = [("lock-n1", True), ("lock-n2", True), ("lock-x", False), ("lock-y", False)]
            minted = sorted((uuid7() for _ in names), reverse=True)
            async with session_scope(engine) as session:
                made = {}
                for (name, noisy), candybar_id in zip(names, minted, strict=True):
                    made[name] = await insert_candybar(
                        session, CandybarManifest(name=name, schedule="* * * * *",
                                                  entrypoint="/bin/true", noise=noisy),
                        candybar_id=candybar_id,
                    )
            n1, n2, x, y = (made[n].id for n in ("lock-n1", "lock-n2", "lock-x", "lock-y"))
            assert [n1, n2, x] == sorted([n1, n2, x], reverse=True)
            ghost = uuid7()

            async with session_scope(engine) as session:
                rows = await lock_noise_batch(session, {x, ghost})
                # The given ids that exist plus today's noise rows, in id order.
                assert [r.id for r in rows] == sorted([n1, n2, x])

            locked = asyncio.Event()
            release = asyncio.Event()

            async def holder() -> None:
                async with session_scope(engine) as session:
                    held = await lock_noise_batch(session, {x})
                    locked.set()
                    await release.wait()
                    for row in held:
                        await update_candybar(session, row, {"noise": row.id == x})

            async def lock_one(candybar_id: UUID) -> bool:
                async with session_scope(engine) as session:
                    return (await lock_candybar(session, candybar_id)).noise

            hold = asyncio.create_task(holder())
            await locked.wait()
            # A row outside the batch's set is free.
            assert await asyncio.wait_for(lock_one(y), timeout=5) is False
            # A row inside it waits for the batch, then sees its committed value.
            waiter = asyncio.create_task(lock_one(n1))
            done, _ = await asyncio.wait({waiter}, timeout=0.5)
            assert not done, "a single-row lock did not wait for the batch's lock"
            release.set()
            await hold
            assert await asyncio.wait_for(waiter, timeout=5) is False
        finally:
            await engine.dispose()

    asyncio.run(scenario())
