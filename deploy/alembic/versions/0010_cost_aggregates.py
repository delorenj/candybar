"""Candybar cost: `invocations.cpu_seconds` and the `candybar_costs` aggregate.

`invocations.cpu_seconds` (double precision, nullable): the CPU time one run
used, in core-seconds, measured by its runner. NULL means "not measured", which
`cpu_load` (NOT NULL, "unmeasured reads 0") could not say. It also stops a
systemd run's bad `cpu_load` from counting: the systemd runner divided the
unit's whole-activation CPU by only its LAST ExecStart's wall span, so a
oneshot with ExecStartPre lines reported 10-100,000x too much. Rows written
by that runner keep `cpu_seconds` NULL; their `cpu_load` stays as recorded.

The backfill derives `cpu_seconds = cpu_load * runtime_ms / 1000` for every
finished run of a non-systemd candybar whose `cpu_load` is above 0 (the exec
runner's `cpu_load` IS usage / runtime, so this inverts it up to the 4-decimal
rounding it stored).

`candybar_costs` (one row per candybar): the rolling-window totals the cost
surfaces read. The executor refreshes a candybar's row when one of its
invocations reaches a terminal state, and `candybar cost --refresh` rebuilds
all of them. `totals` is jsonb so a new cost dimension adds a key, not a
migration.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("invocations", sa.Column("cpu_seconds", sa.Double(), nullable=True))
    op.create_check_constraint(
        "invocations_cpu_seconds_nonnegative",
        "invocations",
        "cpu_seconds IS NULL OR cpu_seconds >= 0",
    )
    op.execute(
        """
        UPDATE invocations AS i
        SET cpu_seconds = i.cpu_load * i.runtime_ms / 1000.0
        FROM candybars AS c
        WHERE c.id = i.candybar_id
          AND i.result IS NOT NULL
          AND i.runtime_ms IS NOT NULL
          AND i.cpu_load > 0
          AND coalesce(c.runner ->> 'kind', 'exec') <> 'systemd'
        """
    )
    op.create_table(
        "candybar_costs",
        sa.Column(
            "candybar_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("candybars.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("totals", postgresql.JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("candybar_costs")
    op.drop_constraint("invocations_cpu_seconds_nonnegative", "invocations", type_="check")
    op.drop_column("invocations", "cpu_seconds")
