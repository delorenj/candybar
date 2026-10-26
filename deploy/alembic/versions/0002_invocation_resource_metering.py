"""Per-invocation resource metering columns on invocations (Story 3.1, AD-9).

`memory_peak_bytes` (cgroup v2 memory.peak) and `cpu_load` (cpu.stat
usage_usec delta / wall runtime, average busy cores) are executor-measured.
Additive and NOT NULL with a server default of 0: rows recorded before this
revision read as unmeasured, which is what they were.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invocations",
        sa.Column("memory_peak_bytes", sa.BigInteger, nullable=False, server_default="0"),
    )
    op.add_column(
        "invocations",
        sa.Column("cpu_load", sa.Double, nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "invocations_metering_nonnegative",
        "invocations",
        "memory_peak_bytes >= 0 AND cpu_load >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("invocations_metering_nonnegative", "invocations", type_="check")
    op.drop_column("invocations", "cpu_load")
    op.drop_column("invocations", "memory_peak_bytes")
