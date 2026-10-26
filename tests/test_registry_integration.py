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


def test_definitions_crud_round_trip_through_the_api(migrated_db: str) -> None:
    """Story 5.1: register, list, get, patch on real Postgres via the real routes.

    Every response is checked key-for-key against the contract file's field
    lists; the counters on GET /candybars/{id} are the registry's; a PATCH
    publishes exactly one `updated` fact and the row change commits; a rename
    onto a taken name is 409 with nothing published or written.
    """
    import asyncio
    import json
    from datetime import UTC, datetime
    from uuid import UUID

    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from candybar.api.app import create_app
    from candybar.events.parse import parse_envelope
    from candybar.registry import (
        TerminalInvocation,
        get_candybar,
        record_terminal_invocation,
    )
    from contract_shapes import LIST_KEYS, SINGLE_KEYS, assert_contract_shape

    class Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        async def publish(self, event: object) -> None:
            self.events.append(parse_envelope(json.loads(event.model_dump_json())))

    sink = Sink()
    api_engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(create_app(session_factory=lambda: session_scope(api_engine), sink=sink))

    assert client.get("/candybars").json() == []
    first = client.post(
        "/candybars",
        json={"name": "crud-a", "description": "first", "tags": ["x"],
              "schedule": "*/5 * * * *", "entrypoint": "/bin/true"},
    )
    assert first.status_code == 201
    assert_contract_shape(first.json(), SINGLE_KEYS)
    second = client.post(
        "/candybars",
        json={"name": "crud-b", "schedule": "0 * * * *", "entrypoint": "/bin/true",
              "noise": True},
    )
    a_id, b_id = first.json()["id"], second.json()["id"]

    listed = client.get("/candybars").json()
    assert [item["id"] for item in listed] == [a_id, b_id]
    for item in listed:
        assert_contract_shape(item, LIST_KEYS)
    assert listed[1]["noise"] is True

    async def record_one() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                now = datetime.now(UTC)
                await record_terminal_invocation(
                    session,
                    TerminalInvocation(
                        invocation_id=uuid7(), candybar_id=UUID(a_id), trigger="manual",
                        tick_id=None, idempotency_key=f"candybar:{a_id}:manual:crud",
                        result="success", exit_code=0, runtime_ms=3, error_message=None,
                        stderr_tail=None, token_spend=0, cost_usd=None,
                        started_at=now, finished_at=now,
                    ),
                )
        finally:
            await engine.dispose()

    asyncio.run(record_one())
    detail = client.get(f"/candybars/{a_id}").json()
    assert_contract_shape(detail, SINGLE_KEYS)
    assert detail["totalInvoked"] == 1 and detail["totalSuccessful"] == 1
    assert detail["createdAt"] == first.json()["createdAt"]

    sink.events.clear()
    patched = client.patch(f"/candybars/{a_id}", json={"schedule": "0 3 * * *", "tags": ["x"]})
    assert patched.status_code == 200
    assert_contract_shape(patched.json(), SINGLE_KEYS)
    assert patched.json()["schedule"] == "0 3 * * *" and patched.json()["totalInvoked"] == 1
    assert len(sink.events) == 1
    event = sink.events[0]
    assert event.type == "bloodbank.cron.candybar.updated"
    assert event.data.changes == {"schedule": "0 3 * * *"}
    assert client.get(f"/candybars/{a_id}").json()["schedule"] == "0 3 * * *"

    async def stored_row() -> tuple[str, str, datetime, datetime]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                row = await get_candybar(session, UUID(a_id))
                return row.name, row.schedule, row.created_at, row.updated_at
        finally:
            await engine.dispose()

    name, schedule, created_at, updated_at = asyncio.run(stored_row())
    assert (name, schedule) == ("crud-a", "0 3 * * *") and updated_at > created_at

    sink.events.clear()
    clash = client.patch(f"/candybars/{a_id}", json={"name": "crud-b"})
    assert clash.status_code == 409 and sink.events == []
    assert asyncio.run(stored_row())[0] == "crud-a"

    renamed = client.patch(f"/candybars/{a_id}", json={"name": "crud-c"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "crud-c"
    assert sink.events[0].data.name == "crud-c"
    assert client.patch(f"/candybars/{uuid7()}", json={"noise": True}).status_code == 404


def test_definition_update_lock_serializes_and_maps_rename_clash(migrated_db: str) -> None:
    """Story 5.1 store primitives on real Postgres.

    `lock_candybar` holds the row FOR UPDATE: a second locker waits until the
    first commits and then sees the committed values (so a PATCH never diffs
    against a stale before-image). `update_candybar` maps a rename onto a
    taken name to DuplicateNameError via the real unique constraint.
    """
    import asyncio

    from candybar.registry import lock_candybar, update_candybar

    async def scenario() -> None:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                a = await insert_candybar(
                    session, CandybarManifest(name="lock-a", schedule="* * * * *",
                                              entrypoint="/bin/true"),
                )
                await insert_candybar(
                    session, CandybarManifest(name="lock-b", schedule="* * * * *",
                                              entrypoint="/bin/true"),
                )

            first_locked = asyncio.Event()
            release = asyncio.Event()

            async def holder() -> None:
                async with session_scope(engine) as session:
                    row = await lock_candybar(session, a.id)
                    first_locked.set()
                    await release.wait()
                    await update_candybar(session, row, {"schedule": "0 * * * *"})

            async def second() -> str:
                async with session_scope(engine) as session:
                    row = await lock_candybar(session, a.id)
                    return row.schedule

            hold = asyncio.create_task(holder())
            await first_locked.wait()
            waiter = asyncio.create_task(second())
            done, _ = await asyncio.wait({waiter}, timeout=0.5)
            assert not done, "second lock_candybar did not wait for the first transaction"
            release.set()
            await hold
            assert await asyncio.wait_for(waiter, timeout=5) == "0 * * * *"

            with pytest.raises(DuplicateNameError):
                async with session_scope(engine) as session:
                    row = await lock_candybar(session, a.id)
                    await update_candybar(session, row, {"name": "lock-b"})
            async with session_scope(engine) as session:
                assert (await lock_candybar(session, a.id)).name == "lock-a"
                assert await lock_candybar(session, uuid7()) is None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_timezone_column_default_round_trip_and_migration_keeps_old_rows_utc() -> None:
    """Story 7.1: migration 0005 adds `timezone` (NOT NULL, default UTC); a row that
    predates it reads as UTC after upgrade, and a registered timezone round-trips."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    from candybar.registry import get_candybar
    from candybar.registry.store import list_schedules

    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "0004")
    try:
        with create_engine(_sync_url()).begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO candybars (id, name, description, tags, schedule, entrypoint, "
                    "noise, total_invoked, total_successful, created_at, updated_at) VALUES "
                    "(:id, 'pre-epic-7', '', '[]', '0 * * * *', '/bin/true', false, 0, 0, now(), now())"
                ),
                {"id": str(uuid7())},
            )
        command.upgrade(cfg, "head")

        async def scenario() -> list[tuple[str, str]]:
            engine = create_registry_engine(_sync_url())
            try:
                async with session_scope(engine) as session:
                    ny = await insert_candybar(
                        session,
                        CandybarManifest(
                            name="ifvault", schedule="40 1 * * *", entrypoint="/bin/true",
                            timezone="America/New_York",
                        ),
                    )
                    plain = await insert_candybar(
                        session,
                        CandybarManifest(name="plain", schedule="0 * * * *", entrypoint="/bin/true"),
                    )
                async with session_scope(engine) as session:
                    assert (await get_candybar(session, ny.id)).timezone == "America/New_York"
                    assert (await get_candybar(session, plain.id)).timezone == "UTC"
                    entries = await list_schedules(session)
                return sorted((e.name, e.timezone) for e in entries)
            finally:
                await engine.dispose()

        assert asyncio.run(scenario()) == [
            ("ifvault", "America/New_York"),
            ("plain", "UTC"),
            ("pre-epic-7", "UTC"),
        ]
        with create_engine(_sync_url()).connect() as conn:
            col = conn.execute(
                text(
                    "SELECT is_nullable, column_default FROM information_schema.columns "
                    "WHERE table_name = 'candybars' AND column_name = 'timezone'"
                )
            ).one()
            assert col[0] == "NO" and "UTC" in col[1]
    finally:
        command.downgrade(cfg, "base")


def test_runner_column_is_nullable_jsonb_and_old_rows_keep_running_their_entrypoint() -> None:
    """Story 7.3: migration 0006 adds `candybars.runner` (jsonb, NULLABLE). A row that
    predates it has SQL NULL and resolves to its entrypoint; a runner round-trips and
    resolves to the runner dict; PATCH-style updates switch between the two shapes."""
    import asyncio

    from alembic import command
    from alembic.config import Config

    from candybar.registry import get_run_target, lock_candybar, update_candybar
    from candybar.runners import runner_label

    cfg = Config(str(ALEMBC_INI))
    cfg.set_main_option("sqlalchemy.url", _sync_url())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "0005")
    old_id = uuid7()
    try:
        with create_engine(_sync_url()).begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO candybars (id, name, description, tags, schedule, entrypoint, "
                    "noise, total_invoked, total_successful, created_at, updated_at) VALUES "
                    "(:id, 'pre-runner', '', '[]', '0 * * * *', '/bin/true', false, 0, 0, now(), now())"
                ),
                {"id": str(old_id)},
            )
        command.upgrade(cfg, "head")
        systemd = {"kind": "systemd", "unit": "cache-prune.service"}
        exec_runner = {"kind": "exec", "argv": ["/usr/bin/job", "--x"], "env": {"A": "1"}}

        async def scenario() -> None:
            engine = create_registry_engine(_sync_url())
            try:
                async with session_scope(engine) as session:
                    unit = await insert_candybar(
                        session,
                        CandybarManifest(name="prune", schedule="0 3 * * *", runner=systemd),
                    )
                    job = await insert_candybar(
                        session,
                        CandybarManifest(name="job", schedule="* * * * *", runner=exec_runner),
                    )
                async with session_scope(engine) as session:
                    assert await get_run_target(session, old_id) == "/bin/true"
                    assert await get_run_target(session, unit.id) == systemd
                    assert await get_run_target(session, job.id) == {
                        **exec_runner,
                    }
                    assert await get_run_target(session, uuid7()) is None
                # The NOT NULL entrypoint column holds the runner's label.
                with create_engine(_sync_url()).connect() as conn:
                    rows = dict(
                        conn.execute(
                            text("SELECT name, entrypoint FROM candybars ORDER BY name")
                        ).all()
                    )
                    assert rows["prune"] == "systemd:cache-prune.service"
                    assert rows["job"] == "/usr/bin/job"
                    # SQL NULL, not JSON null, for a bare-entrypoint row.
                    assert conn.execute(
                        text("SELECT runner IS NULL FROM candybars WHERE name = 'pre-runner'")
                    ).scalar() is True
                # An entrypoint replaces the runner; a runner replaces the entrypoint.
                async with session_scope(engine) as session:
                    row = await lock_candybar(session, unit.id)
                    await update_candybar(session, row, {"entrypoint": "/bin/true", "runner": None})
                async with session_scope(engine) as session:
                    assert await get_run_target(session, unit.id) == "/bin/true"
                async with session_scope(engine) as session:
                    row = await lock_candybar(session, old_id)
                    await update_candybar(session, row, {"runner": exec_runner})
                async with session_scope(engine) as session:
                    assert await get_run_target(session, old_id) == exec_runner
                    row = await lock_candybar(session, old_id)
                    assert row.entrypoint == runner_label(exec_runner)
            finally:
                await engine.dispose()

        asyncio.run(scenario())
        with create_engine(_sync_url()).connect() as conn:
            col = conn.execute(
                text(
                    "SELECT is_nullable, data_type FROM information_schema.columns "
                    "WHERE table_name = 'candybars' AND column_name = 'runner'"
                )
            ).one()
            assert col == ("YES", "jsonb")
    finally:
        command.downgrade(cfg, "base")
