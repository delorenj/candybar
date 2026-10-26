"""Registry store: the daemon's sole Postgres access layer (AD-10).

Every read/write of the `candybar` database goes through this module (or the
alembic migrations under deploy/). The sole-writer guard test pins it: no
other module under src/candybar/ may import sqlalchemy or psycopg.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from .ids import uuid7
from .manifest import CandybarManifest
from .models import Candybar

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost:5432/candybar"


class DuplicateNameError(Exception):
    """A candybar with this name already exists (unique constraint hit).

    Registry-level error so callers never import sqlalchemy themselves — the
    sole-writer guard forbids DB imports outside this package.
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
