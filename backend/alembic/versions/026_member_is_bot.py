"""chat_members.is_bot: bot senders are tagged in renders, unscored by memory.

Bot output derives from the chat (it summarizes/answers; it doesn't witness),
so memory search stops SCORING it: bot lines are stripped from the embedding
input while staying in chunk text, lexical search, and direct reads. Every
render also tags bot lines [bot] so models can weigh provenance.

The chat's own bot account is backfilled true — its live sends are marked
source='self' anyway, but its imported previous-generation history is only
identifiable by sender id. Other bots (Telegram exports carry no bot flag)
are operator-flagged from the Members tab; flipping the flag stales the
chat's chunks, same contract as a rename.

Revision ID: 026
Revises: 025
Create Date: 2026-07-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "026"
down_revision: str | None = "025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_members",
        sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The owning bot's account: flag any member row it has in its chats (its
    # own sends usually aren't member rows, but imported old-generation
    # history created one).
    op.execute(
        """
        UPDATE chat_members SET is_bot = true
        FROM chats JOIN bots ON bots.id = chats.bot_id
        WHERE chat_members.chat_id = chats.id
          AND chat_members.sender_id = bots.tg_bot_id
        """
    )


def downgrade() -> None:
    op.drop_column("chat_members", "is_bot")
