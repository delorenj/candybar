"""Steward task spend: cost and token usage (steward-contract.md section 3).

`steward_tasks.cost_usd` (double precision, nullable): the OpenCode
session's cost as the provider reports it. `steward_tasks.tokens` (jsonb,
nullable): its token usage `{input, output, reasoning, cache: {read,
write}}`. Both stay null until a session ran. Additive: every existing row
reads null.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("steward_tasks", sa.Column("cost_usd", sa.Double(), nullable=True))
    op.add_column("steward_tasks", sa.Column("tokens", postgresql.JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("steward_tasks", "tokens")
    op.drop_column("steward_tasks", "cost_usd")
