"""Polls, structured message content, and generic files.

- `polls` / `poll_votes`: native polls as live records (votes for the bot's
  own polls, final tallies when stopped).
- `messages.content` (nullable JSON): locations, venues, contacts, dice,
  checklists… stored structured and rendered at read time.
- `message_attachments.file_name`: for the new "file" kind (non-image
  documents, read on demand by inspect_media).

Backfill: inbox updates whose message carried only such content were dropped
by the old handler (no text, no supported media) but are still in the inbox.
Those with no stored message row are re-queued so the consumer ingests them
through the normal (idempotent) handler.

Revision ID: 030
Revises: 029
Create Date: 2026-10-14

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "030"
down_revision: str | None = "029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")

# Message keys whose content the new handler stores.
NEW_CONTENT_KEYS = (
    "poll", "location", "venue", "contact", "dice", "checklist", "document",
    "story", "game", "invoice", "giveaway", "paid_media",
)


def upgrade() -> None:
    op.add_column("messages", sa.Column("content", JSON, nullable=True))
    op.add_column("message_attachments", sa.Column("file_name", sa.Text(), nullable=True))
    op.create_table(
        "polls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "message_id", sa.Integer(),
            sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, unique=True,
        ),
        sa.Column(
            "chat_id", sa.Integer(), sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tg_message_id", sa.BigInteger(), nullable=False),
        sa.Column("tg_poll_id", sa.Text(), nullable=True),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("options", JSON, nullable=False),
        sa.Column("type", sa.Text(), nullable=False, server_default="regular"),
        sa.Column("multiple_answers", sa.Boolean(), nullable=True),
        sa.Column("anonymous", sa.Boolean(), nullable=True),
        sa.Column("sent_by_bot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("close_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_voters", sa.Integer(), nullable=True),
        sa.Column("counts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_polls_tg_poll_id", "polls", ["tg_poll_id"])
    op.create_table(
        "poll_votes",
        sa.Column(
            "poll_id", sa.Integer(), sa.ForeignKey("polls.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("voter_id", sa.BigInteger(), primary_key=True),
        sa.Column("voter_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("option_ids", JSON, nullable=False),
        sa.Column("voted_at", sa.DateTime(timezone=True), nullable=False),
    )

    if op.get_bind().dialect.name != "postgresql":
        return
    keys = ", ".join(f"'{k}'" for k in NEW_CONTENT_KEYS)
    op.execute(
        f"""
        UPDATE updates_inbox u
        SET processed_at = NULL, error = NULL
        FROM chats c
        WHERE u.payload ? 'message'
          AND c.bot_id = u.bot_id
          AND c.tg_chat_id = (u.payload->'message'->'chat'->>'id')::bigint
          AND EXISTS (
            SELECT 1 FROM jsonb_object_keys(u.payload->'message') k WHERE k IN ({keys})
          )
          AND NOT EXISTS (
            SELECT 1 FROM messages m
            WHERE m.chat_id = c.id
              AND m.tg_message_id = (u.payload->'message'->>'message_id')::bigint
          )
        """
    )


def downgrade() -> None:
    op.drop_table("poll_votes")
    op.drop_index("ix_polls_tg_poll_id", table_name="polls")
    op.drop_table("polls")
    op.drop_column("message_attachments", "file_name")
    op.drop_column("messages", "content")
