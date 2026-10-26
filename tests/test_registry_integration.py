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


def test_invocation_and_skip_models_round_trip(migrated_db: str) -> None:
    """Pin the ORM models against the MIGRATED schema (drift breaks 1.6/Epic 2)."""
    import asyncio
    from datetime import UTC, datetime

    from sqlalchemy import select

    from candybar.registry.models import Candybar, Invocation, Skip

    async def scenario() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            cb_id = uuid7()
            async with session_scope(engine) as session:
                session.add(
                    Candybar(
                        id=cb_id,
                        name="orm-roundtrip",
                        description="",
                        tags=[],
                        schedule="* * * * *",
                        entrypoint="/bin/true",
                        noise=False,
                        total_invoked=0,
                        total_successful=0,
                        created_at=datetime.now(UTC),
                        updated_at=datetime.now(UTC),
                    )
                )
            async with session_scope(engine) as session:
                now = datetime.now(UTC)
                session.add(
                    Invocation(
                        id=uuid7(),
                        candybar_id=cb_id,
                        trigger="manual",
                        tick_id=None,
                        idempotency_key="candybar:x:manual:test",
                        result=None,
                        started_at=now,
                        created_at=now,
                    )
                )
                session.add(
                    Skip(id=uuid7(), candybar_id=cb_id, kind="once", created_at=now)
                )
            async with session_scope(engine) as session:
                inv = (
                    await session.execute(select(Invocation).where(Invocation.candybar_id == cb_id))
                ).scalar_one()
                assert inv.trigger == "manual" and inv.result is None
                assert inv.started_at.tzinfo is not None  # timestamptz, not naive
                skip = (
                    await session.execute(select(Skip).where(Skip.candybar_id == cb_id))
                ).scalar_one()
                assert skip.kind == "once"
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
            "memory_peak_bytes",
            "cpu_load",
            "gpu_load",
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


def test_resource_metering_round_trip_and_constraints(migrated_db: str) -> None:
    """Story 3.1: measures persist through record_terminal_invocation (migration 0002)."""
    import asyncio
    from datetime import UTC, datetime

    from candybar.registry import TerminalInvocation, record_terminal_invocation
    from candybar.registry.models import Invocation

    async def scenario() -> tuple[int, float, int, float]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                row = await insert_candybar(
                    session,
                    CandybarManifest(
                        name="metered", description="", tags=[], schedule="* * * * *",
                        entrypoint="/bin/true",
                    ),
                )
            now = datetime.now(UTC)
            measured, legacy = uuid7(), uuid7()
            async with session_scope(engine) as session:
                await record_terminal_invocation(
                    session,
                    TerminalInvocation(
                        invocation_id=measured, candybar_id=row.id, trigger="manual",
                        tick_id=None, idempotency_key=f"candybar:{row.id}:manual:m",
                        result="success", exit_code=0, runtime_ms=800, error_message=None,
                        stderr_tail=None, token_spend=0, cost_usd=None, started_at=now,
                        finished_at=now, memory_peak_bytes=64 * 1024 * 1024, cpu_load=0.9738,
                    ),
                )
                await record_terminal_invocation(  # pre-3.1 caller: defaults
                    session,
                    TerminalInvocation(
                        invocation_id=legacy, candybar_id=row.id, trigger="manual",
                        tick_id=None, idempotency_key=f"candybar:{row.id}:manual:l",
                        result="fail", exit_code=1, runtime_ms=5, error_message="x",
                        stderr_tail=None, token_spend=0, cost_usd=None, started_at=now,
                        finished_at=now,
                    ),
                )
            async with session_scope(engine) as session:
                m = await session.get(Invocation, measured)
                lg = await session.get(Invocation, legacy)
                assert m is not None and lg is not None
                return m.memory_peak_bytes, m.cpu_load, lg.memory_peak_bytes, lg.cpu_load
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (64 * 1024 * 1024, 0.9738, 0, 0.0)
    with (
        create_engine(migrated_db).connect() as conn,
        pytest.raises(Exception, match="invocations_metering_nonnegative"),
    ):
        conn.execute(text("UPDATE invocations SET cpu_load = -1"))


def test_gpu_load_round_trip_default_and_constraint(migrated_db: str) -> None:
    """Story 3.2: gpu_load persists (migration 0004); omitted -> 0; negative rejected."""
    import asyncio
    from datetime import UTC, datetime

    from candybar.registry import TerminalInvocation, record_terminal_invocation
    from candybar.registry.models import Invocation

    async def scenario() -> tuple[float, float]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                row = await insert_candybar(
                    session,
                    CandybarManifest(
                        name="gpu-metered", description="", tags=[], schedule="* * * * *",
                        entrypoint="/bin/true",
                    ),
                )
            now = datetime.now(UTC)
            measured, legacy = uuid7(), uuid7()
            common = {
                "candybar_id": row.id, "trigger": "manual", "tick_id": None, "result": "success",
                "exit_code": 0, "runtime_ms": 4000, "error_message": None, "stderr_tail": None,
                "token_spend": 0, "cost_usd": None, "started_at": now, "finished_at": now,
            }
            async with session_scope(engine) as session:
                await record_terminal_invocation(
                    session,
                    TerminalInvocation(
                        invocation_id=measured, idempotency_key=f"candybar:{row.id}:manual:g",
                        gpu_load=0.9612, **common,
                    ),
                )
                await record_terminal_invocation(  # pre-3.2 caller: default 0
                    session,
                    TerminalInvocation(
                        invocation_id=legacy, idempotency_key=f"candybar:{row.id}:manual:n",
                        **common,
                    ),
                )
            async with session_scope(engine) as session:
                m = await session.get(Invocation, measured)
                lg = await session.get(Invocation, legacy)
                assert m is not None and lg is not None
                return m.gpu_load, lg.gpu_load
        finally:
            await engine.dispose()

    assert asyncio.run(scenario()) == (0.9612, 0.0)
    with (
        create_engine(migrated_db).connect() as conn,
        pytest.raises(Exception, match="invocations_gpu_load_nonnegative"),
    ):
        conn.execute(text("UPDATE invocations SET gpu_load = -0.5"))


def test_dispatch_cursor_is_owner_only_monotonic_and_utc(migrated_db: str) -> None:
    """Story 2.2: migration 0003's cursor row only ever moves forward, and the
    schedule inventory carries created_at for the misfire floor."""
    import asyncio
    from datetime import UTC, datetime, timedelta

    from candybar.registry import advance_dispatch_cursor, get_dispatch_cursor
    from candybar.registry.store import list_schedules

    with create_engine(migrated_db).connect() as conn:
        relacl = conn.execute(
            text("SELECT relacl FROM pg_class WHERE relname = 'dispatch_cursor'")
        ).scalar_one()
        assert relacl is None  # owner-only, like the ERD tables (AD-10)

    base = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)

    async def scenario() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                assert await get_dispatch_cursor(session, "d") is None
                await advance_dispatch_cursor(session, "d", base)
            # Concurrent advances (two dispatchers, redelivered older ticks):
            # the row converges on the newest minute, never rewinds.
            minutes = [base + timedelta(minutes=m) for m in (5, 2, 9, 1, 7, 9, 3)]

            async def advance(minute: datetime) -> None:
                async with session_scope(engine) as session:
                    await advance_dispatch_cursor(session, "d", minute)

            await asyncio.gather(*(advance(m) for m in minutes))
            await advance(base)  # an old replay
            async with session_scope(engine) as session:
                got = await get_dispatch_cursor(session, "d")
                assert got == base + timedelta(minutes=9) and got.utcoffset() == timedelta(0)
                assert await get_dispatch_cursor(session, "other") is None  # keyed by durable
            with pytest.raises(ValueError, match="minute boundary"):
                await advance(base + timedelta(seconds=30))
            with pytest.raises(ValueError, match="UTC"):
                await advance(datetime(2026, 10, 26, 10, 0))
            async with session_scope(engine) as session:
                row = await insert_candybar(
                    session,
                    CandybarManifest(name="cursor-inv", schedule="* * * * *", entrypoint="/bin/true"),
                )
            async with session_scope(engine) as session:
                (entry,) = [e for e in await list_schedules(session) if e.id == row.id]
                assert entry.created_at is not None and entry.created_at.tzinfo is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_invocation_reads_round_trip_through_the_api(migrated_db: str) -> None:
    """Story 3.3: the full per-invoke record is retrievable via the API.

    Real rows in real Postgres -> get_invocation/list_invocations -> the real
    FastAPI routes: every CAP-3 measure + both token fields match the row,
    lists are newest-first, limit-bounded and per-candybar.
    """
    import asyncio
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from candybar.api.app import create_app
    from candybar.registry import (
        TerminalInvocation,
        get_invocation,
        list_invocations,
        record_terminal_invocation,
    )

    t0 = datetime(2026, 10, 26, 3, 0, tzinfo=UTC)

    async def seed() -> tuple[object, object, list[object]]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                mine = await insert_candybar(
                    session,
                    CandybarManifest(
                        name="llm-digest", description="", tags=[], schedule="* * * * *",
                        entrypoint="/bin/true",
                    ),
                )
                other = await insert_candybar(
                    session,
                    CandybarManifest(
                        name="other-job", description="", tags=[], schedule="* * * * *",
                        entrypoint="/bin/true",
                    ),
                )
            ids = [uuid7() for _ in range(3)]
            async with session_scope(engine) as session:
                for i, inv in enumerate(ids):
                    await record_terminal_invocation(
                        session,
                        TerminalInvocation(
                            invocation_id=inv, candybar_id=mine.id, trigger="scheduled",
                            tick_id=f"tick:{29883000 + i}",
                            idempotency_key=f"candybar:{mine.id}:tick:tick:{29883000 + i}",
                            result="success" if i != 1 else "fail",
                            exit_code=0 if i != 1 else 1, runtime_ms=1000 + i,
                            error_message=None if i != 1 else "exit code 1",
                            stderr_tail=None, token_spend=1200 + i,
                            cost_usd=Decimal("0.03") if i != 2 else None,
                            started_at=t0 + timedelta(minutes=i),
                            finished_at=t0 + timedelta(minutes=i, seconds=2),
                            memory_peak_bytes=64 * 1024 * 1024 + i, cpu_load=0.5 + i,
                            gpu_load=0.25 * i,
                        ),
                    )
                await record_terminal_invocation(
                    session,
                    TerminalInvocation(
                        invocation_id=uuid7(), candybar_id=other.id, trigger="manual",
                        tick_id=None, idempotency_key=f"candybar:{other.id}:manual:x",
                        result="success", exit_code=0, runtime_ms=5, error_message=None,
                        stderr_tail=None, token_spend=0, cost_usd=None,
                        started_at=t0 + timedelta(hours=1), finished_at=t0 + timedelta(hours=1),
                    ),
                )
            async with session_scope(engine) as session:
                assert await get_invocation(session, uuid7()) is None
                assert (await get_invocation(session, ids[0])).token_spend == 1200
                newest_two = await list_invocations(session, mine.id, 2)
                assert [r.id for r in newest_two] == [ids[2], ids[1]]
                with pytest.raises(ValueError):
                    await list_invocations(session, mine.id, 0)
            return mine.id, other.id, ids
        finally:
            await engine.dispose()

    mine_id, other_id, ids = asyncio.run(seed())

    # The TestClient runs its own loop: a NullPool engine never shares a
    # connection across loops.
    api_engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(create_app(session_factory=lambda: session_scope(api_engine), sink=None))

    body = client.get(f"/invocations/{ids[1]}").json()
    assert body["candybarId"] == str(mine_id) and body["result"] == "fail"
    assert body["runtimeMs"] == 1001
    assert body["memoryPeakBytes"] == 64 * 1024 * 1024 + 1
    assert body["cpuLoad"] == 1.5 and body["gpuLoad"] == 0.25
    assert body["tokenSpend"] == 1201 and body["costUsd"] == 0.03
    assert body["errorMessage"] == "exit code 1" and body["exitCode"] == 1
    assert body["startedAt"] == (t0 + timedelta(minutes=1)).isoformat()
    assert client.get(f"/invocations/{ids[2]}").json()["costUsd"] is None
    assert client.get(f"/invocations/{uuid7()}").status_code == 404

    listed = client.get(f"/candybars/{mine_id}/invocations").json()["invocations"]
    assert [i["id"] for i in listed] == [str(x) for x in reversed(ids)]
    assert len(client.get(f"/candybars/{other_id}/invocations").json()["invocations"]) == 1
    assert client.get(f"/candybars/{uuid7()}/invocations").status_code == 404
