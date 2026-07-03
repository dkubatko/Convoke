"""Workflow observability: per-evaluation stage/score on trigger_states,
workflow_id on agent_runs

Revision ID: 008
Revises: 007
Create Date: 2026-07-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "008"
down_revision: str | None = "007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("trigger_states", sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trigger_states", sa.Column("last_stage", sa.Text(), nullable=True))
    op.add_column("trigger_states", sa.Column("last_score", sa.Float(), nullable=True))
    op.add_column("trigger_states", sa.Column("last_confidence", sa.Float(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column(
            "workflow_id",
            sa.Integer(),
            sa.ForeignKey("workflows.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "workflow_id")
    op.drop_column("trigger_states", "last_confidence")
    op.drop_column("trigger_states", "last_score")
    op.drop_column("trigger_states", "last_stage")
    op.drop_column("trigger_states", "last_evaluated_at")
