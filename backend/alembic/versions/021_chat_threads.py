"""Per-thread monitoring: the chat_threads table.

Records a thread's title (captured from forum-topic service events or assigned
by an operator) and whether the bot monitors it. Rows exist only for threads
with a title or a non-default monitored flag; a thread with no row is monitored
and unnamed, so the default (all threads monitored) needs no backfill.

Revision ID: 021
Revises: 020
Create Date: 2026-07-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_threads",
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("thread_key", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("monitored", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id", "thread_key"),
    )


def downgrade() -> None:
    op.drop_table("chat_threads")
