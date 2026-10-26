"""Enabled flag on candybars (Story 7.5, AD-21).

`enabled` (bool, NOT NULL, default true) lets a migration cutover register a
candybar disabled, retire the job's native scheduler, then enable it; a
rollback disables it again. A disabled candybar is never dispatched and has
no upcoming `/schedule` rows. Additive: every existing row reads `true` and
keeps firing exactly as before.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candybars",
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
    )


def downgrade() -> None:
    op.drop_column("candybars", "enabled")
