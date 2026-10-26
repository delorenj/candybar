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
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from .ids import uuid7
from .manifest import CandybarManifest
from .models import (
    SKIP_OCCURRENCE,
    SKIP_REST_OF_DAY,
    Candybar,
    DispatchCursor,
    Invocation,
    Skip,
)

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_DATABASE_URL = "postgresql+psycopg://localhost:5432/candybar"
# Skip row ids are UUIDv5 of the override's identity, so storing the same
# skip twice is a primary-key no-op (Story 2.3) — no unique index migration.
SKIP_NAMESPACE = UUID("5b1c0f3e-8a52-4d0b-9a7e-2c6f1d4e8b90")


class DuplicateNameError(Exception):
    """A candybar with this name already exists (unique constraint hit).

    Registry-level error so callers never import sqlalchemy themselves — the
    sole-writer guard forbids DB imports outside this package.
    """


class UnknownCandybarError(KeyError):
    """No candybar with this id is registered (e.g. storing a skip for it)."""


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
    # Fires before registration are never "missed" (Story 2.2). None = unknown:
    # no floor beyond the dispatcher cursor.
    created_at: datetime | None = None


async def list_schedules(session: AsyncSession) -> list[ScheduleEntry]:
    """Read the current registry inventory without exposing ORM to dispatchers."""
    result = await session.execute(
        select(Candybar.id, Candybar.name, Candybar.schedule, Candybar.created_at)
    )
    return [
        ScheduleEntry(id=id, name=name, schedule=schedule, created_at=created_at)
        for id, name, schedule, created_at in result
    ]


def _require_tick_minute(minute: datetime, what: str = "cursor minute") -> None:
    if minute.tzinfo is None or minute.utcoffset() is None or minute.utcoffset().total_seconds():
        raise ValueError(f"{what} must be timezone-aware UTC")
    if minute.second or minute.microsecond:
        raise ValueError(f"{what} must be on a minute boundary")


async def get_dispatch_cursor(session: AsyncSession, name: str) -> datetime | None:
    """The newest tick minute the named tick consumer fully dispatched, or None."""
    result = await session.execute(
        select(DispatchCursor.tick_minute).where(DispatchCursor.name == name)
    )
    minute = result.scalar_one_or_none()
    return minute.astimezone(UTC) if minute is not None else None


async def advance_dispatch_cursor(session: AsyncSession, name: str, minute: datetime) -> None:
    """Move the named cursor forward to `minute`; never backwards (Story 2.2).

    One atomic upsert with GREATEST: a redelivered or replayed older tick, or
    a second dispatcher racing on an older minute, can never rewind it.
    """
    _require_tick_minute(minute)
    now = datetime.now(UTC)
    stmt = pg_insert(DispatchCursor).values(name=name, tick_minute=minute, updated_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=[DispatchCursor.name],
        set_={
            "tick_minute": func.greatest(DispatchCursor.tick_minute, stmt.excluded.tick_minute),
            "updated_at": now,
        },
    )
    await session.execute(stmt)


@dataclass(frozen=True)
class SkipEntry:
    """One schedule override row (`skips`), without exposing the ORM."""

    id: UUID
    candybar_id: UUID
    kind: str
    fire_time: datetime | None
    day: date | None
    reason: str | None  # free operator note; never the event reason
    created_at: datetime


def _skip_entry(row: Skip) -> SkipEntry:
    return SkipEntry(
        id=row.id,
        candybar_id=row.candybar_id,
        kind=row.kind,
        fire_time=row.fire_time.astimezone(UTC) if row.fire_time is not None else None,
        day=row.day,
        reason=row.reason,
        created_at=row.created_at,
    )


def occurrence_skip_id(candybar_id: UUID, fire_time: datetime) -> UUID:
    """The deterministic row id of the occurrence skip `{candybar_id, fire_time}`."""
    _require_tick_minute(fire_time, "fire_time")
    epoch_minute = int(fire_time.timestamp()) // 60
    return uuid5(SKIP_NAMESPACE, f"{SKIP_OCCURRENCE}:{candybar_id}:{epoch_minute}")


async def add_occurrence_skip(
    session: AsyncSession, candybar_id: UUID, fire_time: datetime, *, reason: str | None = None
) -> tuple[SkipEntry, bool]:
    """Store a skip of the single fire `fire_time` of a candybar (Story 2.3).

    Idempotent: returns `(entry, created)`, and `created` is False when the
    same skip was already stored (the first row, and its note, are kept).
    Whether `fire_time` is a fire the schedule actually produces is the
    caller's check (the API's 404, Story 5.6): a skip that matches no fire is
    inert. Raises UnknownCandybarError and ValueError (naive/off-minute time).
    """
    _require_tick_minute(fire_time, "fire_time")
    if await session.get(Candybar, candybar_id) is None:
        raise UnknownCandybarError(str(candybar_id))
    skip_id = occurrence_skip_id(candybar_id, fire_time)
    stmt = (
        pg_insert(Skip)
        .values(
            id=skip_id,
            candybar_id=candybar_id,
            kind=SKIP_OCCURRENCE,
            fire_time=fire_time,
            day=None,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[Skip.id])
        .returning(Skip.id)
    )
    try:
        created = (await session.execute(stmt)).scalar_one_or_none() is not None
    except IntegrityError as exc:  # the candybar was deleted under us
        if "skips_candybar_id_fkey" in str(exc):
            raise UnknownCandybarError(str(candybar_id)) from exc
        raise
    row = await session.get(Skip, skip_id, populate_existing=True)
    return _skip_entry(row), created


def _require_date(day: date) -> None:
    # A datetime IS a date subclass; the override's identity is the UTC date,
    # so a datetime (or anything else) is a caller bug, refused not coerced.
    if isinstance(day, datetime) or not isinstance(day, date):
        # ValueError (not TypeError): invalid skip inputs are one error class
        # for callers, matching add_occurrence_skip's bad-time ValueError.
        raise ValueError(  # noqa: TRY004
            "day must be a datetime.date (a UTC calendar date)"
        )


def rest_of_day_skip_id(candybar_id: UUID, day: date) -> UUID:
    """The deterministic row id of the rest-of-day skip `{candybar_id, day}`."""
    _require_date(day)
    return uuid5(SKIP_NAMESPACE, f"{SKIP_REST_OF_DAY}:{candybar_id}:{day.isoformat()}")


async def add_rest_of_day_skip(
    session: AsyncSession, candybar_id: UUID, day: date, *, reason: str | None = None
) -> tuple[SkipEntry, bool]:
    """Store a skip of every remaining fire of a candybar on the UTC date `day`.

    The stored date covers that whole UTC day; already-dispatched fires are
    unaffected (their ticks are gone), and the following UTC day dispatches
    normally. Idempotent, same contract as `add_occurrence_skip`: `(entry,
    created)`, `created` False keeps the first row and its note. Whether the
    schedule fires at all on `day` is the caller's check (the API's 404,
    Story 5.6): a fireless or past date stores inertly. Raises
    UnknownCandybarError and ValueError (a non-`date` day, incl. `datetime`).
    """
    _require_date(day)
    if await session.get(Candybar, candybar_id) is None:
        raise UnknownCandybarError(str(candybar_id))
    skip_id = rest_of_day_skip_id(candybar_id, day)
    stmt = (
        pg_insert(Skip)
        .values(
            id=skip_id,
            candybar_id=candybar_id,
            kind=SKIP_REST_OF_DAY,
            fire_time=None,
            day=day,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=[Skip.id])
        .returning(Skip.id)
    )
    try:
        created = (await session.execute(stmt)).scalar_one_or_none() is not None
    except IntegrityError as exc:  # the candybar was deleted under us
        if "skips_candybar_id_fkey" in str(exc):
            raise UnknownCandybarError(str(candybar_id)) from exc
        raise
    row = await session.get(Skip, skip_id, populate_existing=True)
    return _skip_entry(row), created


async def list_skips(session: AsyncSession, start: datetime, end: datetime) -> list[SkipEntry]:
    """Operator skips relevant to the UTC window `start <= t < end`.

    Occurrence rows with `start <= fire_time < end` (Story 2.3) plus
    rest-of-day rows whose date intersects the window (Story 2.4): fires in
    the window have dates in `[start.date(), (end - 1m).date()]`, and a day
    row matters iff its `day` is in that range. Ordered by fire_time, then
    day, then candybar id; day rows (NULL fire_time) sort last, oldest date
    first — every key can tie, so the triple keeps the order deterministic.
    """
    _require_tick_minute(start, "window start")
    _require_tick_minute(end, "window end")
    result = await session.scalars(
        select(Skip)
        .where(
            or_(
                and_(
                    Skip.kind == SKIP_OCCURRENCE,
                    Skip.fire_time >= start,
                    Skip.fire_time < end,
                ),
                and_(
                    Skip.kind == SKIP_REST_OF_DAY,
                    Skip.day >= start.date(),
                    Skip.day <= (end - timedelta(minutes=1)).date(),
                ),
            )
        )
        .order_by(Skip.fire_time, Skip.day, Skip.candybar_id)
    )
    return [_skip_entry(row) for row in result]


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


async def get_candybar(session: AsyncSession, candybar_id: UUID) -> Candybar | None:
    """The registry row for a candybar id, or None when unknown."""
    return await session.get(Candybar, candybar_id)


async def list_candybars(session: AsyncSession) -> list[Candybar]:
    """Every registered candybar, oldest first (created_at, then id) — Story 5.1."""
    result = await session.scalars(select(Candybar).order_by(Candybar.created_at, Candybar.id))
    return list(result.all())


async def lock_candybar(session: AsyncSession, candybar_id: UUID) -> Candybar | None:
    """The candybar row locked FOR UPDATE until the caller's commit, or None.

    Serializes concurrent definition updates of one candybar (Story 5.1): each
    diffs against the row the previous one committed, so `updated.changes`
    never reports a stale before-image.
    """
    return await session.get(Candybar, candybar_id, with_for_update=True, populate_existing=True)


# Definition fields a caller may change (Story 5.1); ids, timestamps and
# counters are registry-owned.
UPDATABLE_FIELDS = ("name", "description", "tags", "schedule", "entrypoint", "noise")


async def update_candybar(
    session: AsyncSession, row: Candybar, changes: dict[str, object]
) -> Candybar:
    """Apply `changes` to a (locked) candybar row and flush. Caller commits.

    Raises DuplicateNameError when a rename hits the unique name constraint.
    """
    unknown = set(changes) - set(UPDATABLE_FIELDS)
    if unknown:
        raise ValueError(f"not updatable: {sorted(unknown)}")
    for field_name, value in changes.items():
        setattr(row, field_name, list(value) if field_name == "tags" else value)  # type: ignore[arg-type]
    row.updated_at = datetime.now(UTC)
    try:
        await session.flush()
    except IntegrityError as exc:
        if "candybars_name_key" in str(exc):
            # Never touch `row` here: the failed flush expired it, and any
            # attribute load raises PendingRollbackError instead of this.
            raise DuplicateNameError(str(changes.get("name", ""))) from exc
        raise
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


# Upper bound for one page of invocation history (the API enforces it too).
INVOCATION_LIST_MAX = 500


async def get_invocation(session: AsyncSession, invocation_id: UUID) -> Invocation | None:
    """The recorded invocation row for an invocation id, or None (unknown or in flight).

    Rows are written only at the terminal state (Story 1.6), so an invocation
    still running has no row yet.
    """
    return await session.get(Invocation, invocation_id)


async def list_invocations(
    session: AsyncSession, candybar_id: UUID, limit: int = 50
) -> list[Invocation]:
    """A candybar's recorded invocations, newest first (index invocations_candybar_started)."""
    if not 1 <= limit <= INVOCATION_LIST_MAX:
        raise ValueError(f"limit must be within 1..{INVOCATION_LIST_MAX}, got {limit}")
    result = await session.execute(
        select(Invocation)
        .where(Invocation.candybar_id == candybar_id)
        .order_by(Invocation.started_at.desc(), Invocation.id.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


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
    # Executor-measured (Stories 3.1, 3.2); 0 when unmeasured.
    memory_peak_bytes: int = 0
    cpu_load: float = 0.0
    gpu_load: float = 0.0


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
        memory_peak_bytes=record.memory_peak_bytes,
        cpu_load=record.cpu_load,
        gpu_load=record.gpu_load,
        started_at=record.started_at,
        finished_at=record.finished_at,
        created_at=now,
    )
    # Atomic in-database increment: concurrent executions of one candybar run
    # as parallel tasks, and an ORM read-modify-write (`+= 1` on a loaded row)
    # loses updates under READ COMMITTED. The UPDATE also row-locks the
    # candybar until commit; a duplicate-key rollback below undoes it.
    bumped = await session.execute(
        update(Candybar)
        .where(Candybar.id == record.candybar_id)
        .values(
            total_invoked=Candybar.total_invoked + 1,
            total_successful=Candybar.total_successful
            + (1 if record.result == "success" else 0),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if bumped.rowcount == 0:
        raise KeyError(f"unknown candybar: {record.candybar_id}")
    session.add(row)
    try:
        await session.flush()
    except IntegrityError as exc:
        if "invocations_idempotency_key_key" in str(exc):
            raise DuplicateInvocationError(record.idempotency_key) from exc
        raise
