"""Configurable embeddings: registry/prefix behavior, the swap job's
orchestration (sqlite — DDL no-ops, JSON vectors are dim-agnostic), and the
API contract."""

from datetime import datetime, timezone

import pytest

from app.memory.embeddings import EMBEDDING_REGISTRY, FakeEmbedder, custom_spec
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
            threshold_floor=0.4, threshold_ceil=0.9,
        )

    async def probe_dim(self) -> int:
        if self.fail_load:
            raise RuntimeError("no such model on the hub")
        return self.dim

    def shutdown(self) -> None:
        pass


async def seed(db_sessionmaker, *, target: dict | None):
    async with db_sessionmaker() as s:
        s.add(EmbeddingState(
            id=1, model_id="intfloat/multilingual-e5-small", dim=384,
            doc_prefix="passage: ", query_prefix="query: ",
            threshold_floor=0.70, threshold_ceil=0.88,
            status="reembedding" if target is not None else "ready", target=target,
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


TARGET = {"model_id": "fake/model", "dim": None, "doc_prefix": "", "query_prefix": "",
          "threshold_floor": 0.4, "threshold_ceil": 0.9}


async def test_swap_job_reembeds_everything_and_recalibrates(db_sessionmaker, monkeypatch):
    wf_id = await seed(db_sessionmaker, target=TARGET)
    fake = SwapFake(dim=768)
    job = ReembedJob(db_sessionmaker, EmbedderHandle(SwapFake(dim=384)))
    monkeypatch.setattr(job, "_build_embedder", lambda spec: fake)

    await job._tick()

    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "ready" and state.error is None
        assert state.model_id == "fake/model" and state.dim == 768
        assert state.target is None and state.finished_at is not None
        from sqlalchemy import select
        examples = (await s.execute(select(WorkflowExample))).scalars().all()
        assert all(e.embedding is not None and len(e.embedding) == 768 for e in examples)
        wf = await s.get(Workflow, wf_id)
        assert 0.4 <= wf.threshold <= 0.9  # recalibrated inside the new band
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding is not None and len(chunk.embedding) == 768
        note = (await s.execute(select(Note))).scalar_one()
        assert note.embedding is not None and len(note.embedding) == 768
    assert job.handle.spec.id == "fake/model"  # live handle swapped


async def test_swap_job_failure_leaves_config_untouched(db_sessionmaker, monkeypatch):
    await seed(db_sessionmaker, target=TARGET)
    job = ReembedJob(db_sessionmaker, EmbedderHandle(SwapFake(dim=384)))
    monkeypatch.setattr(job, "_build_embedder", lambda spec: SwapFake(fail_load=True))

    await job._tick()

    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "ready"
        assert "no such model" in state.error
        assert state.model_id == "intfloat/multilingual-e5-small"  # never poisoned
        assert state.target is None
        from sqlalchemy import select
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding is not None  # vectors untouched


async def test_idle_job_does_nothing(db_sessionmaker, monkeypatch):
    await seed(db_sessionmaker, target=None)  # status ready
    job = ReembedJob(db_sessionmaker, EmbedderHandle(SwapFake(dim=384)))

    def boom(spec):  # would explode if the job ran
        raise AssertionError("job ran while idle")

    monkeypatch.setattr(job, "_build_embedder", boom)
    await job._tick()


def test_registry_and_custom_spec():
    assert EMBEDDING_REGISTRY["intfloat/multilingual-e5-small"].dim == 384
    assert len(EMBEDDING_REGISTRY) == 5
    spec = custom_spec("someone/some-model")
    assert spec.dim is None and spec.doc_prefix == ""


@pytest.mark.parametrize("model_id,expect_prefix", [
    ("intfloat/multilingual-e5-small", "passage: "),
    ("BAAI/bge-m3", ""),
])
def test_prefixes_are_data(model_id, expect_prefix):
    assert EMBEDDING_REGISTRY[model_id].doc_prefix == expect_prefix


async def test_embeddings_api_contract(db_sessionmaker, client):
    async with db_sessionmaker() as s:
        s.add(EmbeddingState(id=1, model_id="intfloat/multilingual-e5-small", dim=384,
                             doc_prefix="passage: ", query_prefix="query: "))
        await s.commit()

    got = await client.get("/api/embeddings")
    assert got.status_code == 200
    body = got.json()
    assert body["current"]["model_id"] == "intfloat/multilingual-e5-small"
    assert len(body["registry"]) == 5

    switched = await client.post("/api/embeddings/model", json={"model_id": "BAAI/bge-m3"})
    assert switched.status_code == 202
    assert switched.json()["current"]["status"] == "reembedding"
    assert switched.json()["current"]["target_model_id"] == "BAAI/bge-m3"

    again = await client.post("/api/embeddings/model", json={"model_id": "BAAI/bge-m3"})
    assert again.status_code == 409


async def test_transient_failure_resumes_instead_of_aborting(db_sessionmaker, monkeypatch):
    """A mid-job hiccup (after vectors were already nulled) must keep
    status='reembedding' so the next tick resumes — aborting would strand the
    system with no vectors and no way to finish."""
    wf_id = await seed(db_sessionmaker, target=TARGET)

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
    job = ReembedJob(db_sessionmaker, EmbedderHandle(SwapFake(dim=384)))
    monkeypatch.setattr(job, "_build_embedder", lambda spec: flaky)

    await job._tick()  # fails during example re-embed, AFTER vectors nulled
    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "reembedding"  # NOT aborted
        assert "hub timeout" in state.error

    await job._tick()  # resumes and completes
    async with db_sessionmaker() as s:
        state = await s.get(EmbeddingState, 1)
        assert state.status == "ready" and state.error is None
        from sqlalchemy import select
        wf = await s.get(Workflow, wf_id)
        assert 0.4 <= wf.threshold <= 0.9
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding is not None and len(chunk.embedding) == 768


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
