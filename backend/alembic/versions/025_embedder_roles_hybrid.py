"""Embedder roles (intent | memory) + lexical search infrastructure.

1. `embedding_state` becomes one row per embedder ROLE. The existing
   singleton (id=1) is the intent gate's row (it owns workflow_examples —
   that is what its model was validated for). A second row (id=2,
   role='memory') now owns chunks + notes:

   - FRESH install (nothing embedded anywhere): the memory row seeds the
     memory registry default, multilingual-e5-base (768d), and chunks/notes
     are retyped to vector(768) in lockstep — same pattern as 020, and free
     because nothing is stored.
   - EXISTING deploy: the memory row copies the intent row's live config, so
     stored chunk/note vectors remain queryable unchanged. Memory retrieval
     only actually improves when the operator swaps the memory model on the
     Models page (which re-chunks + re-embeds); a migration can't do hours of
     CPU work.

   `max_tokens` records the model's probed input window (0 = unknown; the
   worker probes and backfills on startup). The chunker clamps its token
   budget to it so no chunk is silently truncated by the encoder again.

2. Lexical search channels for hybrid retrieval over `chunks.text`:
   pg_trgm + unaccent extensions, an IMMUTABLE unaccent wrapper (the raw
   function is STABLE and can't be indexed — standard PostgreSQL-wiki
   pattern), a GIN FTS expression index using the language-neutral 'simple'
   config, and a GIN trigram index for word-similarity matching.

Postgres-only DDL, like the rest of the chain (tests use sqlite create_all).

Revision ID: 025
Revises: 024
Create Date: 2026-07-09

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "025"
down_revision: str | None = "024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MEMORY_DEFAULT = "intfloat/multilingual-e5-base"


def upgrade() -> None:
    # --- 1. Role-keyed embedding_state ---
    op.add_column("embedding_state", sa.Column("role", sa.Text(), nullable=True))
    op.add_column(
        "embedding_state",
        sa.Column("max_tokens", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE embedding_state SET role = 'intent' WHERE id = 1")

    # Seed the memory row + (fresh installs only) retype its tables. One DO
    # block so the retype happens exactly when the fresh seed does.
    op.execute(
        f"""
        DO $$
        DECLARE
            fresh boolean;
        BEGIN
            SELECT NOT EXISTS (SELECT 1 FROM chunks WHERE embedding IS NOT NULL)
               AND NOT EXISTS (SELECT 1 FROM notes WHERE embedding IS NOT NULL)
            INTO fresh;
            IF fresh THEN
                INSERT INTO embedding_state
                    (id, role, model_id, dim, max_tokens, doc_prefix, query_prefix,
                     status, total, done)
                VALUES
                    (2, 'memory', '{MEMORY_DEFAULT}', 768, 0, 'passage: ', 'query: ',
                     'ready', 0, 0);
                DROP INDEX IF EXISTS ix_chunks_embedding;
                ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768) USING NULL;
                ALTER TABLE notes ALTER COLUMN embedding TYPE vector(768) USING NULL;
                CREATE INDEX ix_chunks_embedding ON chunks
                    USING hnsw (embedding vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64);
            ELSE
                -- Live vectors were written by the (previously shared) intent
                -- row's model: the memory row starts as its copy so search
                -- keeps working; the operator upgrades it from the UI.
                INSERT INTO embedding_state
                    (id, role, model_id, dim, max_tokens, doc_prefix, query_prefix,
                     status, total, done)
                SELECT 2, 'memory', model_id, dim, 0, doc_prefix, query_prefix,
                       'ready', 0, 0
                FROM embedding_state WHERE id = 1;
            END IF;
        END $$;
        """
    )
    op.alter_column("embedding_state", "role", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint("uq_embedding_state_role", "embedding_state", ["role"])

    # --- 2. Lexical channels for hybrid retrieval ---
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION f_unaccent(text) RETURNS text AS
        $$ SELECT public.unaccent('public.unaccent', $1) $$
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_fts ON chunks "
        "USING gin (to_tsvector('simple', f_unaccent(text)))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_chunks_text_trgm ON chunks "
        "USING gin (f_unaccent(text) gin_trgm_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_trgm")
    op.execute("DROP INDEX IF EXISTS ix_chunks_text_fts")
    op.execute("DROP FUNCTION IF EXISTS f_unaccent(text)")
    # Extensions stay: other objects may have grown to depend on them.
    op.execute("DELETE FROM embedding_state WHERE role = 'memory'")
    op.drop_constraint("uq_embedding_state_role", "embedding_state", type_="unique")
    op.drop_column("embedding_state", "max_tokens")
    op.drop_column("embedding_state", "role")
