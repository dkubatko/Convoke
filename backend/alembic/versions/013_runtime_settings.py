"""Operator-tunable runtime settings

Revision ID: 013
Revises: 012
Create Date: 2026-07-03

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "runtime_settings",
        sa.Column("key", sa.String(), primary_key=True),
        sa.Column("value", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("runtime_settings")
