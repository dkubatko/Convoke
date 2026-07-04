"""Episode-centric intent pipeline: topics as first-class rows

Replaces the per-(workflow, chat, thread) TriggerState blob with:
- intent_cursors: the evaluation cursor + observability, split out
- intent_episodes: one row per occurrence of an intent, with a lifecycle
  (candidate → tracking → converged → fired → satisfied → closed), per-episode
  slots, a rolling summary, and the agent's execution summary (the feedback
  loop that suppresses same-topic re-fires)

Revision ID: 015
Revises: 014
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "015"
down_revision: str | None = "014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("trigger_states")

    op.create_table(
        "intent_cursors",
        sa.Column("workflow_id", sa.Integer(),
                  sa.ForeignKey("workflows.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("chat_id", sa.Integer(),
                  sa.ForeignKey("chats.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("thread_key", sa.BigInteger(), primary_key=True, server_default="0"),
        sa.Column("last_tg_message_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_llm_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_stage", sa.Text(), nullable=True),
        sa.Column("last_score", sa.Float(), nullable=True),
        sa.Column("last_confidence", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
    )

    op.create_table(
        "intent_episodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workflow_id", sa.Integer(),
                  sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Integer(),
                  sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_key", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.Text(), nullable=False, server_default="candidate"),
        sa.Column("slots", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("anchor_tg_message_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("fingerprint", sa.Text(), nullable=True),
        sa.Column("execution_summary", sa.Text(), nullable=True),
        sa.Column("agent_run_id", sa.Integer(), nullable=True),
        sa.Column("unrelated_streak", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parked_at_tg_message_id", sa.BigInteger(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("close_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_intent_episodes_key_status", "intent_episodes",
        ["workflow_id", "chat_id", "thread_key", "status"],
    )
    op.create_index(
        "ix_intent_episodes_fingerprint", "intent_episodes",
        ["workflow_id", "chat_id", "fingerprint"],
    )

    op.add_column(
        "pending_fires",
        sa.Column("episode_id", sa.Integer(),
                  sa.ForeignKey("intent_episodes.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index(
        "uq_pending_fires_live_episode", "pending_fires", ["episode_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','confirm_wait','confirmed') AND episode_id IS NOT NULL"
        ),
    )

    op.add_column(
        "workflows",
        sa.Column("dedup_window_hours", sa.Integer(), nullable=False, server_default="12"),
    )


def downgrade() -> None:
    op.drop_column("workflows", "dedup_window_hours")
    op.drop_index("uq_pending_fires_live_episode", table_name="pending_fires")
    op.drop_column("pending_fires", "episode_id")
    op.drop_index("ix_intent_episodes_fingerprint", table_name="intent_episodes")
    op.drop_index("ix_intent_episodes_key_status", table_name="intent_episodes")
    op.drop_table("intent_episodes")
    op.drop_table("intent_cursors")
    # Recreate trigger_states as of revision 012 (empty — evaluation state is
    # rebuilt by the sweeper).
    op.create_table(
        "trigger_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("workflow_id", sa.Integer(),
                  sa.ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("chat_id", sa.Integer(),
                  sa.ForeignKey("chats.id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_key", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("last_tg_message_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("slots", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_match_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_llm_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("armed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_stage", sa.Text(), nullable=True),
        sa.Column("last_score", sa.Float(), nullable=True),
        sa.Column("last_confidence", sa.Float(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint("workflow_id", "chat_id", "thread_key"),
    )
