"""Per-invocation GPU load column on invocations (Story 3.2, AD-9).

`gpu_load` is the executor-measured average GPU load of the invocation's
process tree: nvidia-smi pmon per-pid SM utilization averaged over the
samples taken while it ran (1.0 = one busy GPU). Additive and NOT NULL with
a server default of 0, like 0002: earlier rows and GPU-less hosts read as
unmeasured.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invocations",
        sa.Column("gpu_load", sa.Double, nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "invocations_gpu_load_nonnegative",
        "invocations",
        "gpu_load >= 0",
    )


def downgrade() -> None:
    op.drop_constraint("invocations_gpu_load_nonnegative", "invocations", type_="check")
    op.drop_column("invocations", "gpu_load")
