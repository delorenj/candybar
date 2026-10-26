"""Refuse to consume while the registry's alembic head is not ours (AD-17, Story 7.2).

Lives in `registry/` because it reads the schema: AD-10 keeps every SQL and
alembic touch behind that one module. The executor role only consumes `SchemaGate`.

The host executor shares the registry only through the `registry/` module at
the repo checkout's git SHA, while the container (heartbeat/dispatcher/api)
owns migrations (its entrypoint runs `alembic upgrade head`). A host executor
on a different schema than the database would read and write rows it does not
understand, so it consumes nothing until the two agree. This is a guard, not a
lock: the deploy procedure (container up, then
`systemctl --user restart candybar-executor`) is what keeps them in step, and
the guard turns a botched order into a visible wait instead of corruption.

The expected head comes from the migration scripts of the checkout the process
runs from (`deploy/alembic`); the database head from `alembic_version`. The
guard is fail-closed: an unreadable script directory or a multi-head tree
raises, so the unit fails loudly instead of consuming blind. Set
`CANDYBAR_SCHEMA_GUARD=off` to skip it (tests, one-off dev runs).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from .store import session_scope

log = logging.getLogger(__name__)

SCHEMA_GUARD_ENV = "CANDYBAR_SCHEMA_GUARD"
ALEMBIC_DIR_ENV = "CANDYBAR_ALEMBIC_DIR"
# While blocked, look again often (a deploy is in flight); while open, look
# rarely (it is a cheap one-row read, but there is no point hammering).
BLOCKED_RECHECK_SECONDS = 5.0
OPEN_RECHECK_SECONDS = 30.0


class SchemaGuardError(RuntimeError):
    """The guard cannot determine this process's own migration head."""


def guard_enabled() -> bool:
    return os.environ.get(SCHEMA_GUARD_ENV, "").strip().lower() not in {"off", "0", "false"}


def default_alembic_dir() -> Path:
    override = os.environ.get(ALEMBIC_DIR_ENV)
    if override:
        return Path(override)
    # src/candybar/executor/schema_guard.py -> repo root is four levels up.
    return Path(__file__).resolve().parents[3] / "deploy" / "alembic"


def own_head(script_dir: Path | None = None) -> str:
    """The single head of this checkout's migration scripts."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    path = script_dir or default_alembic_dir()
    if not (path / "versions").is_dir():
        raise SchemaGuardError(
            f"alembic scripts not found at {path}: the host executor must run from the repo "
            f"checkout (or set {ALEMBIC_DIR_ENV}); {SCHEMA_GUARD_ENV}=off skips the guard"
        )
    # A bare Config: the repo's alembic.ini would also mutate sys.path.
    cfg = Config()
    cfg.set_main_option("script_location", str(path))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        raise SchemaGuardError(f"expected exactly one alembic head, found {sorted(heads)}")
    return heads[0]


async def database_head(engine: AsyncEngine) -> str | None:
    """The registry's stamped revision; None when never migrated."""
    try:
        async with session_scope(engine) as session:
            result = await session.execute(text("SELECT version_num FROM alembic_version"))
            rows = result.scalars().all()
    except SQLAlchemyError as exc:
        # `alembic_version` missing is "not migrated"; a dead database must
        # raise so the caller keeps waiting rather than mistaking it for one.
        if "alembic_version" in str(exc) and "does not exist" in str(exc):
            return None
        raise
    return rows[0] if len(rows) == 1 else None


class SchemaGate:
    """`open()` is True while the database head equals ours (cached, rechecked)."""

    def __init__(
        self,
        engine: AsyncEngine,
        expected_head: str,
        *,
        clock: Callable[[], float] = time.monotonic,
        blocked_recheck: float = BLOCKED_RECHECK_SECONDS,
        open_recheck: float = OPEN_RECHECK_SECONDS,
    ) -> None:
        self._engine = engine
        self.expected_head = expected_head
        self._clock = clock
        self._blocked_recheck = blocked_recheck
        self._open_recheck = open_recheck
        self._checked_at: float | None = None
        self._open = False
        self._announced: bool | None = None

    async def open(self) -> bool:
        now = self._clock()
        interval = self._open_recheck if self._open else self._blocked_recheck
        if self._checked_at is not None and now - self._checked_at < interval:
            return self._open
        self._checked_at = now
        try:
            found = await database_head(self._engine)
        except (SQLAlchemyError, OSError) as exc:
            # Fail closed on a database we cannot read, but keep trying.
            log.warning("schema guard cannot read the registry head (%s: %s)", type(exc).__name__, exc)
            self._open = False
            return False
        self._open = found == self.expected_head
        if self._announced is not self._open:
            self._announced = self._open
            if self._open:
                log.info("schema guard open: registry head %s matches this checkout", found)
            else:
                log.warning(
                    "schema guard CLOSED: registry head is %s but this checkout expects %s; "
                    "consuming nothing until they match (redeploy the container, then "
                    "restart candybar-executor)",
                    found,
                    self.expected_head,
                )
        return self._open


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
