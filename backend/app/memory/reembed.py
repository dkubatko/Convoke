"""Worker loop: swap an embedding model and rebuild that role's vectors.

Triggered by the API setting embedding_state.status='reembedding' on a role's
row. The vector dimension is runtime data (the operator's model choice), so
the job runs the DDL itself — the singleton-locked worker makes that
race-free, and every step is idempotent: a crash mid-job re-enters on restart
because eligibility is `embedding IS NULL`.

Roles rebuild independently:

- intent → workflow_examples. Examples re-embed in seconds and thresholds
  recalibrate; while nulled, load_positive_vectors returns [] and windows go
  straight to the cheap classifier (recall preserved).

- memory → chunks + notes. History is RE-CHUNKED first: chunk boundaries are
  cut against the model's own tokenizer and probed input window (plus the
  operator's chunk-size setting), so a model swap — or a chunk-size change
  "rebuilt" by swapping to the same model — always re-cuts. While running,
  memory search returns empty and the memory loop pauses; degradation is
  deliberate and safe.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.runtime_settings import effective_settings
from app.intent.examples import calibrate_threshold
from app.memory.chunker import chunk_chat, chunk_token_budget
from app.memory.embeddings import ROLES, LocalEmbedder
from app.memory.runtime import EmbedderHandle, embedding_state_for, spec_from_state
from app.memory.store import embed_pending_chunks
from app.models import (
    Chat,
    Chunk,
    ChunkState,
    EmbeddingState,
    Note,
    Workflow,
    WorkflowExample,
)

log = logging.getLogger("convoke.reembed")

TICK_S = 3

_ROLE_TABLES = {"intent": ("workflow_examples",), "memory": ("chunks", "notes")}


class ReembedJob:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], handles: dict[str, EmbedderHandle]
    ) -> None:
        self.sessionmaker = sessionmaker
        self.handles = handles
        self.settings = get_settings()

    async def run(self) -> None:
        while True:
            for role in ROLES:
                await self._tick(role)
            await asyncio.sleep(TICK_S)

    async def _tick(self, role: str) -> None:
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            if state is None or state.status != "reembedding":
                return
        try:
            await self._run_job(role)
        except Exception as e:  # noqa: BLE001 — the loop must survive
            # Transient failure mid-job (model hub hiccup, DB blip): keep
            # status='reembedding' so the next tick RESUMES — every step is
            # IS-NULL-idempotent. Reverting here would strand nulled vectors
            # with no mechanism to finish. Only the pre-mutation probe failure
            # (handled inside _run_job) terminates the job.
            log.exception("re-embed attempt failed (%s); will retry", role)
            await self._note_error(role, f"{type(e).__name__}: {str(e)[:200]} — retrying")

    async def _run_job(self, role: str) -> None:
        # 1. Load/probe the TARGET model before touching anything — a bad
        # custom id must fail without poisoning the live config. (A resumed
        # job after a crash has target=None: the current fields are already
        # the proven new model.)
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            target = state.target
            spec = _spec_from_target(target) if target else spec_from_state(state)
            await self._phase(session, state, "loading model")
        new_embedder = self._build_embedder(spec)
        try:
            dim, window = await new_embedder.probe_limits()
        except Exception as e:  # noqa: BLE001 — surface on the state row
            new_embedder.shutdown()
            await self._record_error(role, f"model failed to load: {type(e).__name__}: {str(e)[:200]}")
            return
        if spec.dim is not None and dim != spec.dim:
            new_embedder.shutdown()
            await self._record_error(role, f"expected {spec.dim} dimensions, model produces {dim}")
            return

        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            # The target proved loadable: it becomes the current config.
            state.model_id = spec.id
            state.doc_prefix = spec.doc_prefix
            state.query_prefix = spec.query_prefix
            state.target = None
            state.dim = dim
            state.max_tokens = window
            state.error = None
            state.started_at = state.started_at or datetime.now(timezone.utc)
            # 2. Re-type the role's vector columns (nulls everything).
            # Postgres only — sqlite's JSON variant is dim-agnostic.
            await self._phase(session, state, "resizing vector columns")
            if session.bind.dialect.name == "postgresql":
                if role == "memory":
                    await session.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding"))
                for table in _ROLE_TABLES[role]:
                    await session.execute(
                        text(
                            f"ALTER TABLE {table} ALTER COLUMN embedding "
                            f"TYPE vector({dim}) USING NULL"
                        )
                    )
            else:
                for table in _ROLE_TABLES[role]:
                    await session.execute(text(f"UPDATE {table} SET embedding = NULL"))
            await session.commit()

        # 3. Swap the live handle — every loop now embeds this role with the
        # new model.
        self.handles[role].replace(new_embedder)

        if role == "intent":
            await self._rebuild_intent()
        else:
            await self._rebuild_memory()

        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            state.status = "ready"
            state.phase = None
            state.error = None  # clear any retry notes from transient failures
            state.finished_at = datetime.now(timezone.utc)
            await session.commit()
        log.info("re-embed complete: %s → %s (%dd, %d-token window)", role, spec.id, dim, window)

    async def _rebuild_intent(self) -> None:
        """Workflow examples + threshold recalibration — small and fast."""
        handle = self.handles["intent"]
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, "intent")
            await self._phase(session, state, "re-embedding workflow examples")
            permissiveness = (
                await effective_settings(session, self.settings)
            ).intent_prefilter_permissiveness
            wf_ids = (
                (await session.execute(select(Workflow.id).where(Workflow.type == "intent")))
                .scalars()
                .all()
            )
            for wf_id in wf_ids:
                rows = (
                    (
                        await session.execute(
                            select(WorkflowExample).where(WorkflowExample.workflow_id == wf_id)
                        )
                    )
                    .scalars()
                    .all()
                )
                if not rows:
                    continue
                vecs = await handle.embed_passages([r.text for r in rows])
                for row, vec in zip(rows, vecs):
                    row.embedding = vec
                wf = await session.get(Workflow, wf_id)
                wf.threshold = calibrate_threshold(
                    [r.embedding for r in rows if r.kind == "positive"],
                    [r.embedding for r in rows if r.kind == "negative"],
                    permissiveness=permissiveness,
                )
            await session.commit()

    async def _rebuild_memory(self) -> None:
        """Re-cut history against the new tokenizer/budget, then re-embed."""
        handle = self.handles["memory"]

        # 4. Re-chunk. Old boundaries were cut by a different tokenizer (or
        # budget), so they are meaningless now — drop and re-cut. Idempotent:
        # a crash mid-way re-enters here and re-cuts from scratch.
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, "memory")
            await self._phase(session, state, "re-chunking history")
            effective = await effective_settings(session, self.settings)
            budget = chunk_token_budget(effective, state)
            lull = timedelta(seconds=effective.chunk_lull_seconds)
            await session.execute(delete(Chunk))
            await session.execute(update(ChunkState).values(last_tg_message_id=0))
            chat_ids = (
                await session.execute(select(Chat.id).where(Chat.status == "authorized"))
            ).scalars().all()
            await session.commit()
        now = datetime.now(timezone.utc)
        for chat_id in chat_ids:
            while True:
                async with self.sessionmaker() as session:
                    before = await session.get(ChunkState, chat_id)
                    cursor = before.last_tg_message_id if before else 0
                    await chunk_chat(
                        session,
                        chat_id,
                        handle,
                        now,
                        lull,
                        budget,
                        effective.chunk_max_messages,
                        effective.chunk_overlap_messages,
                    )
                    after = await session.get(ChunkState, chat_id)
                    moved = after is not None and after.last_tg_message_id > cursor
                    await session.commit()
                if not moved:
                    break

        # 5. Chunks, with progress. Everything is `embedding IS NULL` after
        # the re-cut; content_version still guards racing edits.
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, "memory")
            state.total = (
                await session.execute(
                    select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))
                )
            ).scalar_one()
            state.done = 0
            await self._phase(session, state, "re-embedding chunks")
        # Smaller batches than the memory loop's: `done` only advances per
        # committed batch, and on CPU a heavy model takes minutes per large
        # batch — 0/N the whole time reads as a hang (observed live).
        batch = min(self.settings.embedding_batch_size, 8)
        while True:
            async with self.sessionmaker() as session:
                n = await embed_pending_chunks(session, handle, batch)
                if n:
                    state = await embedding_state_for(session, "memory")
                    state.done += n
                await session.commit()
            if n < batch:
                break

        # 6. Notes backfill.
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, "memory")
            await self._phase(session, state, "re-embedding notes")
            notes = (
                (
                    await session.execute(
                        select(Note).where(Note.embedding.is_(None), Note.deleted.is_(False))
                    )
                )
                .scalars()
                .all()
            )
            if notes:
                vecs = await handle.embed_passages([n.text for n in notes])
                for note, vec in zip(notes, vecs):
                    note.embedding = vec
            await session.commit()

        # 7. Rebuild the HNSW index at the end — bulk inserts shouldn't pay
        # per-row index maintenance.
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, "memory")
            await self._phase(session, state, "rebuilding search index")
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks "
                        "USING hnsw (embedding vector_cosine_ops) "
                        "WITH (m = 16, ef_construction = 64)"
                    )
                )
            await session.commit()

    def _build_embedder(self, spec) -> LocalEmbedder:
        """Seam: tests substitute a fake (no ProcessPool, no downloads)."""
        return LocalEmbedder(spec, batch_size=self.settings.embedding_batch_size)

    @staticmethod
    async def _phase(session: AsyncSession, state: EmbeddingState, phase: str) -> None:
        state.phase = phase
        await session.commit()
        log.info("re-embed (%s): %s", state.role, phase)

    async def _note_error(self, role: str, message: str) -> None:
        """Surface a retryable failure on the row without ending the job."""
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            if state is not None and state.status == "reembedding":
                state.error = message
                await session.commit()

    async def _record_error(self, role: str, message: str) -> None:
        """Terminal: the target model can't load — abandon the swap. Called
        only before anything was mutated, so the current config stays valid."""
        async with self.sessionmaker() as session:
            state = await embedding_state_for(session, role)
            if state is not None:
                state.status = "ready"
                state.target = None  # abandoned; current config untouched
                state.phase = None
                state.error = message
                state.finished_at = datetime.now(timezone.utc)
                await session.commit()


def _spec_from_target(target: dict):
    from app.memory.embeddings import EmbeddingModelSpec

    return EmbeddingModelSpec(
        id=target["model_id"],
        label=target.get("label", target["model_id"]),
        dim=target.get("dim") or None,
        doc_prefix=target.get("doc_prefix", ""),
        query_prefix=target.get("query_prefix", ""),
    )
