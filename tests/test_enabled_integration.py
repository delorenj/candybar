"""Live-Postgres proof of the `enabled` flag (Story 7.5, AD-21).

Env-gated: CANDYBAR_TEST_PG=1 with DATABASE_URL at a THROWAWAY database
(`migrated_db` downgrades to base afterwards). Through the real routes on
real Postgres: migration 0007 gives existing rows `enabled = true`; a
candybar registered disabled is stored disabled, listed with
`enabled: false`, absent from the dispatcher's `list_schedules` and from the
executor's `candybar_enabled` read as False; `POST /candybars/{id}/enabled`
flips it with one `updated` fact; and the 0007 downgrade drops the column.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text

from candybar.registry import candybar_enabled, create_registry_engine, session_scope
from candybar.registry.store import list_schedules
from test_registry_integration import migrated_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)


def _reads(dsn: str, ids: dict[str, str]) -> tuple[set[str], dict[str, bool | None]]:
    async def read() -> tuple[set[str], dict[str, bool | None]]:
        engine = create_registry_engine(dsn)
        try:
            async with session_scope(engine) as session:
                scheduled = {e.name for e in await list_schedules(session)}
                flags = {n: await candybar_enabled(session, UUID(i)) for n, i in ids.items()}
                return scheduled, flags
        finally:
            await engine.dispose()

    return asyncio.run(read())


def test_enabled_round_trip_on_postgres(migrated_db: str) -> None:  # noqa: F811
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from candybar.api.app import create_app
    from candybar.events.parse import parse_envelope
    from contract_shapes import LIST_KEYS, assert_contract_shape

    class Sink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        async def publish(self, event: Any) -> None:
            self.events.append(parse_envelope(json.loads(event.model_dump_json())))

    sink = Sink()
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(create_app(session_factory=lambda: session_scope(engine), sink=sink))

    ids: dict[str, str] = {}
    for name, enabled in [("stays-on", None), ("migrant", False)]:
        body: dict[str, Any] = {
            "name": name, "schedule": "* * * * *",
            "runner": {"kind": "systemd", "unit": f"{name}.service"},
        }
        if enabled is not None:
            body["enabled"] = enabled
        resp = client.post("/candybars", json=body)
        assert resp.status_code == 201, resp.text
        ids[name] = resp.json()["id"]
    assert [e.data.enabled for e in sink.events] == [True, False]

    listed = {item["name"]: item for item in client.get("/candybars").json()}
    for item in listed.values():
        assert_contract_shape(item, LIST_KEYS)
    assert listed["stays-on"]["enabled"] is True and listed["migrant"]["enabled"] is False
    scheduled, flags = _reads(migrated_db, ids)
    assert scheduled == {"stays-on"}
    assert flags == {"stays-on": True, "migrant": False}

    sink.events.clear()
    resp = client.post(f"/candybars/{ids['migrant']}/enabled", json={"enabled": True})
    assert resp.status_code == 200 and resp.json()["enabled"] is True
    assert [e.data.changes for e in sink.events] == [{"enabled": True}]
    scheduled, flags = _reads(migrated_db, ids)
    assert scheduled == {"stays-on", "migrant"} and flags["migrant"] is True

    resp = client.post(f"/candybars/{ids['stays-on']}/enabled", json={"enabled": False})
    assert resp.status_code == 200
    scheduled, _ = _reads(migrated_db, ids)
    assert scheduled == {"migrant"}
    assert _reads(migrated_db, {"ghost": "00000000-0000-7000-8000-000000000000"})[1] == {
        "ghost": None
    }
    asyncio.run(engine.dispose())


def test_migration_0007_defaults_existing_rows_to_enabled(migrated_db: str) -> None:  # noqa: F811
    from alembic import command
    from alembic.config import Config

    from test_registry_integration import ALEMBC_INI, REPO_ROOT, _sync_url

    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.downgrade(cfg, "0006")
    sync = migrated_db
    with create_engine(sync).begin() as conn:
        conn.execute(text(
            "INSERT INTO candybars (id, name, description, tags, schedule, timezone, entrypoint, "
            "noise, total_invoked, total_successful, created_at, updated_at) VALUES "
            "('01a176ae-bd0a-7b8f-8e20-08665b8beb9a', 'old', '', '[]', '* * * * *', 'UTC', "
            "'/bin/true', false, 0, 0, now(), now())"
        ))
    command.upgrade(cfg, "head")
    with create_engine(sync).connect() as conn:
        assert conn.execute(text("SELECT enabled FROM candybars WHERE name = 'old'")).scalar() is True
