"""Archive flag, candybar profiles and steward tasks (steward-contract.md).

`candybars.archived` (bool, NOT NULL, default false): an archived candybar is
hidden from lists and never scheduled (archiving also disables it).
`candybar_profiles`: one row per candybar, created lazily, holding the
steward's overview, project classification and latest evaluation (contract
section 2). `steward_tasks`: one row per enrich / fix / evaluate / refactor
task (contract section 3), indexed for the per-candybar history read and with
a partial UNIQUE index that allows at most one queued-or-running task per
(candybar, action). Additive: every existing row reads `archived = false`.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-03

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "candybars",
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "candybar_profiles",
        sa.Column(
            "candybar_id",
            sa.Uuid,
            sa.ForeignKey("candybars.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("overview", postgresql.JSONB, nullable=True),
        sa.Column("template_version", sa.Integer, nullable=True),
        sa.Column("project", postgresql.JSONB, nullable=True),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrich_task_id", sa.Uuid, nullable=True),
        sa.Column("evaluation", postgresql.JSONB, nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "steward_tasks",
        sa.Column("id", sa.Uuid, primary_key=True),
        sa.Column(
            "candybar_id",
            sa.Uuid,
            sa.ForeignKey("candybars.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("model", sa.Text, nullable=False),
        sa.Column(
            "parent_task_id",
            sa.Uuid,
            sa.ForeignKey("steward_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("session_id", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("report_key", sa.Text, nullable=True),
        sa.Column("invocation_id", sa.Uuid, nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action IN ('enrich', 'fix', 'evaluate', 'refactor')",
            name="steward_tasks_action_check",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="steward_tasks_status_check",
        ),
    )
    op.create_index(
        "steward_tasks_candybar_created", "steward_tasks", ["candybar_id", "created_at"]
    )
    op.create_index(
        "steward_tasks_active",
        "steward_tasks",
        ["candybar_id", "action"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_index("steward_tasks_active", table_name="steward_tasks")
    op.drop_index("steward_tasks_candybar_created", table_name="steward_tasks")
    op.drop_table("steward_tasks")
    op.drop_table("candybar_profiles")
    op.drop_column("candybars", "archived")
