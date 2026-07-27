"""Record the media an agent run attached to its reply.

Adds `agent_runs.media` (nullable JSON): [{"kind", "source", "ok"}] per
attached item, written after delivery so it reflects what actually reached
the chat. Nullable with no backfill — null means the run predates the
feature or attached nothing (media is only written when the agent attached
something).

Revision ID: 029
Revises: 028
Create Date: 2026-07-26

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "029"
down_revision: str | None = "028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # JSONB on Postgres from the start — 022 created tool_calls as plain json
    # against a JSONVariant model and 024 had to retype it; don't repeat that.
    op.add_column(
        "agent_runs",
        sa.Column(
            "media",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("agent_runs", "media")
