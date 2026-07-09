"""Configurable embeddings: per-role registries/prefix behavior, the swap
job's orchestration per role (sqlite — DDL no-ops, JSON vectors are
dim-agnostic), and the API contract."""

from datetime import datetime, timezone

import pytest

from app.memory.embeddings import (
    INTENT_EMBEDDING_REGISTRY,
    MEMORY_EMBEDDING_REGISTRY,
    FakeEmbedder,
    custom_spec,
)
from app.memory.reembed import ReembedJob
from app.memory.runtime import EmbedderHandle
from app.models import (
    Bot,
    Chat,
    Chunk,
    EmbeddingState,
    Message,
    Note,
    Workflow,
    WorkflowExample,
)

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


class SwapFake(FakeEmbedder):
    """FakeEmbedder + the LocalEmbedder surface the job needs."""

    def __init__(self, dim: int = 384, fail_load: bool = False):
        super().__init__(dim)
        self.fail_load = fail_load
        from app.memory.embeddings import EmbeddingModelSpec

        self.spec = EmbeddingModelSpec(
            id="fake/model", label="fake", dim=None, doc_prefix="", query_prefix="",
        )

    async def probe_limits(self) -> tuple[int, int]:
        if self.fail_load:
            raise RuntimeError("no such model on the hub")
        return self.dim, self.max_tokens

    def shutdown(self) -> None:
        pass


def _handles(dim: int = 384) -> dict[str, EmbedderHandle]:
    return {"intent": EmbedderHandle(SwapFake(dim=dim)), "memory": EmbedderHandle(SwapFake(dim=dim))}


async def seed(db_sessionmaker, *, intent_target: dict | None = None, memory_target: dict | None = None):
    async with db_sessionmaker() as s:
        s.add(EmbeddingState(
            id=1, role="intent",
            model_id="sentence-transformers/distiluse-base-multilingual-cased-v2", dim=512,
            doc_prefix="", query_prefix="",
            status="reembedding" if intent_target is not None else "ready", target=intent_target,
        ))
        s.add(EmbeddingState(
            id=2, role="memory",
            model_id="intfloat/multilingual-e5-base", dim=768,
            doc_prefix="passage: ", query_prefix="query: ",
            status="reembedding" if memory_target is not None else "ready", target=memory_target,
        ))
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        for tg in (1, 2):
            s.add(Message(chat_id=chat.id, tg_message_id=tg, sender_name="A",
                          text=f"msg {tg}", sent_at=T0))
        s.add(Chunk(chat_id=chat.id, thread_id=None, msg_tg_id_start=1, msg_tg_id_end=2,
                    text="old render", embedding=[0.1] * 384, stale=False, content_version=0))
        s.add(Note(chat_id=chat.id, key="tz", text="timezone is PST", embedding=[0.2] * 384))
        wf = Workflow(name="movies", type="intent", action_prompt="rate",
                      trigger_prompt="movie mention", required_slots=[],
                      threshold=0.83, examples_status="ready")
        s.add(wf)
        await s.flush()
        s.add(WorkflowExample(workflow_id=wf.id, kind="positive",
                              text="wanna watch Moana?", embedding=[0.3] * 384))
        s.add(WorkflowExample(workflow_id=wf.id, kind="negative",
                              text="my TV is fixed", embedding=[0.4] * 384))
        await s.commit()
        return wf.id


TARGET = {"model_id": "fake/model", "dim": None, "doc_prefix": "", "query_prefix": ""}


async def test_intent_swap_reembeds_examples_and_recalibrates(db_sessionmaker, monkeypatch):
    wf_id = await seed(db_sessionmaker, intent_target=TARGET)
    fake = SwapFake(dim=768)
    job = ReembedJob(db_sessionmaker, _handles())
    monkeypatch.setattr(job, "_build_embedder", lambda spec: fake)

    await job._tick("intent")

    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "ready" and state.error is None
        assert state.model_id == "fake/model" and state.dim == 768
        assert state.max_tokens == 512  # probed window persisted
        assert state.target is None and state.finished_at is not None
        from sqlalchemy import select
        examples = (await s.execute(select(WorkflowExample))).scalars().all()
        assert all(e.embedding is not None and len(e.embedding) == 768 for e in examples)
        wf = await s.get(Workflow, wf_id)
        # Recalibrated off the re-embedded examples: one positive → the
        # permissive floor, and no longer the seeded 0.83.
        assert wf.threshold == pytest.approx(0.15)
        # Intent owns workflow_examples ONLY — memory vectors untouched.
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding is not None and len(chunk.embedding) == 384
        note = (await s.execute(select(Note))).scalar_one()
        assert note.embedding is not None and len(note.embedding) == 384
    assert job.handles["intent"].spec.id == "fake/model"  # live handle swapped


async def test_memory_swap_rechunks_and_reembeds_chunks_and_notes(db_sessionmaker, monkeypatch):
    wf_id = await seed(db_sessionmaker, memory_target=TARGET)
    fake = SwapFake(dim=768)
    job = ReembedJob(db_sessionmaker, _handles())
    monkeypatch.setattr(job, "_build_embedder", lambda spec: fake)

    await job._tick("memory")

    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 2)
        assert state.status == "ready" and state.error is None
        assert state.model_id == "fake/model" and state.dim == 768
        assert state.max_tokens == 512
        from sqlalchemy import select
        # History was re-cut with the new tokenizer: the seeded chunk (its
        # boundaries belong to the old model) is gone, replaced by a fresh
        # cut covering the same messages, embedded at the new dimension.
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.text != "old render"
        assert chunk.msg_tg_id_start == 1 and chunk.msg_tg_id_end == 2
        assert chunk.embedding is not None and len(chunk.embedding) == 768
        note = (await s.execute(select(Note))).scalar_one()
        assert note.embedding is not None and len(note.embedding) == 768
        # Memory owns chunks + notes ONLY — intent examples untouched.
        examples = (await s.execute(select(WorkflowExample))).scalars().all()
        assert all(len(e.embedding) == 384 for e in examples)
        wf = await s.get(Workflow, wf_id)
        assert wf.threshold == pytest.approx(0.83)
    assert job.handles["memory"].spec.id == "fake/model"


async def test_swap_job_failure_leaves_config_untouched(db_sessionmaker, monkeypatch):
    await seed(db_sessionmaker, memory_target=TARGET)
    job = ReembedJob(db_sessionmaker, _handles())
    monkeypatch.setattr(job, "_build_embedder", lambda spec: SwapFake(fail_load=True))

    await job._tick("memory")

    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 2)
        assert state.status == "ready"
        assert "no such model" in state.error
        assert state.model_id == "intfloat/multilingual-e5-base"  # never poisoned
        assert state.target is None
        from sqlalchemy import select
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding is not None and chunk.text == "old render"  # untouched


async def test_idle_job_does_nothing(db_sessionmaker, monkeypatch):
    await seed(db_sessionmaker)  # both roles ready
    job = ReembedJob(db_sessionmaker, _handles())

    def boom(spec):  # would explode if the job ran
        raise AssertionError("job ran while idle")

    monkeypatch.setattr(job, "_build_embedder", boom)
    await job._tick("intent")
    await job._tick("memory")


DISTILUSE = "sentence-transformers/distiluse-base-multilingual-cased-v2"
MPNET = "sentence-transformers/paraphrase-multilingual-mpnet-base-v2"
E5_BASE = "intfloat/multilingual-e5-base"
E5_SMALL = "intfloat/multilingual-e5-small"


def test_registries_and_custom_spec():
    assert INTENT_EMBEDDING_REGISTRY[DISTILUSE].dim == 512
    assert len(INTENT_EMBEDDING_REGISTRY) == 3
    assert MEMORY_EMBEDDING_REGISTRY[E5_BASE].dim == 768
    assert len(MEMORY_EMBEDDING_REGISTRY) == 4
    # Registries are role-disjoint by design: paraphrase models gate intent,
    # retrieval models search memory.
    assert not set(INTENT_EMBEDDING_REGISTRY) & set(MEMORY_EMBEDDING_REGISTRY)
    spec = custom_spec("someone/some-model")
    assert spec.dim is None and spec.doc_prefix == ""


@pytest.mark.parametrize("model_id", list(INTENT_EMBEDDING_REGISTRY))
def test_intent_prefixes_are_data(model_id):
    # The intent registry's paraphrase models need no prefixes; the fields
    # still exist as data so a custom/prefixed model can override them.
    spec = INTENT_EMBEDDING_REGISTRY[model_id]
    assert spec.doc_prefix == "" and spec.query_prefix == ""


def test_memory_e5_prefixes():
    # mE5 is trained with asymmetric prefixes — dropping them costs recall.
    for model_id in (E5_BASE, E5_SMALL):
        spec = MEMORY_EMBEDDING_REGISTRY[model_id]
        assert spec.doc_prefix == "passage: " and spec.query_prefix == "query: "


async def test_embeddings_api_contract(db_sessionmaker, client):
    async with db_sessionmaker() as s:
        s.add(EmbeddingState(id=1, role="intent", model_id=DISTILUSE, dim=512,
                             doc_prefix="", query_prefix=""))
        s.add(EmbeddingState(id=2, role="memory", model_id=E5_BASE, dim=768,
                             doc_prefix="passage: ", query_prefix="query: "))
        await s.commit()

    for role, model, registry_len in (("intent", DISTILUSE, 3), ("memory", E5_BASE, 4)):
        got = await client.get(f"/api/embeddings/{role}")
        assert got.status_code == 200
        body = got.json()
        assert body["role"] == role
        assert body["current"]["model_id"] == model
        assert len(body["registry"]) == registry_len

    assert (await client.get("/api/embeddings/nonsense")).status_code == 404

    switched = await client.post("/api/embeddings/intent/model", json={"model_id": MPNET})
    assert switched.status_code == 202
    assert switched.json()["current"]["status"] == "reembedding"
    assert switched.json()["current"]["target_model_id"] == MPNET

    again = await client.post("/api/embeddings/intent/model", json={"model_id": MPNET})
    assert again.status_code == 409

    # Roles rebuild independently: intent mid-swap doesn't block memory —
    # and re-POSTing the CURRENT model is the rebuild path.
    rebuild = await client.post("/api/embeddings/memory/model", json={"model_id": E5_BASE})
    assert rebuild.status_code == 202
    assert rebuild.json()["current"]["target_model_id"] == E5_BASE


async def test_transient_failure_resumes_instead_of_aborting(db_sessionmaker, monkeypatch):
    """A mid-job hiccup (after vectors were already nulled) must keep
    status='reembedding' so the next tick resumes — aborting would strand the
    system with no vectors and no way to finish."""
    wf_id = await seed(db_sessionmaker, intent_target=TARGET)

    class FlakyOnce(SwapFake):
        def __init__(self):
            super().__init__(dim=768)
            self.failed = False

        async def embed_passages(self, texts):
            if not self.failed:
                self.failed = True
                raise RuntimeError("hub timeout")
            return await super().embed_passages(texts)

    flaky = FlakyOnce()
    job = ReembedJob(db_sessionmaker, _handles())
    monkeypatch.setattr(job, "_build_embedder", lambda spec: flaky)

    await job._tick("intent")  # fails during example re-embed, AFTER vectors nulled
    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "reembedding"  # NOT aborted
        assert "hub timeout" in state.error

    await job._tick("intent")  # resumes and completes
    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "ready" and state.error is None
        wf = await s.get(Workflow, wf_id)
        assert wf.threshold == pytest.approx(0.15)


def test_vector_columns_are_dimensionless():
    """pgvector's SQLAlchemy type validates dimensions client-side on every
    bind — a typed Vector(384) rejects writes the moment the swap job re-types
    the DB columns (seen live: 'expected 384 dimensions, not 768'). The DB
    typmod owns the dimension; the ORM type must not."""
    from pgvector.sqlalchemy import Vector

    from app.models import Chunk, Note, WorkflowExample

    for model in (Chunk, Note, WorkflowExample):
        col_type = model.__table__.c.embedding.type
        assert isinstance(col_type, Vector)
        assert col_type.dim is None


def test_rrf_merge_fuses_ranks_not_scores():
    from app.memory.store import rrf_merge

    # Chunk 7 is mid-ranked in BOTH channels; 1 and 9 top one channel each.
    # Rank-consensus must beat a single first place: 7 wins.
    fused = rrf_merge([[1, 7, 3], [9, 7, 4]], k=60)
    top = sorted(fused, key=fused.get, reverse=True)
    assert top[0] == 7
    assert fused[7] == pytest.approx(2 / 62)
    assert fused[1] == pytest.approx(1 / 61)
    # Channels are independent: an id missing from one list just scores less.
    assert set(fused) == {1, 7, 3, 9, 4}
    assert rrf_merge([]) == {}
