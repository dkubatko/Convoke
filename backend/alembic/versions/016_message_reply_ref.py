"""Messages remember what they reply to; tracking merges into candidate

Replies: the intent classifier renders the quoted original inline, so a reply
after a long pause ("sure, that works" → replying to yesterday's proposal)
carries its context even when the original is far outside the transcript.

Episodes: the candidate/tracking split collapses into one pre-fire state —
`candidate` — whose leash and cap protection derive from gathered slots.

Revision ID: 016
Revises: 015
Create Date: 2026-07-04

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column("reply_to_tg_message_id", sa.BigInteger(), nullable=True),
    )
    op.execute("UPDATE intent_episodes SET status = 'candidate' WHERE status = 'tracking'")


def downgrade() -> None:
    op.drop_column("messages", "reply_to_tg_message_id")
