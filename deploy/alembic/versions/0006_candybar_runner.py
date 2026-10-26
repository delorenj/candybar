"""Runner definition on candybars (Story 7.3, AD-18).

`runner` is the jsonb `{kind: exec|systemd, ...}` that tells the executor how
to start a candybar's work. Additive and NULLABLE: a NULL runner is the
pre-Epic-7 default (exec the bare `entrypoint`), so every existing row keeps
running exactly as before. Shape validity (kind, argv, unit name) is the
API's check at registration, not a database constraint.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("candybars", sa.Column("runner", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("candybars", "runner")
