"""Default embedding model → distiluse; drop the per-model clamp band.

The retrieval-style e5-small default separated on-topic from off-topic poorly
on non-English chats (near-random on real Russian traffic). The registry now
ships multilingual PARAPHRASE models; distiluse is the recommended default. The
prefilter clamp band is now one global constant (recall-first calibration
self-scales to the model), so the per-row threshold_floor/threshold_ceil columns
are dropped.

The model flip only rewrites the singleton for a FRESH install — still on the
e5-small seed with nothing embedded yet — where changing the model + dimension
is free. An existing deploy with stored vectors is left on e5 until the operator
triggers a re-embed from the Models page (its 384d vectors can't be reinterpreted
by a migration).

Revision ID: 020
Revises: 019
Create Date: 2026-07-06

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "020"
down_revision: str | None = "019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE embedding_state
        SET model_id = 'sentence-transformers/distiluse-base-multilingual-cased-v2',
            dim = 512, doc_prefix = '', query_prefix = ''
        WHERE id = 1
          AND model_id = 'intfloat/multilingual-e5-small'
          AND NOT EXISTS (SELECT 1 FROM chunks WHERE embedding IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM workflow_examples WHERE embedding IS NOT NULL)
          AND NOT EXISTS (SELECT 1 FROM notes WHERE embedding IS NOT NULL)
        """
    )
    with op.batch_alter_table("embedding_state") as batch:
        batch.drop_column("threshold_floor")
        batch.drop_column("threshold_ceil")


def downgrade() -> None:
    with op.batch_alter_table("embedding_state") as batch:
        batch.add_column(sa.Column("threshold_floor", sa.Float(), server_default="0.15"))
        batch.add_column(sa.Column("threshold_ceil", sa.Float(), server_default="0.90"))
