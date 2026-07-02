"""Gap markers + bot last_polled_at

Revision ID: 007
Revises: 006
Create Date: 2026-07-02

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "memory_gaps",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gap_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gap_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_memory_gaps_chat", "memory_gaps", ["chat_id"])


def downgrade() -> None:
    op.drop_index("ix_memory_gaps_chat", table_name="memory_gaps")
    op.drop_table("memory_gaps")
    op.drop_column("bots", "last_polled_at")
