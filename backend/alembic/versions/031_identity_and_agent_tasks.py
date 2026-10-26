"""Per-chat identity and agent-scheduled tasks.

- `chats.identity` (nullable text): the operator-written identity for the
  bot in this chat; NULL means the built-in default.
- `agent_tasks`: one-off / recurring tasks the agent schedules in a chat.
- `agent_runs.task_id`: which task queued a run (trigger 'task').

Revision ID: 031
Revises: 030
Create Date: 2026-10-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "031"
down_revision: str | None = "030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chats", sa.Column("identity", sa.Text(), nullable=True))
    op.create_table(
        "agent_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "chat_id", sa.Integer(), sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("thread_id", sa.BigInteger(), nullable=True),
        sa.Column("task", sa.Text(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cron", sa.Text(), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.Column("created_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("created_by_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_run_id", sa.Integer(), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_agent_tasks_due", "agent_tasks", ["status", "next_fire_at"])
    op.add_column(
        "agent_runs",
        sa.Column(
            "task_id", sa.Integer(),
            sa.ForeignKey("agent_tasks.id", ondelete="SET NULL"), nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "task_id")
    op.drop_index("ix_agent_tasks_due", table_name="agent_tasks")
    op.drop_table("agent_tasks")
    op.drop_column("chats", "identity")
