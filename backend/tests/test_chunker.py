from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.memory.chunker import chunk_chat, segment_messages
from app.models import Bot, Chat, Chunk, ChunkState, Message

LULL = timedelta(minutes=30)
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def msg(tg_id: int, minutes: float, text: str = "hi", thread: int | None = None) -> Message:
    return Message(
        chat_id=1,
        tg_message_id=tg_id,
        thread_id=thread,
        sender_id=1,
        sender_name="Alice",
        text=text,
        sent_at=T0 + timedelta(minutes=minutes),
    )


def test_lull_splits_segments():
    messages = [msg(1, 0), msg(2, 1), msg(3, 90), msg(4, 91)]
    now = T0 + timedelta(minutes=200)
    segs = segment_messages(messages, now, LULL, max_messages=24, overlap=0)
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 2), (3, 4)]


def test_active_tail_stays_unchunked():
    messages = [msg(1, 0), msg(2, 1), msg(3, 90), msg(4, 91)]
    now = T0 + timedelta(minutes=95)  # last burst still "hot"
    segs = segment_messages(messages, now, LULL, max_messages=24, overlap=0)
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 2)]


def test_max_size_closes_segment():
    messages = [msg(i, i * 0.1) for i in range(1, 30)]
    now = T0 + timedelta(minutes=1)  # still active — only the size-full segment closes
    segs = segment_messages(messages, now, LULL, max_messages=10, overlap=0)
    assert len(segs) == 2
    assert (segs[0].tg_id_start, segs[0].tg_id_end) == (1, 10)
    assert (segs[1].tg_id_start, segs[1].tg_id_end) == (11, 20)


def test_overlap_carries_context():
    messages = [msg(i, i * 0.1) for i in range(1, 21)]
    now = T0 + timedelta(minutes=1)
    segs = segment_messages(messages, now, LULL, max_messages=10, overlap=3)
    assert len(segs[1].messages) == 13  # 10 + 3 overlap
    assert segs[1].tg_id_start == 11  # covered range excludes the overlap


def test_threads_segment_independently():
    messages = [msg(1, 0, thread=100), msg(2, 1, thread=200), msg(3, 2, thread=100)]
    now = T0 + timedelta(minutes=120)
    segs = segment_messages(messages, now, LULL, max_messages=24, overlap=0)
    assert {s.thread_id for s in segs} == {100, 200}


async def _setup_chat(db_sessionmaker):
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.commit()
        return chat.id


async def test_chunk_chat_advances_cursor(db_sessionmaker):
    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        for m in [msg(1, 0), msg(2, 1), msg(3, 90), msg(4, 91)]:
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    now = T0 + timedelta(minutes=95)
    async with db_sessionmaker() as s:
        n = await chunk_chat(s, chat_id, now, LULL, 24, 0)
        await s.commit()
    assert n == 1

    async with db_sessionmaker() as s:
        state = await s.get(ChunkState, chat_id)
        assert state.last_tg_message_id == 2
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert "Alice" in chunk.text

    # later, the second burst goes cold and gets chunked exactly once
    now = T0 + timedelta(minutes=200)
    async with db_sessionmaker() as s:
        n = await chunk_chat(s, chat_id, now, LULL, 24, 0)
        await s.commit()
    assert n == 1
    async with db_sessionmaker() as s:
        assert len((await s.execute(select(Chunk))).scalars().all()) == 2
        n = await chunk_chat(s, chat_id, now, LULL, 24, 0)
        assert n == 0  # idempotent


async def test_forum_cursor_does_not_skip_active_thread(db_sessionmaker):
    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        # thread 100: closed long ago; thread 200: active, with LOWER ids pending
        for m in [msg(1, 0, thread=200), msg(2, 1, thread=100), msg(3, 2, thread=100)]:
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    # thread 100 cold (closed), thread 200's message is recent relative to now?
    # No: all are old — but thread 200 keeps getting messages later.
    now = T0 + timedelta(minutes=20)  # nothing cold yet
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, now, LULL, 24, 0) == 0
        await s.commit()

    now = T0 + timedelta(minutes=120)  # both threads cold now
    async with db_sessionmaker() as s:
        n = await chunk_chat(s, chat_id, now, LULL, 24, 0)
        await s.commit()
    assert n == 2
    async with db_sessionmaker() as s:
        state = await s.get(ChunkState, chat_id)
        assert state.last_tg_message_id == 3
