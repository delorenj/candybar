"""Dispatcher last-seen tick cursor (Story 2.2, AD-6 misfire coalescing).

One row per tick consumer (keyed by its durable name) holding the newest UTC
tick minute the dispatcher fully dispatched. A consumed tick more than one
minute past it is the gap whose missed fires are coalesced. Additive and
owner-only like 0001: no GRANTs, the daemon is the sole writer (AD-10).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispatch_cursor",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("tick_minute", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("dispatch_cursor")
