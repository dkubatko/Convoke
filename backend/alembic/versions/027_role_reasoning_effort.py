"""model_role_assignments.reasoning_effort: per-role reasoning level.

NULL = Default (the parameter is omitted from calls entirely). Values are
validated with a live micro-call at assignment time — no OpenAI-compatible
provider exposes a discovery API for supported levels, so the only truth is
asking. Per-role because one model can serve both agent (wants effort) and
intent (wants latency).

Revision ID: 027
Revises: 026
Create Date: 2026-07-10

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "027"
down_revision: str | None = "026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "model_role_assignments", sa.Column("reasoning_effort", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("model_role_assignments", "reasoning_effort")
