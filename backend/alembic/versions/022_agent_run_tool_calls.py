"""Record the tools an agent called per run.

Adds `agent_runs.tool_calls` (nullable JSON): an ordered list of the tools the
agent invoked during the run, each {"tool", "args", "ok"}. Nullable with no
backfill — existing rows predate capture and read as "unknown" (null), distinct
from an empty list ("called no tools").

Revision ID: 022
Revises: 021
Create Date: 2026-07-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agent_runs", sa.Column("tool_calls", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("agent_runs", "tool_calls")
