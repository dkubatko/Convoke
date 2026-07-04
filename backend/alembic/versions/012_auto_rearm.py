"""Automatic dedup (lull re-arm) + optional slot-agnostic cooldown rate limit

Revision ID: 012
Revises: 011
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No-slot dedup: the "armed" bit. Fire disarms; the conversation moving on
    # (a lull or an off-topic window) re-arms.
    op.add_column(
        "trigger_states",
        sa.Column("armed", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    # A state mid-cooldown is "recently fired" → start it disarmed so it doesn't
    # immediately re-fire under the new automatic dedup.
    op.execute(
        "UPDATE trigger_states SET armed = false "
        "WHERE cooldown_until IS NOT NULL AND cooldown_until > now()"
    )
    # cooldown is now an OPTIONAL rate limit, off by default. Existing workflows
    # carried the old 3600s default; clear it so they use automatic dedup, and
    # move the column default to 0. cooldown_until (trigger_states) stays.
    op.execute("UPDATE workflows SET cooldown_seconds = 0")
    op.alter_column("workflows", "cooldown_seconds", server_default="0")


def downgrade() -> None:
    op.alter_column("workflows", "cooldown_seconds", server_default="3600")
    op.drop_column("trigger_states", "armed")
