"""Per-trigger evaluation cursor on trigger_states

Revision ID: 011
Revises: 010
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trigger_states",
        sa.Column("last_tg_message_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # Default: inherit the old shared per-chat cursor so workflows don't
    # re-evaluate their whole backlog on deploy.
    op.execute(
        """
        UPDATE trigger_states ts
        SET last_tg_message_id = COALESCE(
            (SELECT ces.last_tg_message_id FROM chat_eval_state ces
             WHERE ces.chat_id = ts.chat_id), 0)
        """
    )
    # Exception: a workflow currently (or recently) in cooldown had its
    # during-cooldown messages consumed by the old shared cursor. Rewind its
    # cursor to just before the fire that started the cooldown, so those
    # messages are re-evaluated once the cooldown lifts (the whole point of the
    # per-trigger cursor). Bounded to the last 6h to avoid deep backlogs.
    op.execute(
        """
        UPDATE trigger_states ts
        SET last_tg_message_id = COALESCE((
            SELECT MAX(m.tg_message_id) FROM messages m
            WHERE m.chat_id = ts.chat_id
              AND m.sent_at <= ts.cooldown_until - make_interval(secs => w.cooldown_seconds)
        ), 0)
        FROM workflows w
        WHERE w.id = ts.workflow_id
          AND ts.cooldown_until IS NOT NULL
          AND ts.cooldown_until > now() - interval '6 hours'
        """
    )


def downgrade() -> None:
    op.drop_column("trigger_states", "last_tg_message_id")
