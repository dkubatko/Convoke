"""Reconcile drifted schema: vector dims, chat_members NOT NULL, JSON -> JSONB.

1. Vector dimension repair. Migration 020 flipped a FRESH install's
   embedding_state to distiluse (512d) but left the vector columns created by
   003/004/006 at vector(384): the first embed write then fails forever.
   020 now retypes the columns itself, but any install created between 020 and
   this revision is stuck — so this migration re-checks: if the recorded
   `embedding_state.dim` differs from the columns' actual typmod, it retypes
   `chunks.embedding` / `notes.embedding` / `workflow_examples.embedding` to
   vector(dim) USING NULL, dropping/recreating the chunks HNSW index around the
   rewrite (the same DDL the operator re-embed job in app/memory/reembed.py
   runs). Nulled vectors are correct: the worker re-embeds anything with a NULL
   embedding. On a consistent database this is a no-op. The check runs
   server-side in a DO block so it also renders under `alembic upgrade --sql`.

2. `chat_members.created_at/updated_at` become NOT NULL. 023 originally
   created them nullable; it now creates them NOT NULL, but databases upgraded
   through the old 023 still have them nullable. Nulls (there should be none —
   both columns have a now() server default) are backfilled defensively.

3. `agent_runs.tool_calls` (022) and `connected_models.capabilities` (018)
   were created as plain json but the models declare JSONVariant (JSONB on
   Postgres): retype to jsonb so schema and models agree.

Postgres-only DDL throughout, like the rest of the chain — migrations run
against Postgres (tests use sqlite via create_all, not alembic).

Revision ID: 024
Revises: 023
Create Date: 2026-07-08

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "024"
down_revision: str | None = "023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Vector columns must match embedding_state.dim. For pgvector,
    # pg_attribute.atttypmod IS the declared dimension (-1 if unconstrained).
    op.execute(
        """
        DO $$
        DECLARE
            want integer;
            have integer;
            tbl  text;
        BEGIN
            SELECT dim INTO want FROM embedding_state WHERE id = 1;
            IF want IS NULL THEN
                RETURN;  -- no singleton: nothing to reconcile against
            END IF;
            FOREACH tbl IN ARRAY ARRAY['chunks', 'notes', 'workflow_examples'] LOOP
                SELECT atttypmod INTO have
                FROM pg_attribute
                WHERE attrelid = tbl::regclass
                  AND attname = 'embedding'
                  AND NOT attisdropped;
                IF have IS DISTINCT FROM want THEN
                    IF tbl = 'chunks' THEN
                        DROP INDEX IF EXISTS ix_chunks_embedding;
                    END IF;
                    EXECUTE format(
                        'ALTER TABLE %I ALTER COLUMN embedding TYPE vector(%s) USING NULL',
                        tbl, want
                    );
                    IF tbl = 'chunks' THEN
                        CREATE INDEX ix_chunks_embedding ON chunks
                            USING hnsw (embedding vector_cosine_ops)
                            WITH (m = 16, ef_construction = 64);
                    END IF;
                END IF;
            END LOOP;
        END $$;
        """
    )

    # 2. chat_members timestamps: backfill any nulls, then match the model
    # (and the current 023) with NOT NULL.
    op.execute("UPDATE chat_members SET created_at = now() WHERE created_at IS NULL")
    op.execute("UPDATE chat_members SET updated_at = now() WHERE updated_at IS NULL")
    op.alter_column(
        "chat_members",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )
    op.alter_column(
        "chat_members",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=False,
    )

    # 3. json -> jsonb, matching the models' JSONVariant. The capabilities
    # default is dropped/re-set explicitly: Postgres won't always auto-cast a
    # column default across the type change.
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN tool_calls TYPE jsonb USING tool_calls::jsonb"
    )
    op.execute("ALTER TABLE connected_models ALTER COLUMN capabilities DROP DEFAULT")
    op.execute(
        "ALTER TABLE connected_models ALTER COLUMN capabilities "
        "TYPE jsonb USING capabilities::jsonb"
    )
    op.execute(
        "ALTER TABLE connected_models ALTER COLUMN capabilities SET DEFAULT '{}'::jsonb"
    )


def downgrade() -> None:
    # The vector-dimension reconcile has no inverse: it restored consistency,
    # and downgrading must not re-break it.
    op.execute(
        "ALTER TABLE connected_models ALTER COLUMN capabilities DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE connected_models ALTER COLUMN capabilities "
        "TYPE json USING capabilities::json"
    )
    op.execute(
        "ALTER TABLE connected_models ALTER COLUMN capabilities SET DEFAULT '{}'::json"
    )
    op.execute(
        "ALTER TABLE agent_runs ALTER COLUMN tool_calls TYPE json USING tool_calls::json"
    )
    op.alter_column(
        "chat_members",
        "updated_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
    op.alter_column(
        "chat_members",
        "created_at",
        existing_type=sa.DateTime(timezone=True),
        existing_server_default=sa.func.now(),
        nullable=True,
    )
