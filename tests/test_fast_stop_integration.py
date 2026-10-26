"""Live proof of the Story 1.9 fast-stop contract: executor and dispatcher
roles stop well inside docker's 10s default SIGTERM grace.

Env-gated: CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (with DATABASE_URL).
The roles attach their PRODUCTION durables (candybar-executor-invoke,
candybar-dispatcher-ticks) — the point is the real pull-subscription drain
path. On a bare CI nats-server the production-named streams are created
first; on the live bus they already exist and are left untouched.

Without the unsubscribe-before-drain fix, nats-py's drain waits the full
~30s DrainTimeoutError on an idle pull subscription and this test fails.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import nats
import pytest
from nats.js.api import RetentionPolicy, StreamConfig
from nats.js.errors import NotFoundError

from candybar.dispatcher.role import run_dispatcher
from candybar.executor.role import run_executor

pytestmark = pytest.mark.skipif(
    os.environ.get("CANDYBAR_TEST_NATS") != "1" or os.environ.get("CANDYBAR_TEST_PG") != "1",
    reason="requires CANDYBAR_TEST_NATS=1 and CANDYBAR_TEST_PG=1 (and DATABASE_URL)",
)

# Docker's default stop grace; the roles must beat it with margin.
STOP_BUDGET_SECONDS = 8.0

STREAMS = (
    ("BLOODBANK_COMMANDS", ["bloodbank.cmd.>"], RetentionPolicy.WORK_QUEUE),
    ("BLOODBANK_EVENTS", ["bloodbank.evt.>"], RetentionPolicy.LIMITS),
)


async def _ensure_streams() -> None:
    nc = await nats.connect(os.environ.get("NATS_URL", "nats://localhost:4222"))
    try:
        js = nc.jetstream()
        for name, subjects, retention in STREAMS:
            try:
                await js.stream_info(name)
            except NotFoundError:
                await js.add_stream(
                    StreamConfig(name=name, subjects=subjects, retention=retention)
                )
    finally:
        await nc.close()


async def _measure_stop(role_run: Callable[[asyncio.Event], Awaitable[None]]) -> float:
    stop = asyncio.Event()
    task = asyncio.create_task(role_run(stop))
    # Let the role connect, attach its durable, and settle into fetch().
    await asyncio.sleep(3)
    started = time.monotonic()
    stop.set()
    await asyncio.wait_for(task, timeout=STOP_BUDGET_SECONDS + 5)
    return time.monotonic() - started


REPO_ROOT = Path(__file__).resolve().parent.parent


def _ensure_schema() -> None:
    """The throwaway DATABASE_URL may be empty (CI): upgrade to head first."""
    from alembic import command
    from alembic.config import Config

    from candybar.registry import normalize_dsn

    cfg = Config(str(REPO_ROOT / "deploy" / "alembic.ini"))
    cfg.set_main_option(
        "sqlalchemy.url",
        normalize_dsn(os.environ.get("DATABASE_URL", "postgresql://localhost:5432/candybar_test")),
    )
    cfg.set_main_option("script_location", str(REPO_ROOT / "deploy" / "alembic"))
    command.upgrade(cfg, "head")


def test_roles_stop_inside_docker_grace() -> None:
    _ensure_schema()
    asyncio.run(_ensure_streams())

    executor_elapsed = asyncio.run(_measure_stop(run_executor))
    assert executor_elapsed < STOP_BUDGET_SECONDS, (
        f"executor stop took {executor_elapsed:.1f}s — over docker's 10s grace"
    )

    dispatcher_elapsed = asyncio.run(_measure_stop(run_dispatcher))
    assert dispatcher_elapsed < STOP_BUDGET_SECONDS, (
        f"dispatcher stop took {dispatcher_elapsed:.1f}s — over docker's 10s grace"
    )
