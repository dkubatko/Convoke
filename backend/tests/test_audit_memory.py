"""Regression tests from the memory-layer audit: thread isolation in chunk
re-rendering, and embedder recovery from a dead subprocess."""
from concurrent.futures import Future
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import app.memory.embeddings as embeddings
from app.memory.chunker import chunk_chat
from app.memory.store import render_chunk_from_raw
from app.models import Bot, Chat, ChatThread, Chunk, Message

LULL = timedelta(minutes=30)
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def msg(tg_id: int, minutes: float, text: str, thread: int | None = None) -> Message:
    return Message(
        chat_id=1,
        tg_message_id=tg_id,
        thread_id=thread,
        sender_id=1,
        sender_name="Alice",
        text=text,
        sent_at=T0 + timedelta(minutes=minutes),
    )


async def _setup_chat(db_sessionmaker) -> int:
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.commit()
        return chat.id


async def test_render_chunk_excludes_interleaved_other_threads(db_sessionmaker):
    """render_chunk_from_raw selects by chunk id range — but threads interleave
    in tg-id space, so it must also filter by the chunk's thread or another
    thread's messages (including UNMONITORED ones) leak into search hits."""
    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(ChatThread(chat_id=chat_id, thread_key=9, monitored=False))
        rows = [msg(mid, i, f"public-{mid}", thread=2) for i, mid in enumerate([10, 13, 14])]
        rows += [msg(mid, i, f"SECRET-{mid}", thread=9) for i, mid in enumerate([11, 12])]
        for m in rows:
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    now = T0 + timedelta(minutes=200)  # everything cold
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, now, LULL, 24, 0) == 1
        await s.commit()

    async with db_sessionmaker() as s:
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert (chunk.msg_tg_id_start, chunk.msg_tg_id_end) == (10, 14)
        rendered = await render_chunk_from_raw(s, chunk)
        assert "SECRET" not in rendered
        for mid in (10, 13, 14):
            assert f"public-{mid}" in rendered


async def test_local_embedder_recovers_from_broken_pool(monkeypatch):
    """A dead embedding subprocess (e.g. OOM) breaks the pool for every later
    job; the embedder must dispose it, recreate, and retry once — not wedge
    all embedding until a manual restart."""
    pools: list = []

    class FakePool:
        def __init__(self, max_workers: int = 1) -> None:
            self.shut_down = False
            pools.append(self)

        def submit(self, fn, *args):
            f: Future = Future()
            if self is pools[0]:  # the first pool is permanently broken
                f.set_exception(BrokenProcessPool("child died"))
            else:
                f.set_result(fn(*args))
            return f

        def shutdown(self, wait: bool = True, cancel_futures: bool = False) -> None:
            self.shut_down = True

    monkeypatch.setattr(embeddings, "ProcessPoolExecutor", FakePool)
    monkeypatch.setattr(embeddings, "_encode", lambda model_name, texts: [[1.0, 0.0] for _ in texts])

    embedder = embeddings.LocalEmbedder(embeddings.custom_spec("fake-model"), batch_size=8)
    assert await embedder.embed_query("hello") == [1.0, 0.0]
    assert len(pools) == 2 and pools[0].shut_down  # broken pool replaced exactly once
