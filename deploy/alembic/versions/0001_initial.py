"""Initial registry schema: candybars, invocations, skips (AD-10 ERD).

Attribute detail is code-owned; this migration mirrors
candybar.registry.models exactly. Runs as the `candybar` database role:
all objects are owner-only and no GRANTs are issued — the daemon is the
sole writer.

Revision ID: 0001
Revises:
Create Date: 2026-10-25

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candybars",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("schedule", sa.Text, nullable=False),
        sa.Column("entrypoint", sa.Text, nullable=False),
        sa.Column("noise", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("total_invoked", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("total_successful", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="candybars_name_key"),
    )
    op.create_table(
        "invocations",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "candybar_id",
            sa.Uuid,
            sa.ForeignKey("candybars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("tick_id", sa.Text, nullable=True),
        sa.Column("idempotency_key", sa.Text, nullable=False),
        sa.Column("result", sa.Text, nullable=True),
        sa.Column("exit_code", sa.Integer, nullable=True),
        sa.Column("runtime_ms", sa.Integer, nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("stderr_tail", sa.Text, nullable=True),
        sa.Column("token_spend", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Numeric, nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "trigger IN ('scheduled', 'manual', 'misfire')", name="invocations_trigger_check"
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('success', 'fail')", name="invocations_result_check"
        ),
        sa.UniqueConstraint("idempotency_key", name="invocations_idempotency_key_key"),
    )
    op.create_index(
        "invocations_candybar_started", "invocations", ["candybar_id", "started_at"]
    )
    op.create_table(
        "skips",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "candybar_id",
            sa.Uuid,
            sa.ForeignKey("candybars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("fire_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("day", sa.Date, nullable=True),
        sa.Column("reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("skips_candybar", "skips", ["candybar_id"])


def downgrade() -> None:
    op.drop_index("skips_candybar", table_name="skips")
    op.drop_table("skips")
    op.drop_index("invocations_candybar_started", table_name="invocations")
    op.drop_table("invocations")
    op.drop_table("candybars")
