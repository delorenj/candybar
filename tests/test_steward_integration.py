"""Live-Postgres proof of migrations 0008-0009 and the steward contract routes.

Env-gated: CANDYBAR_TEST_PG=1 with DATABASE_URL at a THROWAWAY database
(`migrated_db` downgrades to base afterwards). Through the real routes on
real Postgres: 0008 gives existing rows `archived = false`; archive disables
in one write and enabling an archived candybar is a 409; a profile is created
lazily and merged per top-level key; a steward request stores one queued task
and a twin is refused by the partial unique index too; task lists and the
PATCH status flow; and the 0008 downgrade drops what it added.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from candybar.registry import (
    DuplicateActiveTaskError,
    insert_steward_task,
    session_scope,
    uuid7,
)
from test_api_steward import overview
from test_registry_integration import migrated_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)


class Sink:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        from candybar.events.parse import parse_envelope

        self.events.append(parse_envelope(json.loads(event.model_dump_json())))


def test_steward_contract_round_trip_on_postgres(migrated_db: str) -> None:  # noqa: F811
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from candybar.api.app import create_app

    sink = Sink()
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(create_app(session_factory=lambda: session_scope(engine), sink=sink))

    resp = client.post(
        "/candybars",
        json={"name": "vault", "schedule": "30 4 * * *", "runner": {"kind": "systemd", "unit": "vault.service"}},
    )
    assert resp.status_code == 201, resp.text
    cid = resp.json()["id"]
    assert resp.json()["archived"] is False

    # Archive: one write, one fact; enabling is refused until unarchived.
    sink.events.clear()
    resp = client.post(f"/candybars/{cid}/archived", json={"archived": True})
    assert resp.status_code == 200 and resp.json()["archived"] is True and resp.json()["enabled"] is False
    assert [e.data.changes for e in sink.events] == [{"archived": True, "enabled": False}]
    assert client.post(f"/candybars/{cid}/enabled", json={"enabled": True}).status_code == 409
    assert client.post(f"/candybars/{cid}/steward", json={"action": "fix"}).status_code == 409
    resp = client.post(f"/candybars/{cid}/archived", json={"archived": False})
    assert resp.json()["archived"] is False and resp.json()["enabled"] is False
    assert client.post(f"/candybars/{cid}/enabled", json={"enabled": True}).json()["enabled"] is True

    # Profile: lazy, merged per key.
    assert client.get(f"/candybars/{cid}/profile").json()["updatedAt"] is None
    resp = client.put(f"/candybars/{cid}/profile", json={"overview": overview(), "templateVersion": 1})
    assert resp.status_code == 200, resp.text
    resp = client.put(
        f"/candybars/{cid}/profile",
        json={"project": {"id": "docker", "name": "Docker", "confidence": 1.0, "method": "ledger"}},
    )
    body = resp.json()
    assert body["overview"]["cadenceNote"] and body["templateVersion"] == 1
    assert body["project"]["id"] == "docker"
    assert client.get("/profiles").json()["profiles"] == [body]

    # Steward: one queued task, a twin is a 409 (route and index both).
    resp = client.post(f"/candybars/{cid}/steward", json={"action": "evaluate"})
    assert resp.status_code == 202, resp.text
    task = resp.json()
    assert task["status"] == "queued" and task["model"] == "automaticai/auto"
    twin = client.post(f"/candybars/{cid}/steward", json={"action": "evaluate"})
    assert twin.status_code == 409 and twin.json()["taskId"] == task["id"]

    async def index_twin() -> None:
        async with session_scope(engine) as session:
            await insert_steward_task(
                session, task_id=uuid7(), candybar_id=UUID(cid), action="evaluate", model="m"
            )

    with pytest.raises(DuplicateActiveTaskError):
        asyncio.run(index_twin())

    assert [t["id"] for t in client.get("/steward/tasks?active=1").json()["tasks"]] == [task["id"]]
    resp = client.patch(f"/steward/tasks/{task['id']}", json={"status": "running", "sessionId": "ses_x"})
    assert resp.status_code == 200 and resp.json()["startedAt"]
    resp = client.patch(
        f"/steward/tasks/{task['id']}",
        json={"status": "succeeded", "outcome": "Nominal", "summary": "fine", "reportKey": "2026-11-03/x.md"},
    )
    assert resp.status_code == 200 and resp.json()["finishedAt"] and resp.json()["outcome"] == "Nominal"
    tokens = {"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 3, "write": 0}}
    resp = client.patch(f"/steward/tasks/{task['id']}", json={"costUsd": 0.0125, "tokens": tokens})
    assert resp.status_code == 200 and resp.json()["costUsd"] == 0.0125 and resp.json()["tokens"] == tokens
    assert client.get("/steward/tasks?active=1").json()["tasks"] == []
    listed = client.get(f"/candybars/{cid}/steward/tasks").json()
    assert [t["status"] for t in listed["tasks"]] == ["succeeded"]
    # The index frees the slot once terminal.
    assert client.post(f"/candybars/{cid}/steward", json={"action": "evaluate"}).status_code == 202
    types = [e.type.rsplit(".", 1)[1] for e in sink.events if "steward_task" in e.type]
    assert types == ["queued", "started", "completed", "queued"]
    # Cancel: the queued twin becomes failed ("cancelled by operator"); again is a 409.
    queued = client.get("/steward/tasks?active=1").json()["tasks"][0]
    resp = client.post(f"/steward/tasks/{queued['id']}/cancel")
    assert resp.status_code == 200 and resp.json()["error"] == "cancelled by operator"
    assert client.post(f"/steward/tasks/{queued['id']}/cancel").status_code == 409
    asyncio.run(engine.dispose())


def test_migration_0009_adds_nullable_spend_and_downgrades(migrated_db: str) -> None:  # noqa: F811
    from alembic import command
    from alembic.config import Config

    from test_registry_integration import ALEMBC_INI, REPO_ROOT, _sync_url

    def columns() -> dict[str, str]:
        with create_engine(migrated_db).connect() as conn:
            return {r[0]: r[1] for r in conn.execute(text(
                "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'steward_tasks'"
            ))}

    assert columns().get("cost_usd") == "YES" and columns().get("tokens") == "YES"
    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.downgrade(cfg, "0008")
    assert not {"cost_usd", "tokens"} & set(columns())
    command.upgrade(cfg, "head")
    assert {"cost_usd", "tokens"} <= set(columns())


def test_migration_0008_defaults_and_downgrade(migrated_db: str) -> None:  # noqa: F811
    from alembic import command
    from alembic.config import Config

    from test_registry_integration import ALEMBC_INI, REPO_ROOT, _sync_url

    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.downgrade(cfg, "0007")
    with create_engine(migrated_db).begin() as conn:
        conn.execute(text(
            "INSERT INTO candybars (id, name, description, tags, schedule, timezone, entrypoint, "
            "noise, enabled, total_invoked, total_successful, created_at, updated_at) VALUES "
            "('01a176ae-bd0a-7b8f-8e20-08665b8beb9b', 'old', '', '[]', '* * * * *', 'UTC', "
            "'/bin/true', false, true, 0, 0, now(), now())"
        ))
    command.upgrade(cfg, "head")
    with create_engine(migrated_db).connect() as conn:
        assert conn.execute(text("SELECT archived FROM candybars WHERE name = 'old'")).scalar() is False
        tables = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert {"candybar_profiles", "steward_tasks"} <= tables
    command.downgrade(cfg, "0007")
    with create_engine(migrated_db).connect() as conn:
        tables = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
        assert not {"candybar_profiles", "steward_tasks"} & tables
        cols = {r[0] for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'candybars'"
        ))}
        assert "archived" not in cols
    command.upgrade(cfg, "head")
