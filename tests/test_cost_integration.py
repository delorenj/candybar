"""Live-Postgres proof of migration 0010 and the cost aggregate.

Env-gated like the other integration suites: CANDYBAR_TEST_PG=1 with
DATABASE_URL at a THROWAWAY database (`migrated_db` downgrades to base
afterwards). Proves, on real Postgres:

* 0010 backfills `cpu_seconds` for finished runs of exec/hermes/n8n candybars
  (cpu_load x runtime), leaves a systemd candybar's rows and every unmeasured
  (cpu_load 0) row NULL, and its downgrade drops what it added;
* `refresh_costs` totals: what counts as measured, the window cutoff, claim
  rows excluded, n8n never metered, one row per candybar (re-running upserts);
* the executor's terminal write refreshes the candybar's totals, and a failing
  refresh never un-records the run;
* the API carries a non-null `cost` on the list and detail, `/costs` ranks, and
  `POST /costs/refresh` rebuilds.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text

from candybar.registry import (
    CandybarManifest,
    TerminalInvocation,
    create_registry_engine,
    get_cost,
    insert_candybar,
    list_costs,
    record_terminal_invocation,
    refresh_costs,
    session_scope,
    uuid7,
)
from test_registry_integration import _dsn, migrated_db  # noqa: F401  (fixture)

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="set CANDYBAR_TEST_PG=1 (and DATABASE_URL) to run live-Postgres tests",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
GIB = 1024**3


def _alembic_cfg() -> Any:
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "deploy" / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", _dsn())
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    return cfg


def _manifest(name: str, runner: dict | None = None, schedule: str = "0 * * * *") -> CandybarManifest:
    if runner is None:
        return CandybarManifest(
            name=name, description="", tags=[], schedule=schedule, entrypoint="/bin/true"
        )
    return CandybarManifest(name=name, description="", tags=[], schedule=schedule, runner=runner)


def _terminal(
    candybar_id: UUID,
    *,
    started: datetime,
    runtime_ms: int = 10_000,
    result: str = "success",
    cpu_seconds: float | None = 2.0,
    cpu_load: float | None = None,
    memory: int = 100 * 1024 * 1024,
    gpu_load: float = 0.0,
    tokens: int = 0,
    usd: float | None = None,
) -> TerminalInvocation:
    from decimal import Decimal

    key = uuid4().hex
    return TerminalInvocation(
        invocation_id=uuid7(),
        candybar_id=candybar_id,
        trigger="manual",
        tick_id=None,
        idempotency_key=f"candybar:{candybar_id}:manual:{key}",
        result=result,
        exit_code=0 if result == "success" else 1,
        runtime_ms=runtime_ms,
        error_message=None,
        stderr_tail=None,
        token_spend=tokens,
        cost_usd=None if usd is None else Decimal(str(usd)),
        started_at=started,
        finished_at=started + timedelta(milliseconds=runtime_ms),
        memory_peak_bytes=memory,
        cpu_load=cpu_load if cpu_load is not None else ((cpu_seconds or 0) / (runtime_ms / 1000)),
        cpu_seconds=cpu_seconds,
        gpu_load=gpu_load,
    )


# --- migration 0010 -----------------------------------------------------------


def test_migration_0010_backfills_cpu_seconds_and_downgrades(migrated_db: str) -> None:  # noqa: F811
    from alembic import command

    cfg = _alembic_cfg()
    command.downgrade(cfg, "0009")
    ids = {"exec": uuid4(), "systemd": uuid4(), "n8n": uuid4()}
    with create_engine(migrated_db).begin() as conn:
        for kind, cid in ids.items():
            runner = {"kind": kind, "unit": "x.service"} if kind == "systemd" else (
                {"kind": "n8n", "webhook": "http://n8n/webhook/x"} if kind == "n8n" else None
            )
            conn.execute(
                text(
                    "INSERT INTO candybars (id, name, description, tags, schedule, entrypoint, "
                    "noise, total_invoked, total_successful, created_at, updated_at, runner) "
                    "VALUES (:id, :name, '', '[]', '* * * * *', '/bin/true', false, 0, 0, now(), "
                    "now(), CAST(:runner AS jsonb))"
                ),
                {
                    "id": cid,
                    "name": f"backfill-{kind}",
                    "runner": None if runner is None else json.dumps(runner),
                },
            )

            def run(label: str, *, result: str | None, runtime_ms: int | None, load: float,
                    cid: UUID = cid, kind: str = kind) -> None:
                conn.execute(
                    text(
                        "INSERT INTO invocations (id, candybar_id, trigger, idempotency_key, result, "
                        "runtime_ms, token_spend, memory_peak_bytes, cpu_load, gpu_load, started_at, "
                        "created_at) VALUES (:id, :cid, 'manual', :key, :result, :rt, 0, 0, :load, 0, "
                        "now(), now())"
                    ),
                    {"id": uuid4(), "cid": cid, "key": f"{kind}-{label}", "result": result,
                     "rt": runtime_ms, "load": load},
                )

            run("measured", result="success", runtime_ms=4000, load=0.5)  # 2 core-s
            run("idle", result="success", runtime_ms=4000, load=0.0)  # unmeasured
            run("claim", result=None, runtime_ms=None, load=0.0)  # still running
    command.upgrade(cfg, "head")
    with create_engine(migrated_db).connect() as conn:
        rows = {
            (r[0], r[1]): r[2]
            for r in conn.execute(
                text(
                    "SELECT coalesce(c.runner ->> 'kind', 'exec'), split_part(i.idempotency_key, '-', 2), "
                    "i.cpu_seconds FROM invocations i JOIN candybars c ON c.id = i.candybar_id"
                )
            )
        }
    assert rows[("exec", "measured")] == pytest.approx(2.0)
    assert rows[("n8n", "measured")] == pytest.approx(2.0)  # the aggregate, not the migration, drops n8n
    assert rows[("systemd", "measured")] is None  # that runner's cpu_load was wrong
    for kind in ids:
        assert rows[(kind, "idle")] is None and rows[(kind, "claim")] is None
    # The table 0010 adds is there, and the downgrade takes both additions away again.
    with create_engine(migrated_db).connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM candybar_costs")).scalar_one() == 0
    command.downgrade(cfg, "0009")
    with create_engine(migrated_db).connect() as conn:
        cols = {
            r[0]
            for r in conn.execute(
                text("SELECT column_name FROM information_schema.columns WHERE table_name = 'invocations'")
            )
        }
        tables = {r[0] for r in conn.execute(text("SELECT tablename FROM pg_tables"))}
    assert "cpu_seconds" not in cols and "candybar_costs" not in tables
    command.upgrade(cfg, "head")


def test_cpu_seconds_cannot_be_negative(migrated_db: str) -> None:  # noqa: F811
    from sqlalchemy.exc import IntegrityError

    cid = uuid4()
    with create_engine(migrated_db).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO candybars (id, name, description, tags, schedule, entrypoint, noise, "
                "total_invoked, total_successful, created_at, updated_at) VALUES (:id, 'neg', '', "
                "'[]', '* * * * *', '/bin/true', false, 0, 0, now(), now())"
            ),
            {"id": cid},
        )
    with pytest.raises(IntegrityError), create_engine(migrated_db).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO invocations (id, candybar_id, trigger, idempotency_key, token_spend, "
                "memory_peak_bytes, cpu_load, gpu_load, cpu_seconds, started_at, created_at) VALUES "
                "(:id, :cid, 'manual', 'neg', 0, 0, 0, 0, -1, now(), now())"
            ),
            {"id": uuid4(), "cid": cid},
        )


# --- the aggregate -------------------------------------------------------------


def test_refresh_costs_totals_what_was_measured_inside_the_window(migrated_db: str) -> None:  # noqa: F811
    async def scenario() -> dict[str, Any]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                cand = await insert_candybar(session, _manifest("costed"))
                legacy_sd = await insert_candybar(
                    session, _manifest("legacy-systemd", {"kind": "systemd", "unit": "x.service"})
                )
                n8n = await insert_candybar(
                    session,
                    _manifest("flow", {"kind": "n8n", "webhook": "http://n8n.test/webhook/x"}),
                )
                quiet = await insert_candybar(session, _manifest("quiet"))
            now = datetime.now(UTC)
            async with session_scope(engine) as session:
                for i in range(4):  # 4 metered runs: 2 core-s, 100 MB, 10 s, 1000 tokens each
                    await record_terminal_invocation(
                        session, _terminal(cand.id, started=now - timedelta(hours=i), tokens=1000)
                    )
                await record_terminal_invocation(  # a failure, GPU busy, USD reported, no tokens
                    session,
                    _terminal(cand.id, started=now - timedelta(hours=5), result="fail",
                              cpu_seconds=6.0, gpu_load=0.5, usd=0.25),
                )
                await record_terminal_invocation(  # not metered (scope failed): not in CPU/memory
                    session,
                    _terminal(cand.id, started=now - timedelta(hours=6), cpu_seconds=None, memory=0),
                )
                await record_terminal_invocation(  # older than the window: ignored
                    session,
                    _terminal(cand.id, started=now - timedelta(days=9), cpu_seconds=999.0),
                )
                await record_terminal_invocation(  # systemd row from before the fix: no cpu_seconds
                    session,
                    _terminal(legacy_sd.id, started=now, cpu_seconds=None, cpu_load=500.0),
                )
                await record_terminal_invocation(  # n8n: the webhook client's CPU is not the job's
                    session, _terminal(n8n.id, started=now, tokens=50)
                )
            async with session_scope(engine) as session:
                written = await refresh_costs(session, window_days=7)
            async with session_scope(engine) as session:
                rows = await list_costs(session)
                again = await get_cost(session, cand.id)
                assert again is not None
            return {
                "written": written,
                "rows": rows,
                "ids": {"cand": cand.id, "sd": legacy_sd.id, "n8n": n8n.id, "quiet": quiet.id},
                "window": again.window_days,
            }
        finally:
            await engine.dispose()

    out = asyncio.run(scenario())
    assert out["written"] == 4 and out["window"] == 7  # every candybar gets a row, runs or not
    t = out["rows"][out["ids"]["cand"]].totals
    assert t["runs"] == 6 and t["failedRuns"] == 1  # the 9-day-old run is outside
    assert t["cpuRuns"] == 5 and t["cpuSeconds"] == pytest.approx(4 * 2.0 + 6.0)
    assert t["gpuSeconds"] == pytest.approx(0.5 * 10)  # only the one run that used the GPU
    assert t["memRuns"] == 5 and t["memPeakBytesMax"] == 100 * 1024 * 1024
    assert t["memGibSeconds"] == pytest.approx(5 * (100 / 1024) * 10)
    assert t["tokenSum"] == 4000 and t["tokenRuns"] == 4
    assert t["usdSum"] == pytest.approx(0.25) and t["usdRuns"] == 1
    assert t["runtimeMs"] == 6 * 10_000
    sd = out["rows"][out["ids"]["sd"]].totals
    assert sd["runs"] == 1 and sd["cpuRuns"] == 0 and sd["cpuSeconds"] == 0  # unmeasured, not 500 cores
    flow = out["rows"][out["ids"]["n8n"]].totals
    assert flow["runs"] == 1 and flow["cpuRuns"] == 0 and flow["memRuns"] == 0
    assert flow["tokenSum"] == 50 and flow["tokenRuns"] == 1  # self-reported tokens do count
    quiet = out["rows"][out["ids"]["quiet"]].totals
    assert quiet["runs"] == 0 and quiet["tokenRuns"] == 0 and quiet["firstRunAt"] is None


def test_a_claim_row_is_not_a_run_and_refresh_is_an_upsert(migrated_db: str) -> None:  # noqa: F811
    from candybar.registry import InvocationClaim, claim_invocation

    async def scenario() -> tuple[int, int, int]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                cand = await insert_candybar(session, _manifest("claimed"))
                other = await insert_candybar(session, _manifest("bystander"))
            now = datetime.now(UTC)
            async with session_scope(engine) as session:
                await claim_invocation(
                    session,
                    InvocationClaim(
                        invocation_id=uuid7(), candybar_id=cand.id, trigger="manual", tick_id=None,
                        idempotency_key=f"candybar:{cand.id}:manual:inflight", started_at=now,
                    ),
                )
            async with session_scope(engine) as session:
                await refresh_costs(session, cand.id)  # one candybar only
                first = await list_costs(session)
            async with session_scope(engine) as session:
                await record_terminal_invocation(session, _terminal(cand.id, started=now))
                await refresh_costs(session, cand.id)
                await refresh_costs(session, cand.id)  # twice: still one row
            async with session_scope(engine) as session:
                second = await list_costs(session)
            return (
                first[cand.id].totals["runs"] + (100 if other.id in first else 0),
                second[cand.id].totals["runs"],
                len(second),
            )
        finally:
            await engine.dispose()

    claimed_only, after_terminal, rows = asyncio.run(scenario())
    assert claimed_only == 0  # the in-flight claim is not a run, and the bystander was not touched
    assert after_terminal == 1 and rows == 1


def test_the_terminal_write_refreshes_the_cost_and_a_refresh_failure_does_not_undo_it(
    migrated_db: str, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    from candybar.executor import role

    async def scenario() -> tuple[bool, int, bool, int, bool]:
        engine = create_registry_engine(migrated_db)
        try:
            async with session_scope(engine) as session:
                cand = await insert_candybar(session, _manifest("hooked"))
            now = datetime.now(UTC)
            rec = _terminal(cand.id, started=now)
            recorded = await role.record_and_refresh_cost(engine, rec)
            async with session_scope(engine) as session:
                row = await get_cost(session, cand.id)
            assert row is not None
            duplicate = await role.record_and_refresh_cost(engine, rec)  # same key again

            async def boom(*_a: Any, **_k: Any) -> int:
                raise RuntimeError("cost table is locked")

            monkeypatch.setattr(role, "refresh_costs", boom)
            survived = await role.record_and_refresh_cost(engine, _terminal(cand.id, started=now))
            async with session_scope(engine) as session:
                from candybar.registry import list_invocations

                runs = await list_invocations(session, cand.id, 10)
                stale = await get_cost(session, cand.id)
            assert stale is not None
            return recorded, row.totals["runs"], duplicate, len(runs), survived and stale.totals["runs"] == 1
        finally:
            await engine.dispose()

    recorded, runs_in_cost, duplicate, stored, survived = asyncio.run(scenario())
    assert recorded is True and runs_in_cost == 1  # the aggregate followed the terminal write
    assert duplicate is False  # a same-key race records nothing
    assert stored == 2 and survived  # the failed refresh left the second run recorded, aggregate stale


# --- the API on real Postgres ---------------------------------------------------


def test_the_api_carries_a_cost_everywhere_and_ranks_them(migrated_db: str) -> None:  # noqa: F811
    from fastapi.testclient import TestClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    from candybar.api.app import create_app
    from test_api_definitions import FakeSink

    engine = create_async_engine(migrated_db, poolclass=NullPool)
    client = TestClient(
        create_app(session_factory=lambda: session_scope(engine), sink=FakeSink())
    )
    made = client.post(
        "/candybars",
        json={"name": "api-cost", "schedule": "*/5 * * * *", "entrypoint": "/bin/true"},
    )
    assert made.status_code == 201
    created = made.json()
    assert created["cost"]["tier"] == "unmeasured" and created["cost"]["computedAt"] is None
    cid = UUID(created["id"])

    async def seed() -> None:
        now = datetime.now(UTC)
        async with session_scope(engine) as session:
            for i in range(10):  # 30 core-s a run at 288 runs/day: far past red
                await record_terminal_invocation(
                    session,
                    _terminal(cid, started=now - timedelta(minutes=5 * i), cpu_seconds=30.0,
                              tokens=100 if i == 0 else 0),
                )
        async with session_scope(engine) as session:
            await refresh_costs(session, cid)

    asyncio.run(seed())
    listed = {c["name"]: c for c in client.get("/candybars").json()}
    cost = listed["api-cost"]["cost"]
    assert cost["tier"] == "red" and cost["driver"] == "cpu" and cost["runs"] == 10
    assert cost["perRun"]["cpuS"] == 30.0 and cost["firesPerDay"] == 288.0
    assert cost["perDay"]["cpuS"] == 8640.0
    assert cost["measuredRuns"]["tokens"] == 1  # 1 of 10 runs reported tokens, and it says so
    assert any("1 of 10 runs reported tokens" in n for n in cost["notes"])
    detail = client.get(f"/candybars/{cid}").json()
    assert detail["cost"]["tier"] == "red" and detail["totalInvoked"] == 10
    patched = client.patch(f"/candybars/{cid}", json={"description": "x"}).json()
    assert patched["cost"]["tier"] == "red"  # every single-definition response carries it
    one = client.get(f"/candybars/{cid}/cost").json()
    assert one["cost"]["tier"] == "red" and one["configError"] is None
    by_label = {v["label"]: v for v in one["whatIf"]}
    assert by_label["daily"]["tier"] == "green"  # 30 core-s a day
    assert by_label["hourly"]["tier"] == "yellow"  # 720 core-s a day
    assert one["thresholds"]["cpu"] == {"yellow": 300.0, "red": 1800.0, "unit": "core-s/day"}
    ranking = client.get("/costs").json()
    assert ranking["windowDays"] == 7 and ranking["candybars"][0]["name"] == "api-cost"
    refreshed = client.post("/costs/refresh").json()
    assert refreshed["refreshed"] >= 1 and refreshed["windowDays"] == 7
    assert client.post(f"/costs/refresh?candybarId={cid}").json()["refreshed"] == 1
    missing = client.post(f"/costs/refresh?candybarId={uuid4()}")
    assert missing.status_code == 404
    assert client.get(f"/candybars/{uuid4()}/cost").status_code == 404
