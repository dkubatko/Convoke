"""Chat member identity mapping (stable id -> display name).

Creates `chat_members`: one row per (chat, Telegram user id) holding the name
we render for that person everywhere the model sees a message. Decouples the
displayed name from the raw per-message `sender_name`, so a person reads
consistently across imported history (which may carry a different label, e.g.
an exporter's phone-contact name) and live traffic.

Backfill: `auto_name` = each user's latest observed name (by sent_at), from
non-bot messages. `override_name`/`handle` start null.

Then invalidates memory (drops chunks + the chunk cursor) so the worker
re-chunks under the new name-resolving render — no manual step, same path the
importer already uses.

Revision ID: 023
Revises: 022
Create Date: 2026-07-08

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "chat_members",
        sa.Column(
            "chat_id",
            sa.Integer(),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sender_id", sa.BigInteger(), primary_key=True),
        sa.Column("auto_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("override_name", sa.Text(), nullable=True),
        sa.Column("handle", sa.Text(), nullable=True),
        sa.Column("name_basis_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    # Seed auto_name from each user's latest observed name (skip the bot's own
    # sends). DISTINCT ON is Postgres-only; migrations run against Postgres.
    op.execute(
        """
        INSERT INTO chat_members (chat_id, sender_id, auto_name, name_basis_at)
        SELECT DISTINCT ON (chat_id, sender_id)
               chat_id,
               sender_id,
               TRIM(COALESCE(sender_name, '')),  -- app-side writes are trimmed too
               CASE WHEN NULLIF(TRIM(COALESCE(sender_name, '')), '') IS NOT NULL
                    THEN sent_at END
        FROM messages
        WHERE sender_id IS NOT NULL
          AND source <> 'self'
          -- Exclude the bot itself: its live sends are source='self', but an
          -- imported export can carry its past messages as source='import',
          -- so also filter by bot id (globally unique, won't match a user).
          AND sender_id NOT IN (SELECT tg_bot_id FROM bots)
        -- Prefer the latest message that actually carries a name, so a member
        -- whose newest message is empty (deleted account, service-ish row)
        -- still seeds a real auto_name rather than ''.
        ORDER BY chat_id, sender_id,
                 (NULLIF(TRIM(COALESCE(sender_name, '')), '') IS NOT NULL) DESC,
                 sent_at DESC
        """
    )

    # Existing chunks embed the pre-mapping names; drop them + the cursor so the
    # memory loop rebuilds every chat under the new render (canonical names).
    op.execute("DELETE FROM chunks")
    op.execute("DELETE FROM chunk_state")


def downgrade() -> None:
    op.drop_table("chat_members")
    op.execute("DELETE FROM chunks")
    op.execute("DELETE FROM chunk_state")
