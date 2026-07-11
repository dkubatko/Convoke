"""connected_models.api: which OpenAI-compatible dialect the endpoint speaks.

'chat' (/chat/completions, the universal default) or 'responses'
(/v1/responses — reasoning persists across tool calls; some models allow
reasoning WITH tools only there). Explicit and operator-chosen, never
auto-detected: half-implemented /responses endpoints pass trivial probes
and break on real workloads.

Revision ID: 028
Revises: 027
Create Date: 2026-07-11

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "028"
down_revision: str | None = "027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "connected_models",
        sa.Column("api", sa.Text(), nullable=False, server_default="chat"),
    )


def downgrade() -> None:
    op.drop_column("connected_models", "api")
