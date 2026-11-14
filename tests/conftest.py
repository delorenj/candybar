"""Suite-wide test defaults.

GPU metering (Story 3.2) samples the host's real `nvidia-smi pmon` by default.
The unit suite must be deterministic on GPU and GPU-less hosts alike, so the
process-default sampler is off unless the environment says otherwise.
tests/test_gpu.py drives its own samplers, using a fake pmon or the real one
behind CANDYBAR_TEST_GPU=1.
"""

from __future__ import annotations

import os

os.environ.setdefault("CANDYBAR_NVIDIA_SMI", "")
# Story 7.2 (AD-17): the host executor serves /metrics on a real port and
# refuses to consume on an alembic-head mismatch. Neither belongs in the unit
# suite by default: no port contention, no database to compare against.
# tests/test_executor_host.py drives both explicitly.
os.environ.setdefault("CANDYBAR_EXECUTOR_METRICS_PORT", "")
os.environ.setdefault("CANDYBAR_SCHEMA_GUARD", "off")
# The steward's checkout lookup scans the real ~/docker, ~/.agents, ... by
# default; the unit suite looks only where a test points it (empty: none).
os.environ.setdefault("CANDYBAR_CHECKOUT_ROOTS", "")


# The API's cost reads (`list_costs`, `get_cost`, `refresh_costs`) are SQL, and
# most API unit tests drive the routes over an in-memory registry whose
# "session" is a stand-in object. Against a stand-in there is no cost table:
# every candybar is simply not aggregated yet. Live-Postgres tests hand the
# routes a real AsyncSession and get the real functions.
import pytest


@pytest.fixture(autouse=True)
def _cost_store_without_a_database(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    import candybar.api.app as app_module

    real = {name: getattr(app_module, name) for name in ("list_costs", "get_cost", "refresh_costs")}

    async def list_costs(session):
        return await real["list_costs"](session) if isinstance(session, AsyncSession) else {}

    async def get_cost(session, candybar_id):
        if isinstance(session, AsyncSession):
            return await real["get_cost"](session, candybar_id)
        return None

    async def refresh_costs(session, candybar_id=None, **kwargs):
        if isinstance(session, AsyncSession):
            return await real["refresh_costs"](session, candybar_id, **kwargs)
        return 0

    monkeypatch.setattr(app_module, "list_costs", list_costs)
    monkeypatch.setattr(app_module, "get_cost", get_cost)
    monkeypatch.setattr(app_module, "refresh_costs", refresh_costs)


ROLLBACK_TIMER = "systemd:user:newapi-allowances.timer"


@pytest.fixture
def rolled_back_clock(tmp_path, monkeypatch):
    """A self-contained rolled-back migration for the protected rollback exception.

    The scenarios that certify a conditional rollback exception must not read
    the live canonical ledger or policy (CAND-53): systemd:newapi-allowances was
    re-cut-over and decommissioned on 2026-11-12, which turned every one of them
    red. This writes a tmp ledger holding one synthetic rolled-back entry with
    the protected binding's exact identities, points CANONICAL_LEDGER at it, and
    writes a tmp policy carrying the rollback exception.
    """
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from candybar import scheduling_policy as policy
    from candybar.migration.ledger import Ledger, LedgerEntry, save_ledger

    binding = policy.ROLLBACK_BINDINGS[ROLLBACK_TIMER]
    entry = LedgerEntry.model_validate({
        "id": binding["ledger_id"],
        "wave": "fixture",
        "candybar": {"name": "newapi-allowances", "schedule": "*/5 * * * *",
                     "runner": {"kind": "systemd", "unit": "newapi-allowances.service"}},
        "native": {"kind": "systemd-timer", "schedule": "OnUnitActiveSec=5m",
                   "unit": "newapi-allowances.timer"},
        "source_of_truth": {"repo": "fixture/repo", "path": "newapi-allowances.timer"},
        "status": "rolled-back",
        "candybar_id": binding["candybar_id"],
        "disable_pending": False,
    })
    ledger = tmp_path / "rollback-fixture" / "ledger.yaml"
    ledger.parent.mkdir()
    save_ledger(ledger, Ledger(jobs=[entry]))
    monkeypatch.setattr(policy, "CANONICAL_LEDGER", ledger)
    canonical = Path(__file__).parents[1] / "deploy/scheduling-policy.json"
    raw = json.loads(canonical.read_text())
    raw["exceptions"] = [e for e in raw["exceptions"] if e["id"] != ROLLBACK_TIMER] + [{
        "id": ROLLBACK_TIMER, "owner": "fixture rollback owner",
        "reason": "Fixture conditional rollback exception.", **binding,
    }]
    path = tmp_path / "rollback-fixture" / "scheduling-policy.json"
    path.write_text(json.dumps(raw))
    return SimpleNamespace(policy=path, ledger=ledger, entry=entry)
