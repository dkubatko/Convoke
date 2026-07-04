"""Per-chat detector setting overrides

Revision ID: 014
Revises: 013
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "014"
down_revision: str | None = "013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_settings",
        sa.Column("chat_id", sa.Integer(), sa.ForeignKey("chats.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("chat_settings")
