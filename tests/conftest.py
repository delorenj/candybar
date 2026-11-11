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
