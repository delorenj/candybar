"""Registry store: the daemon's sole Postgres access layer (AD-10).

Every read/write of the `candybar` database goes through this module (or the
alembic migrations under deploy/). The sole-writer guard test pins it: no
other module under src/candybar/ may import sqlalchemy or psycopg.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from .ids import uuid7
from .manifest import CandybarManifest
from .models import Candybar, Invocation

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost:5432/candybar"


class DuplicateNameError(Exception):
    """A candybar with this name already exists (unique constraint hit).

    Registry-level error so callers never import sqlalchemy themselves — the
    sole-writer guard forbids DB imports outside this package.
    """


class DuplicateInvocationError(Exception):
    """The idempotency_key is already recorded (unique constraint hit).

    Raised out of the terminal write so the caller's transaction rolls back;
    the redelivery path then reloads the recorded row and republishes.
    """


def normalize_dsn(url: str) -> str:
    """Force the psycopg (v3) driver: bare `postgresql://` would default to psycopg2."""
    for bare in ("postgresql://", "postgres://"):
        if url.startswith(bare):
            return "postgresql+psycopg://" + url[len(bare) :]
    return url


def database_url() -> str:
    """Registry DSN from the ecosystem-convention `DATABASE_URL` env var."""
    return normalize_dsn(os.environ.get(DATABASE_URL_ENV, DEFAULT_DATABASE_URL))


def create_registry_engine(url: str | None = None) -> AsyncEngine:
    return create_async_engine(normalize_dsn(url) if url else database_url(), pool_pre_ping=True)


@asynccontextmanager
async def session_scope(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(engine, expire_on_commit=False)
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def name_taken(session: AsyncSession, name: str) -> bool:
    result = await session.execute(select(Candybar.id).where(Candybar.name == name))
    return result.scalar_one_or_none() is not None


@dataclass(frozen=True)
class ScheduleEntry:
    id: UUID
    name: str
    schedule: str


async def list_schedules(session: AsyncSession) -> list[ScheduleEntry]:
    """Read the current registry inventory without exposing ORM to dispatchers."""
    result = await session.execute(select(Candybar.id, Candybar.name, Candybar.schedule))
    return [ScheduleEntry(id=id, name=name, schedule=schedule) for id, name, schedule in result]


async def insert_candybar(
    session: AsyncSession, manifest: CandybarManifest, *, candybar_id: UUID | None = None
) -> Candybar:
    """Insert the registry row for a validated manifest. Caller commits (session_scope).

    `candybar_id` is supplied by callers that already referenced the id (e.g. the
    API publishes `candybar.created` carrying it BEFORE the row commits).
    """
    now = datetime.now(UTC)
    row = Candybar(
        id=candybar_id or uuid7(),
        name=manifest.name,
        description=manifest.description,
        tags=list(manifest.tags),
        schedule=manifest.schedule,
        entrypoint=manifest.entrypoint,
        noise=manifest.noise,
        total_invoked=0,
        total_successful=0,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    try:
        await session.flush()  # surface unique violations here, inside the transaction
    except IntegrityError as exc:
        if "candybars_name_key" in str(exc):
            raise DuplicateNameError(manifest.name) from exc
        raise  # a different constraint fired — never misreport it as a name clash
    return row


async def get_entrypoint(session: AsyncSession, candybar_id: UUID) -> str | None:
    """The registered entrypoint for a candybar id, or None when unknown."""
    result = await session.execute(select(Candybar.entrypoint).where(Candybar.id == candybar_id))
    return result.scalar_one_or_none()


async def invocation_by_key(session: AsyncSession, idempotency_key: str) -> Invocation | None:
    """The recorded invocation for an idempotency key (the executor's dedupe read)."""
    result = await session.execute(
        select(Invocation).where(Invocation.idempotency_key == idempotency_key)
    )
    return result.scalar_one_or_none()


@dataclass(frozen=True)
class TerminalInvocation:
    """Everything the executor learned about a finished (or unstartable) run."""

    invocation_id: UUID
    candybar_id: UUID
    trigger: str
    tick_id: str | None
    idempotency_key: str
    result: str  # "success" | "fail"
    exit_code: int | None
    runtime_ms: int | None
    error_message: str | None
    stderr_tail: str | None
    token_spend: int
    cost_usd: Decimal | None
    started_at: datetime
    finished_at: datetime


async def record_terminal_invocation(session: AsyncSession, record: TerminalInvocation) -> None:
    """Insert the terminal row AND bump counters in the caller's transaction (AD-10).

    Raises DuplicateInvocationError when the idempotency key is already
    recorded: the caller's session_scope rolls back, and the redelivery path
    republishes from the recorded row instead of executing twice.
    """
    now = datetime.now(UTC)
    row = Invocation(
        id=record.invocation_id,
        candybar_id=record.candybar_id,
        trigger=record.trigger,
        tick_id=record.tick_id,
        idempotency_key=record.idempotency_key,
        result=record.result,
        exit_code=record.exit_code,
        runtime_ms=record.runtime_ms,
        error_message=record.error_message,
        stderr_tail=record.stderr_tail,
        token_spend=record.token_spend,
        cost_usd=record.cost_usd,
        started_at=record.started_at,
        finished_at=record.finished_at,
        created_at=now,
    )
    session.add(row)
    candybar = await session.get(Candybar, record.candybar_id)
    if candybar is None:
        raise KeyError(f"unknown candybar: {record.candybar_id}")
    candybar.total_invoked += 1
    if record.result == "success":
        candybar.total_successful += 1
    candybar.updated_at = now
    try:
        await session.flush()
    except IntegrityError as exc:
        if "invocations_idempotency_key_key" in str(exc):
            raise DuplicateInvocationError(record.idempotency_key) from exc
        raise
