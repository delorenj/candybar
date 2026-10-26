"""Per-candybar IANA timezone on candybars (Story 7.1, AD-16).

`timezone` is the IANA zone a candybar's cron is read in as local wall time.
Additive and NOT NULL with a server default of `UTC`: every pre-Epic-7 row
keeps evaluating exactly as before. Validity of the name is the API's
(zoneinfo) check at registration, not a database constraint.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-26

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candybars",
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
    )


def downgrade() -> None:
    op.drop_column("candybars", "timezone")
