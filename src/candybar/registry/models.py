"""SQLAlchemy models for the candybar registry (AD-10 ERD).

Attribute detail is code-owned (spine ERD fixes names + relationships only).
`invocations` carries the fields the executor (1.6) obviously needs;
`skips` carries what Epic 2's skip operations need. `total_invoked` /
`total_successful` on `candybars` are updated in the SAME transaction as the
terminal invocation row — that write path lands with Story 1.6.
All datetimes are timestamptz: UTC everywhere (AD-13).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Double, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

TRIGGERS = ("scheduled", "manual", "misfire")
RESULTS = ("success", "fail")

# Skip kinds (Epic 2 owns the vocabulary). The kind is also the `skipped`
# event's `data.reason` for an operator skip.
SKIP_OCCURRENCE = "occurrence"  # one fire `{candybar_id, fire_time}` (Story 2.3)
SKIP_REST_OF_DAY = "rest_of_day"  # one candybar-local date `{candybar_id, day}` (Story 2.4, AD-16)
SKIP_KINDS = (SKIP_OCCURRENCE, SKIP_REST_OF_DAY)


class Base(DeclarativeBase):
    pass


class Candybar(Base):
    __tablename__ = "candybars"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    schedule: Mapped[str] = mapped_column(Text, nullable=False)
    # IANA zone the cron is read in (Story 7.1, AD-16, migration 0005); every
    # stored and emitted timestamp stays UTC.
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC", server_default="UTC")
    # The exec default for rows without a runner; for a runner row it holds the
    # runner's short label (`runners.runner_label`) so it stays NOT NULL.
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False)
    # How the executor starts the job (Story 7.3, AD-18, migration 0006):
    # `{kind: exec|systemd, ...}`, or SQL NULL for the bare-entrypoint default.
    runner: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    noise: Mapped[bool] = mapped_column(nullable=False, default=False)
    total_invoked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_successful: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Invocation(Base):
    __tablename__ = "invocations"

    # id IS the invocation_id carried by the invoke command / lifecycle events.
    id: Mapped[UUID] = mapped_column(primary_key=True)
    candybar_id: Mapped[UUID] = mapped_column(
        ForeignKey("candybars.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    tick_id: Mapped[str | None] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    result: Mapped[str | None] = mapped_column(Text)  # null until terminal
    exit_code: Mapped[int | None] = mapped_column()
    runtime_ms: Mapped[int | None] = mapped_column()
    error_message: Mapped[str | None] = mapped_column(Text)
    stderr_tail: Mapped[str | None] = mapped_column(Text)
    token_spend: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column()
    # Executor-measured from the cgroup scope (Story 3.1, migration 0002).
    memory_peak_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cpu_load: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    # Executor-measured via nvidia-smi pmon (Story 3.2, migration 0004).
    gpu_load: Mapped[float] = mapped_column(Double, nullable=False, default=0.0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("invocations_candybar_started", "candybar_id", "started_at"),)


class Skip(Base):
    __tablename__ = "skips"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    candybar_id: Mapped[UUID] = mapped_column(
        ForeignKey("candybars.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # one of SKIP_KINDS
    # `occurrence`: the suppressed UTC fire minute.
    fire_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    day: Mapped[date | None] = mapped_column()
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("skips_candybar", "candybar_id"),)


class DispatchCursor(Base):
    """The dispatcher's durable last-seen tick (Story 2.2, AD-6).

    One row per tick consumer, keyed by its durable name. `tick_minute` is the
    newest UTC tick minute fully dispatched; it only ever moves forward. A
    consumed tick more than one minute past it marks the gap whose missed
    fires are coalesced. Dispatcher state, not schedule-override state: the
    `skips` table stays operator overrides (2.3/2.4).
    """

    __tablename__ = "dispatch_cursor"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    tick_minute: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


# Names the sole-writer guard and migration agree on.
TABLES = ("candybars", "invocations", "skips", "dispatch_cursor")
