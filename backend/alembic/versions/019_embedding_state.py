"""Embedding model becomes operator-configurable: the embedding_state singleton

Which model owns the stored vectors, plus swap-job progress. The vector
columns' dimension is changed by the re-embed job at swap time (runtime data,
not authored schema); this migration seeds the singleton with the e5-small
bootstrap default.

Revision ID: 019
Revises: 018
Create Date: 2026-07-05

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "embedding_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("doc_prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("query_prefix", sa.Text(), nullable=False, server_default=""),
        sa.Column("threshold_floor", sa.Float(), nullable=False, server_default="0.70"),
        sa.Column("threshold_ceil", sa.Float(), nullable=False, server_default="0.88"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ready"),
        sa.Column("target", sa.JSON(), nullable=True),
        sa.Column("phase", sa.Text(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("done", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.execute(
        "INSERT INTO embedding_state "
        "(id, model_id, dim, doc_prefix, query_prefix, threshold_floor, threshold_ceil, status) "
        "VALUES (1, 'intfloat/multilingual-e5-small', 384, 'passage: ', 'query: ', 0.70, 0.88, 'ready')"
    )


def downgrade() -> None:
    op.drop_table("embedding_state")
