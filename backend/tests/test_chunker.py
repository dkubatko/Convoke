from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.memory.chunker import chunk_chat, segment_messages
from app.memory.embeddings import FakeEmbedder
from app.models import Bot, Chat, Chunk, ChunkState, Message

LULL = timedelta(minutes=30)
T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
# Roomy defaults so the pre-existing lull/size tests exercise their own rule,
# not the token budget.
BIG = 100_000
EMB = FakeEmbedder()


def flat_costs(messages, per: int = 10) -> dict[int, int]:
    return {m.tg_message_id: per for m in messages}


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
    segs = segment_messages(messages, flat_costs(messages), now, LULL, max_tokens=BIG, max_messages=24, overlap=0)
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 2), (3, 4)]


def test_active_tail_stays_unchunked():
    messages = [msg(1, 0), msg(2, 1), msg(3, 90), msg(4, 91)]
    now = T0 + timedelta(minutes=95)  # last burst still "hot"
    segs = segment_messages(messages, flat_costs(messages), now, LULL, max_tokens=BIG, max_messages=24, overlap=0)
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 2)]


def test_max_size_closes_segment():
    messages = [msg(i, i * 0.1) for i in range(1, 30)]
    now = T0 + timedelta(minutes=1)  # still active — only the size-full segment closes
    segs = segment_messages(messages, flat_costs(messages), now, LULL, max_tokens=BIG, max_messages=10, overlap=0)
    assert len(segs) == 2
    assert (segs[0].tg_id_start, segs[0].tg_id_end) == (1, 10)
    assert (segs[1].tg_id_start, segs[1].tg_id_end) == (11, 20)


def test_overlap_carries_context():
    messages = [msg(i, i * 0.1) for i in range(1, 21)]
    now = T0 + timedelta(minutes=1)
    segs = segment_messages(messages, flat_costs(messages), now, LULL, max_tokens=BIG, max_messages=10, overlap=3)
    assert len(segs[1].messages) == 13  # 10 + 3 overlap
    assert segs[1].tg_id_start == 11  # covered range excludes the overlap


def test_threads_segment_independently():
    messages = [msg(1, 0, thread=100), msg(2, 1, thread=200), msg(3, 2, thread=100)]
    now = T0 + timedelta(minutes=120)
    segs = segment_messages(messages, flat_costs(messages), now, LULL, max_tokens=BIG, max_messages=24, overlap=0)
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
        n = await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0)
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
        n = await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0)
        await s.commit()
    assert n == 1
    async with db_sessionmaker() as s:
        assert len((await s.execute(select(Chunk))).scalars().all()) == 2


async def test_chunk_chat_skips_unmonitored_thread(db_sessionmaker):
    from app.models import ChatThread

    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        # General thread (1,2) + an UNMONITORED thread 55 (3,4).
        for m in [msg(1, 0), msg(2, 1), msg(3, 2, thread=55), msg(4, 3, thread=55)]:
            m.chat_id = chat_id
            s.add(m)
        s.add(ChatThread(chat_id=chat_id, thread_key=55, monitored=False))
        await s.commit()

    now = T0 + timedelta(minutes=200)  # everything cold
    async with db_sessionmaker() as s:
        n = await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0)
        await s.commit()
    assert n == 1  # only the General-thread segment; thread 55 is ignored

    async with db_sessionmaker() as s:
        chunks = (await s.execute(select(Chunk))).scalars().all()
        assert len(chunks) == 1 and chunks[0].thread_id is None
        # The cursor still advanced past the unmonitored tail — not stuck at 2.
        assert (await s.get(ChunkState, chat_id)).last_tg_message_id == 4
        n = await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0)
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
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0) == 0
        await s.commit()

    now = T0 + timedelta(minutes=120)  # both threads cold now
    async with db_sessionmaker() as s:
        n = await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0)
        await s.commit()
    assert n == 2
    async with db_sessionmaker() as s:
        state = await s.get(ChunkState, chat_id)
        assert state.last_tg_message_id == 3


async def test_interleaved_closed_segment_is_not_lost_behind_cursor(db_sessionmaker):
    """Forum threads interleave in tg-id space: a closed segment held back by
    another thread's active tail may START below the safe cursor point. The
    cursor must clamp under it so its early messages are re-covered on the
    next pass — not stranded behind the cursor forever."""
    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        # thread 2: one conversation spanning ids 10-14 and 30-32 (gaps < lull)
        rows = [msg(mid, i, thread=2) for i, mid in enumerate(range(10, 15))]
        rows += [msg(mid, 10 + i, thread=2) for i, mid in enumerate(range(30, 33))]
        # thread 1: ACTIVE tail at ids 20-22, wedged between thread 2's halves
        rows += [msg(mid, 117 + i, thread=1) for i, mid in enumerate(range(20, 23))]
        for m in rows:
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    # pass 1: thread 2's segment [10..32] is closed but held back by thread 1's
    # hot tail — the cursor must stay below id 10, not land at 19.
    now = T0 + timedelta(minutes=120)
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 50, 0) == 0
        await s.commit()
        assert (await s.get(ChunkState, chat_id)).last_tg_message_id < 10

    # pass 2: everything cold — both segments close, covering every message.
    now = T0 + timedelta(minutes=240)
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 50, 0) == 2
        await s.commit()

    async with db_sessionmaker() as s:
        chunks = (await s.execute(select(Chunk))).scalars().all()
        for m in rows:  # every message lands in exactly one chunk of its thread
            covering = [
                c for c in chunks
                if c.thread_id == m.thread_id
                and c.msg_tg_id_start <= m.tg_message_id <= c.msg_tg_id_end
            ]
            assert len(covering) == 1, f"message {m.tg_message_id} in {len(covering)} chunks"


async def test_full_batch_does_not_force_lull_close(db_sessionmaker):
    """When the fetch batch is full, the batch-final message is not chat-final:
    the silence rule must not cut a segment at the batch edge. The tail stays
    unclosed and joins the rest of its conversation on a later pass."""
    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        # burst A (ids 1-2), lull, burst B (ids 3-7) — one conversation each
        for m in [msg(1, 0), msg(2, 1)] + [msg(i, 97 + i) for i in range(3, 8)]:
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    now = T0 + timedelta(minutes=300)  # everything long cold
    async with db_sessionmaker() as s:
        # limit=4 truncates mid-burst-B: only burst A may close, not [3,4]
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0, limit=4) == 1
        await s.commit()
        chunks = (await s.execute(select(Chunk))).scalars().all()
        assert [(c.msg_tg_id_start, c.msg_tg_id_end) for c in chunks] == [(1, 2)]

    async with db_sessionmaker() as s:
        # the untruncated pass closes burst B whole — no boundary at id 4
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0) == 1
        await s.commit()
        chunks = (await s.execute(select(Chunk).order_by(Chunk.id))).scalars().all()
        assert [(c.msg_tg_id_start, c.msg_tg_id_end) for c in chunks] == [(1, 2), (3, 7)]


def test_render_thread_quotes_out_of_transcript_reply_targets():
    """A reply whose target is off-screen gets the quoted original appended;
    a reply to a visible message gets a pure pointer; a reply to a message
    never stored keeps the id."""
    from app.memory.chunker import render_thread

    old = msg(1, 0, text="wanna hike Saturday at 10?")
    a = msg(10, 60, text="cool!")
    a.reply_to_tg_message_id = 1  # target NOT in this transcript
    b = msg(11, 61, text="same!")
    b.reply_to_tg_message_id = 10  # target IS in this transcript
    c = msg(12, 62, text="what was that about?")
    c.reply_to_tg_message_id = 7  # target never stored

    text = render_thread([a, b, c], {1: old, 10: a}, {})
    assert '↳ replies to [#1] [2026-07-01 12:00 UTC] Alice: "wanna hike Saturday at 10?"' in text
    assert text.count("↳") == 1  # the in-transcript reply is NOT expanded
    assert "(replying to #10)" in text  # …it is a pointer instead
    assert "(replying to #7 — message not stored)" in text
    assert "#10:" in text  # every line carries its real Telegram id


# --- token budget: the rule that keeps chunks inside the encoder's window ---


def test_token_budget_closes_segment():
    # 6 rapid messages, 10 tokens each, budget 30 → close every 3 messages.
    messages = [msg(i, i * 0.1) for i in range(1, 7)]
    now = T0 + timedelta(minutes=200)
    segs = segment_messages(
        messages, flat_costs(messages), now, LULL, max_tokens=30, max_messages=24, overlap=0
    )
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 3), (4, 6)]


def test_token_budget_counts_overlap_and_trims_it():
    # Budget 30, overlap 2: the carried tail (20 tokens) leaves room for only
    # one 10-token message per later segment...
    messages = [msg(i, i * 0.1) for i in range(1, 7)]
    now = T0 + timedelta(minutes=200)
    segs = segment_messages(
        messages, flat_costs(messages), now, LULL, max_tokens=30, max_messages=24, overlap=2
    )
    for s in segs:
        total = sum(10 for _ in s.messages)
        assert total <= 30  # overlap included in the budget
    covered = [(s.tg_id_start, s.tg_id_end) for s in segs]
    assert covered[0] == (1, 3)
    # every message is covered exactly once despite the overlap prefixes
    flat = [m for a, b in covered for m in range(a, b + 1)]
    assert flat == list(range(1, 7))


def test_oversized_single_message_gets_its_own_segment():
    messages = [msg(1, 0), msg(2, 0.1), msg(3, 0.2)]
    costs = {1: 10, 2: 500, 3: 10}  # message 2 alone exceeds the budget
    now = T0 + timedelta(minutes=200)
    segs = segment_messages(messages, costs, now, LULL, max_tokens=50, max_messages=24, overlap=0)
    assert [(s.tg_id_start, s.tg_id_end) for s in segs] == [(1, 1), (2, 2), (3, 3)]


def test_chunk_token_budget_clamps_to_model_window():
    from types import SimpleNamespace

    from app.memory.chunker import chunk_token_budget

    settings = SimpleNamespace(chunk_target_tokens=512)
    assert chunk_token_budget(settings, None) == 512
    assert chunk_token_budget(settings, SimpleNamespace(max_tokens=0)) == 512  # unknown window
    assert chunk_token_budget(settings, SimpleNamespace(max_tokens=128)) == 128  # distiluse-style clamp
    assert chunk_token_budget(settings, SimpleNamespace(max_tokens=8192)) == 512  # setting wins


# --- bot lines: tagged [bot] in every render, absent from embedding input ---


def _bot_msg(tg_id: int, minutes: float, text: str) -> Message:
    m = msg(tg_id, minutes, text)
    m.source = "self"
    return m


def test_bot_lines_are_tagged_and_strippable():
    from app.memory.chunker import render_thread

    human = msg(1, 0, text="когда свадьба?")
    bot = _bot_msg(2, 1, text="Event 'Свадьба' created for Oct 19")
    other_bot = msg(3, 2, text="card roll: Waterfall")
    other_bot.sender_id = 777  # flagged member, not source='self'

    full = render_thread([human, bot, other_bot], {}, {}, bot_ids=frozenset({777}))
    assert "[bot] Alice" in full  # both bot kinds tagged
    assert full.count("[bot]") == 2
    assert "когда свадьба?" in full and "#1:" in full  # human line untagged
    assert not full.splitlines()[0].startswith("[bot]")

    stripped = render_thread([human, bot, other_bot], {}, {}, bot_ids=frozenset({777}),
                             strip_bots=True)
    assert "когда свадьба?" in stripped
    assert "Свадьба' created" not in stripped and "Waterfall" not in stripped
    assert "[bot]" not in stripped


def test_reply_quote_tags_bot_targets():
    from app.memory.chunker import render_thread

    bot = _bot_msg(1, 0, text="I created the event")
    reply = msg(10, 60, text="thanks!")
    reply.reply_to_tg_message_id = 1  # target off-transcript -> quoted
    text = render_thread([reply], {1: bot}, {})
    assert '↳ replies to [#1] [2026-07-01 12:00 UTC] [bot] Alice: "I created the event"' in text


async def test_embed_input_excludes_bot_lines(db_sessionmaker):
    """The stored chunk text keeps bot lines; the vector is computed from the
    human-only render (verified via the deterministic FakeEmbedder)."""
    from app.memory.store import embed_pending_chunks, render_chunk_from_raw

    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        h1 = msg(1, 0, text="привет, когда встречаемся?")
        b = _bot_msg(2, 1, text="я не нашёл информации об этом")
        h2 = msg(3, 2, text="в субботу в 10")
        for m in (h1, b, h2):
            m.chat_id = chat_id
            s.add(m)
        await s.commit()

    now = T0 + timedelta(minutes=200)
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0) == 1
        await s.commit()

    async with db_sessionmaker() as s:
        n = await embed_pending_chunks(s, EMB, 8)
        assert n == 1
        chunk = (await s.execute(select(Chunk))).scalar_one()
        # stored text: everything, bot line tagged
        assert "не нашёл" in chunk.text and "[bot]" in chunk.text
        # vector matches the human-only render, not the full text
        stripped = await render_chunk_from_raw(s, chunk, strip_bots=True)
        assert "не нашёл" not in stripped
        assert chunk.embedding == (await EMB.embed_passages([stripped]))[0]
        assert chunk.embedding != (await EMB.embed_passages([chunk.text]))[0]


async def test_bot_scoring_setting_restores_full_text_embedding(db_sessionmaker):
    """memory_ignore_bot_messages=0 embeds the full render (bot lines included)."""
    from app.memory.store import embed_pending_chunks
    from app.models import RuntimeSetting

    chat_id = await _setup_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        h = msg(1, 0, text="привет")
        b = _bot_msg(2, 1, text="я бот и я отвечаю")
        for m in (h, b):
            m.chat_id = chat_id
            s.add(m)
        s.add(RuntimeSetting(key="memory_ignore_bot_messages", value=0))
        await s.commit()

    now = T0 + timedelta(minutes=200)
    async with db_sessionmaker() as s:
        assert await chunk_chat(s, chat_id, EMB, now, LULL, BIG, 24, 0) == 1
        await s.commit()
    async with db_sessionmaker() as s:
        assert await embed_pending_chunks(s, EMB, 8) == 1
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.embedding == (await EMB.embed_passages([chunk.text]))[0]


def test_render_ts_labels_and_converts(monkeypatch):
    """Timestamps carry their timezone; a non-UTC override converts values
    (03:00 UTC = previous-day 20:00 Pacific — the day-boundary case that
    misled the agent live)."""
    from zoneinfo import ZoneInfo

    import app.core.config as config
    from app.memory.chunker import render_ts

    assert render_ts(datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)) == "2026-07-11 03:00 UTC"
    # naive (sqlite) values are UTC by contract
    assert render_ts(datetime(2026, 7, 11, 3, 0)) == "2026-07-11 03:00 UTC"

    monkeypatch.setattr(config, "get_tzinfo", lambda: ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr("app.memory.chunker.get_tzinfo", lambda: ZoneInfo("America/Los_Angeles"))
    assert render_ts(datetime(2026, 7, 11, 3, 0, tzinfo=timezone.utc)) == "2026-07-10 20:00 PDT"
    assert render_ts(datetime(2026, 1, 11, 3, 0, tzinfo=timezone.utc)) == "2026-01-10 19:00 PST"
