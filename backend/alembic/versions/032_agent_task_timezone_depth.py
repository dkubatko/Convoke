"""Agent tasks: per-task timezone and chain depth.

- `agent_tasks.timezone`: IANA zone the cron / a naive one-off time is read
  in (NULL = the reference timezone).
- `agent_tasks.depth`: 0 when a member asked; tasks may schedule follow-up
  tasks up to a fixed depth, so chains can't run away.

Revision ID: 032
Revises: 031
Create Date: 2026-10-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "032"
down_revision: str | None = "031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_tasks", sa.Column("timezone", sa.Text(), nullable=True))
    op.add_column(
        "agent_tasks", sa.Column("depth", sa.Integer(), nullable=False, server_default="0")
    )


def downgrade() -> None:
    op.drop_column("agent_tasks", "depth")
    op.drop_column("agent_tasks", "timezone")
