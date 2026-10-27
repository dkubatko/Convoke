"""Names members call the bot by, per chat.

`chats.call_names` (nullable JSON list): plain names ("Бот", "Bot") whose
whole-word appearance in a message invokes the agent like an @mention.

Revision ID: 033
Revises: 032
Create Date: 2026-10-27

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "033"
down_revision: str | None = "032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chats",
        sa.Column(
            "call_names", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("chats", "call_names")
