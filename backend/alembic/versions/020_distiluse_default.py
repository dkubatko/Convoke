"""Default embedding model → distiluse; drop the per-model clamp band.

The retrieval-style e5-small default separated on-topic from off-topic poorly
on non-English chats (near-random on real Russian traffic). The registry now
ships multilingual PARAPHRASE models; distiluse is the recommended default. The
prefilter clamp band is now one global constant (recall-first calibration
self-scales to the model), so the per-row threshold_floor/threshold_ceil columns
are dropped.

The model flip only rewrites the singleton for a FRESH install — still on the
e5-small seed with nothing embedded yet — where changing the model + dimension
is free. When it fires, the same branch also retypes the three vector columns
(created vector(384) by 003/004/006) to vector(512), dropping/recreating the
chunks HNSW index — the same DDL the re-embed job (app/memory/reembed.py) runs
— so the very first embed write succeeds. An existing deploy with stored
vectors is left on e5 until the operator triggers a re-embed from the Models
page (its 384d vectors can't be reinterpreted by a migration).

The model flip is ONE-WAY: downgrade restores 019's threshold columns but does
not revert model_id/dim or the vector column dimensions (019's schema makes no
claim about them — the dimension is runtime data owned by the re-embed job).

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
    # Fresh-install check + flip + column retype run server-side in one DO
    # block: the retype must happen exactly when the flip does (IF FOUND), and
    # a DO block still renders under `alembic upgrade --sql`.
    op.execute(
        """
        DO $$
        BEGIN
            UPDATE embedding_state
            SET model_id = 'sentence-transformers/distiluse-base-multilingual-cased-v2',
                dim = 512, doc_prefix = '', query_prefix = ''
            WHERE id = 1
              AND model_id = 'intfloat/multilingual-e5-small'
              AND NOT EXISTS (SELECT 1 FROM chunks WHERE embedding IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM workflow_examples WHERE embedding IS NOT NULL)
              AND NOT EXISTS (SELECT 1 FROM notes WHERE embedding IS NOT NULL);
            IF FOUND THEN
                -- Keep the vector columns in lockstep with the new dim, or the
                -- first embed write fails forever (nothing is stored, so
                -- USING NULL discards nothing).
                DROP INDEX IF EXISTS ix_chunks_embedding;
                ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(512) USING NULL;
                ALTER TABLE notes ALTER COLUMN embedding TYPE vector(512) USING NULL;
                ALTER TABLE workflow_examples ALTER COLUMN embedding TYPE vector(512) USING NULL;
                CREATE INDEX ix_chunks_embedding ON chunks
                    USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64);
            END IF;
        END $$;
        """
    )
    with op.batch_alter_table("embedding_state") as batch:
        batch.drop_column("threshold_floor")
        batch.drop_column("threshold_ceil")


def downgrade() -> None:
    # Restore 019's actual schema: NOT NULL with defaults 0.70/0.88.
    with op.batch_alter_table("embedding_state") as batch:
        batch.add_column(
            sa.Column("threshold_floor", sa.Float(), nullable=False, server_default="0.70")
        )
        batch.add_column(
            sa.Column("threshold_ceil", sa.Float(), nullable=False, server_default="0.88")
        )
