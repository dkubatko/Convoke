"""Worker loop: swap the embedding model and rebuild every stored vector.

Triggered by the API setting embedding_state.status='reembedding'. The vector
dimension is runtime data (the operator's model choice), so the job runs the
DDL itself — the singleton-locked worker makes that race-free, and every step
is idempotent: a crash mid-job re-enters on restart because eligibility is
`embedding IS NULL`.

Degradation while running is deliberate and safe: nulled example embeddings
mean load_positive_vectors returns [] and windows go straight to the cheap
classifier (recall preserved); memory/note search returns empty; the memory
loop pauses its embed phase. Examples re-embed FIRST so the prefilter is back
within seconds; chunks follow with progress for the UI.
"""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.intent.examples import calibrate_threshold
from app.memory.embeddings import LocalEmbedder
from app.memory.runtime import EmbedderHandle, spec_from_state
from app.memory.store import embed_pending_chunks
from app.models import Chunk, EmbeddingState, Note, Workflow, WorkflowExample

log = logging.getLogger("convoke.reembed")

TICK_S = 3

_VECTOR_TABLES = ("chunks", "notes", "workflow_examples")


class ReembedJob:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], handle: EmbedderHandle
    ) -> None:
        self.sessionmaker = sessionmaker
        self.handle = handle
        self.settings = get_settings()

    async def run(self) -> None:
        while True:
            await self._tick()
            await asyncio.sleep(TICK_S)

    async def _tick(self) -> None:
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            if state is None or state.status != "reembedding":
                return
        try:
            await self._run_job()
        except Exception as e:  # noqa: BLE001 — the loop must survive
            # Transient failure mid-job (model hub hiccup, DB blip): keep
            # status='reembedding' so the next tick RESUMES — every step is
            # IS-NULL-idempotent. Reverting here would strand nulled vectors
            # with no mechanism to finish. Only the pre-mutation probe failure
            # (handled inside _run_job) terminates the job.
            log.exception("re-embed attempt failed; will retry")
            await self._note_error(f"{type(e).__name__}: {str(e)[:200]} — retrying")

    async def _run_job(self) -> None:
        # 1. Load/probe the TARGET model before touching anything — a bad
        # custom id must fail without poisoning the live config. (A resumed
        # job after a crash has target=None: the current fields are already
        # the proven new model.)
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            target = state.target
            spec = _spec_from_target(target) if target else spec_from_state(state)
            await self._phase(session, state, "loading model")
        new_embedder = self._build_embedder(spec)
        try:
            dim = await new_embedder.probe_dim()
        except Exception as e:  # noqa: BLE001 — surface on the state row
            new_embedder.shutdown()
            await self._record_error(f"model failed to load: {type(e).__name__}: {str(e)[:200]}")
            return
        if spec.dim is not None and dim != spec.dim:
            new_embedder.shutdown()
            await self._record_error(f"expected {spec.dim} dimensions, model produces {dim}")
            return

        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            # The target proved loadable: it becomes the current config.
            state.model_id = spec.id
            state.doc_prefix = spec.doc_prefix
            state.query_prefix = spec.query_prefix
            state.threshold_floor = spec.threshold_floor
            state.threshold_ceil = spec.threshold_ceil
            state.target = None
            state.dim = dim
            state.error = None
            state.started_at = state.started_at or datetime.now(timezone.utc)
            # 2. Re-type the vector columns (nulls everything). Postgres only —
            # sqlite's JSON variant is dim-agnostic.
            await self._phase(session, state, "resizing vector columns")
            if session.bind.dialect.name == "postgresql":
                await session.execute(text("DROP INDEX IF EXISTS ix_chunks_embedding"))
                for table in _VECTOR_TABLES:
                    await session.execute(
                        text(
                            f"ALTER TABLE {table} ALTER COLUMN embedding "
                            f"TYPE vector({dim}) USING NULL"
                        )
                    )
            else:
                for table in _VECTOR_TABLES:
                    await session.execute(text(f"UPDATE {table} SET embedding = NULL"))
            await session.commit()

        # 3. Swap the live handle — every loop now embeds with the new model.
        self.handle.replace(new_embedder)

        # 4. Workflow examples first: smallest set, restores the prefilter.
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            await self._phase(session, state, "re-embedding workflow examples")
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
                vecs = await self.handle.embed_passages([r.text for r in rows])
                for row, vec in zip(rows, vecs):
                    row.embedding = vec
                wf = await session.get(Workflow, wf_id)
                wf.threshold = calibrate_threshold(
                    [r.embedding for r in rows if r.kind == "positive"],
                    [r.embedding for r in rows if r.kind == "negative"],
                    floor=spec.threshold_floor,
                    ceil=spec.threshold_ceil,
                )
            await session.commit()

        # 5. Chunks, with progress. `USING NULL` made every chunk eligible via
        # the existing `embedding IS NULL` predicate; content_version still
        # guards racing edits.
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
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
                n = await embed_pending_chunks(session, self.handle, batch)
                if n:
                    state = await session.get(EmbeddingState, 1)
                    state.done += n
                await session.commit()
            if n < batch:
                break

        # 6. Notes backfill.
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
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
                vecs = await self.handle.embed_passages([n.text for n in notes])
                for note, vec in zip(notes, vecs):
                    note.embedding = vec
            await session.commit()

        # 7. Rebuild the HNSW index at the end — bulk inserts shouldn't pay
        # per-row index maintenance.
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            await self._phase(session, state, "rebuilding search index")
            if session.bind.dialect.name == "postgresql":
                await session.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_chunks_embedding ON chunks "
                        "USING hnsw (embedding vector_cosine_ops) "
                        "WITH (m = 16, ef_construction = 64)"
                    )
                )
            state.status = "ready"
            state.phase = None
            state.error = None  # clear any retry notes from transient failures
            state.finished_at = datetime.now(timezone.utc)
            await session.commit()
        log.info("re-embed complete: %s (%dd)", spec.id, dim)

    def _build_embedder(self, spec) -> LocalEmbedder:
        """Seam: tests substitute a fake (no ProcessPool, no downloads)."""
        return LocalEmbedder(spec, batch_size=self.settings.embedding_batch_size)

    @staticmethod
    async def _phase(session: AsyncSession, state: EmbeddingState, phase: str) -> None:
        state.phase = phase
        await session.commit()
        log.info("re-embed: %s", phase)

    async def _note_error(self, message: str) -> None:
        """Surface a retryable failure on the row without ending the job."""
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
            if state is not None and state.status == "reembedding":
                state.error = message
                await session.commit()

    async def _record_error(self, message: str) -> None:
        """Terminal: the target model can't load — abandon the swap. Called
        only before anything was mutated, so the current config stays valid."""
        async with self.sessionmaker() as session:
            state = await session.get(EmbeddingState, 1)
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
        threshold_floor=target.get("threshold_floor", 0.45),
        threshold_ceil=target.get("threshold_ceil", 0.92),
    )
