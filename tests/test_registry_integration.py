"""Live-Postgres integration tests for the registry (env-gated: CANDYBAR_TEST_PG=1).

Run against a real Postgres (CI spins postgres:17-alpine, matching the live
17.10 server). DATABASE_URL points at a throwaway database
(default: candybar_test). Covers: migration up/down, ERD shape, owner-only
privileges (AD-10), register round-trip, duplicate rejection.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from candybar.registry import (
    CandybarManifest,
    DuplicateNameError,
    create_registry_engine,
    insert_candybar,
    name_taken,
    normalize_dsn,
    session_scope,
    uuid7,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ALEMBC_INI = REPO_ROOT / "deploy" / "alembic.ini"


def _dsn() -> str:
    return normalize_dsn(
        os.environ.get("DATABASE_URL", "postgresql://localhost:5432/candybar_test")
    )


def _sync_url() -> str:
    return _dsn()  # psycopg (v3) serves both sync and async


@pytest.fixture()
def migrated_db():
    """Upgrade to head, yield the DSN, downgrade to base afterwards."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "head")
    try:
        yield _dsn()
    finally:
        command.downgrade(cfg, "base")


def test_migration_creates_erd_tables(migrated_db: str) -> None:
    with create_engine(migrated_db).connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
            )
        }
        assert {"candybars", "invocations", "skips"} <= tables
        cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'candybars'"
                )
            )
        }
        assert {
            "id",
            "name",
            "description",
            "tags",
            "schedule",
            "entrypoint",
            "noise",
            "total_invoked",
            "total_successful",
        } <= cols
        # invocations defaults: no invocations yet -> counters start at zero
        assert conn.execute(text("SELECT count(*) FROM candybars")).scalar() == 0


def test_migration_grants_no_other_role(migrated_db: str) -> None:
    """Owner-only privileges: relacl NULL means no grants beyond the owner (AD-10)."""
    with create_engine(migrated_db).connect() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename, relacl FROM pg_tables t "
                "JOIN pg_class c ON c.relname = t.tablename "
                "WHERE t.schemaname = 'public' AND t.tablename IN "
                "('candybars', 'invocations', 'skips')"
            )
        ).all()
        assert len(rows) == 3
        for _table, relacl in rows:
            assert relacl is None, f"{_table} has explicit grants: {relacl}"


def test_register_round_trip_and_duplicate(migrated_db: str) -> None:
    import asyncio

    async def scenario() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            manifest = CandybarManifest(
                name="itest", schedule="* * * * *", entrypoint="/bin/true"
            )
            async with session_scope(engine) as session:
                assert not await name_taken(session, "itest")
                row = await insert_candybar(session, manifest, candybar_id=uuid7())
            assert row.total_invoked == 0 and row.total_successful == 0

            async with session_scope(engine) as session:
                assert await name_taken(session, "itest")

            # Duplicate insert violates the unique name constraint.
            with pytest.raises(DuplicateNameError):
                async with session_scope(engine) as session:
                    await insert_candybar(session, manifest)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_invocations_and_skips_shape(migrated_db: str) -> None:
    """The 1.6/Epic-2 columns exist with the constraints the spine requires."""
    with create_engine(migrated_db).connect() as conn:
        inv_cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'invocations'"
                )
            )
        }
        assert {
            "id",
            "candybar_id",
            "trigger",
            "tick_id",
            "idempotency_key",
            "result",
            "exit_code",
            "runtime_ms",
            "stderr_tail",
            "token_spend",
            "started_at",
            "finished_at",
        } <= inv_cols
        # trigger check constraint rejects garbage
        conn.execute(
            text(
                "INSERT INTO candybars (id, name, description, tags, schedule, entrypoint, "
                "noise, total_invoked, total_successful, created_at, updated_at) VALUES "
                "('00000000-0000-7000-8000-000000000001', 'shape', '', '[]', '* * * * *', "
                "'/bin/true', false, 0, 0, now(), now())"
            )
        )
        with pytest.raises(Exception, match="invocations_trigger_check"):
            conn.execute(
                text(
                    "INSERT INTO invocations (id, candybar_id, trigger, idempotency_key, "
                    "started_at, created_at) VALUES "
                    "('00000000-0000-7000-8000-000000000002', "
                    "'00000000-0000-7000-8000-000000000001', 'bogus', 'k1', now(), now())"
                )
            )
