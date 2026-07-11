from dataclasses import dataclass

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.members import load_member_names
from app.memory.chunker import render_for_chat
from app.memory.embeddings import Embedder
from app.models import Chunk, Message

# Hybrid retrieval: three first-stage retrievers over the same chunks, fused
# by Reciprocal Rank Fusion. Dense vectors carry meaning and cross-lingual
# matches; the two lexical channels carry what dense famously misses — exact
# quotes, names, dates ("Даня, с днем рождения"). Language-agnostic on
# purpose: FTS uses the 'simple' config (no stemmer to pick per language) and
# pg_trgm word-similarity covers inflected forms and typos in any alphabetic
# script. RRF_K=60 is the standard constant from the original RRF paper.
RRF_K = 60
CHANNEL_POOL = 30  # candidates each channel contributes before fusion
# Explicit floor for the <% word-similarity match (the GUC default 0.6 is
# tuned for near-exact single words; multiword conversational queries land
# lower). Applied per-query via SET LOCAL, never globally.
TRGM_WORD_SIM_THRESHOLD = 0.3


@dataclass
class SearchHit:
    chunk_id: int
    score: float  # fused RRF score — comparable within one result list only
    rendered: str


def rrf_merge(rankings: list[list[int]], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion: id → Σ 1/(k + rank). Operates on ranks, so
    channels with incomparable raw scores (cosine vs ts_rank vs trigram)
    fuse without calibration."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


async def render_chunk_from_raw(
    session: AsyncSession,
    chunk: Chunk,
    names: dict[int, str] | None = None,
    bot_ids: frozenset[int] | None = None,
    strip_bots: bool = False,
) -> str:
    """Render a hit from raw message rows (not the embedded text blob) so edits
    are always reflected and speakers/timestamps are never stale. Delegates to
    render_for_chat for name + reply-linkage resolution; pass preloaded
    `names`/`bot_ids` maps when rendering many chunks of one chat.
    `strip_bots=True` yields the EMBEDDING-INPUT form (bot lines omitted)."""
    messages = (
        (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == chunk.chat_id,
                    # Threads interleave in tg-id space, so the id range alone
                    # would pull in other threads' messages (including
                    # unmonitored ones). Match the chunker's thread keying:
                    # NULL == 0 == the General/main thread.
                    func.coalesce(Message.thread_id, 0) == (chunk.thread_id or 0),
                    Message.tg_message_id >= chunk.msg_tg_id_start,
                    Message.tg_message_id <= chunk.msg_tg_id_end,
                )
                .order_by(Message.tg_message_id)
            )
        )
        .scalars()
        .all()
    )
    return await render_for_chat(
        session, chunk.chat_id, list(messages), names, bot_ids, strip_bots=strip_bots
    )


def _period_filter(after, before):
    """ORM EXISTS restricting chunks to those covering >=1 message inside
    [after, before]. Chunks carry no timestamps — their period is derived
    from the messages they cover (cheap at chunk counts; uses the message
    chat/id index)."""
    conds = [
        Message.chat_id == Chunk.chat_id,
        func.coalesce(Message.thread_id, 0) == func.coalesce(Chunk.thread_id, 0),
        Message.tg_message_id >= Chunk.msg_tg_id_start,
        Message.tg_message_id <= Chunk.msg_tg_id_end,
    ]
    if after is not None:
        conds.append(Message.sent_at >= after)
    if before is not None:
        conds.append(Message.sent_at <= before)
    return select(1).where(*conds).exists()


def _period_sql(after, before) -> tuple[str, dict]:
    """Raw-SQL twin of _period_filter for the textual lexical channels."""
    if after is None and before is None:
        return "", {}
    bounds, params = [], {}
    if after is not None:
        bounds.append("ms.sent_at >= :after")
        params["after"] = after
    if before is not None:
        bounds.append("ms.sent_at <= :before")
        params["before"] = before
    return (
        " AND EXISTS (SELECT 1 FROM messages ms "
        "WHERE ms.chat_id = chunks.chat_id "
        "AND coalesce(ms.thread_id, 0) = coalesce(chunks.thread_id, 0) "
        "AND ms.tg_message_id BETWEEN chunks.msg_tg_id_start AND chunks.msg_tg_id_end "
        f"AND {' AND '.join(bounds)})",
        params,
    )


async def _dense_ids(
    session: AsyncSession, embedder: Embedder, chat_id: int, query: str, after=None, before=None
) -> list[int]:
    qvec = await embedder.embed_query(query)
    stmt = (
        select(Chunk.id)
        .where(Chunk.chat_id == chat_id, Chunk.embedding.is_not(None))
        .order_by(Chunk.embedding.cosine_distance(qvec))
        .limit(CHANNEL_POOL)
    )
    if after is not None or before is not None:
        stmt = stmt.where(_period_filter(after, before))
    return list((await session.execute(stmt)).scalars())


async def _fts_ids(
    session: AsyncSession, chat_id: int, query: str, after=None, before=None
) -> list[int]:
    """Exact-token matches, language-neutral ('simple': no stemming) and
    accent-folded. websearch_to_tsquery is total on arbitrary user text."""
    period, params = _period_sql(after, before)
    return list(
        (
            await session.execute(
                text(
                    "SELECT id FROM chunks "
                    "WHERE chat_id = :chat_id AND to_tsvector('simple', f_unaccent(text)) "
                    "      @@ websearch_to_tsquery('simple', f_unaccent(:q))" + period +
                    " ORDER BY ts_rank(to_tsvector('simple', f_unaccent(text)), "
                    "                 websearch_to_tsquery('simple', f_unaccent(:q))) DESC "
                    "LIMIT :pool"
                ),
                {"chat_id": chat_id, "q": query, "pool": CHANNEL_POOL, **params},
            )
        ).scalars()
    )


async def _trgm_ids(
    session: AsyncSession, chat_id: int, query: str, after=None, before=None
) -> list[int]:
    """Fuzzy word-level matches: inflected forms ('день рождения' ↔ 'Днем
    Рождения'), typos, partial names. <% is index-backed; the threshold GUC
    is scoped to this transaction only."""
    # SET LOCAL can't take bind parameters; set_config(..., is_local=true) is
    # its function form — scoped to the enclosing transaction.
    await session.execute(
        text("SELECT set_config('pg_trgm.word_similarity_threshold', :t, true)"),
        {"t": str(TRGM_WORD_SIM_THRESHOLD)},
    )
    period, params = _period_sql(after, before)
    return list(
        (
            await session.execute(
                text(
                    "SELECT id FROM chunks "
                    "WHERE chat_id = :chat_id AND f_unaccent(:q) <% f_unaccent(text)" + period +
                    " ORDER BY word_similarity(f_unaccent(:q), f_unaccent(text)) DESC "
                    "LIMIT :pool"
                ),
                {"chat_id": chat_id, "q": query, "pool": CHANNEL_POOL, **params},
            )
        ).scalars()
    )


async def search_chat_history(
    session: AsyncSession,
    embedder: Embedder,
    chat_id: int,
    query: str,
    k: int = 6,
    after=None,
    before=None,
) -> list[SearchHit]:
    if session.bind.dialect.name != "postgresql":
        return []  # hybrid search is Postgres-only; unit tests hit this path
    from app.memory.runtime import embedding_state_for

    state = await embedding_state_for(session, "memory")
    if state is not None and state.status == "reembedding":
        return []  # vectors are being rebuilt; query dim may not match yet
    rankings = [
        await _dense_ids(session, embedder, chat_id, query, after, before),
        await _fts_ids(session, chat_id, query, after, before),
        await _trgm_ids(session, chat_id, query, after, before),
    ]
    fused = rrf_merge(rankings)
    top = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:k]
    if not top:
        return []
    chunks = {
        c.id: c
        for c in (
            await session.execute(select(Chunk).where(Chunk.id.in_([cid for cid, _ in top])))
        ).scalars()
    }
    names = await load_member_names(session, chat_id)  # one lookup for all hits
    return [
        SearchHit(
            chunk_id=cid,
            score=score,
            rendered=await render_chunk_from_raw(session, chunks[cid], names),
        )
        for cid, score in top
        if cid in chunks
    ]


async def mark_chunks_stale(session: AsyncSession, chat_id: int, tg_message_id: int) -> None:
    """Called on message edits: the covering chunk re-embeds on the next pass.
    Bumping content_version lets the embed loop detect an edit that races its
    in-flight embedding."""
    await session.execute(
        update(Chunk)
        .where(
            Chunk.chat_id == chat_id,
            Chunk.msg_tg_id_start <= tg_message_id,
            Chunk.msg_tg_id_end >= tg_message_id,
        )
        .values(stale=True, content_version=Chunk.content_version + 1)
    )


async def embed_pending_chunks(
    session: AsyncSession, embedder: Embedder, batch_size: int = 64
) -> int:
    """Embed new chunks and re-embed stale ones (rendering fresh from raw).

    What gets EMBEDDED is the bot-stripped render — memory search scores only
    human testimony (bot replies paraphrase queries and summarize facts, so
    scoring them lets the bot's own past chatter outrank the answers; measured
    live). The STORED text keeps bot lines, so retrieved chunks still show
    them and lexical search still matches them.

    Runs in its own session over seconds of CPU work, so a message edit can
    land mid-batch. The final clear is a guarded UPDATE that only writes when
    content_version is unchanged since read — otherwise the edited chunk stays
    stale and re-embeds next pass instead of being fixed with a stale vector.
    """
    from app.core.runtime_settings import effective_settings
    from app.members import load_bot_sender_ids

    chunks = (
        (
            await session.execute(
                select(Chunk)
                .where((Chunk.embedding.is_(None)) | (Chunk.stale.is_(True)))
                .limit(batch_size)
            )
        )
        .scalars()
        .all()
    )
    if not chunks:
        return 0
    strip = bool((await effective_settings(session)).memory_ignore_bot_messages)
    rendered: list[tuple[int, int, str | None, str]] = []  # (id, version, new_text, embed_input)
    names_by_chat: dict[int, dict[int, str]] = {}  # chunks may span chats
    bots_by_chat: dict[int, frozenset[int]] = {}
    for c in chunks:
        if c.chat_id not in names_by_chat:
            names_by_chat[c.chat_id] = await load_member_names(session, c.chat_id)
            bots_by_chat[c.chat_id] = frozenset(await load_bot_sender_ids(session, c.chat_id))
        names, bots = names_by_chat[c.chat_id], bots_by_chat[c.chat_id]
        # Stale = content changed (edit/rename/flag): refresh the stored text.
        new_text = await render_chunk_from_raw(session, c, names, bots) if c.stale else None
        embed_input = await render_chunk_from_raw(session, c, names, bots, strip_bots=strip)
        rendered.append((c.id, c.content_version, new_text, embed_input))
    vectors = await embedder.embed_passages([r[3] for r in rendered])

    written = 0
    for (chunk_id, version, new_text, _), vector in zip(rendered, vectors):
        values = {"embedding": vector, "stale": False}
        if new_text is not None:
            values["text"] = new_text
        result = await session.execute(
            update(Chunk)
            .where(Chunk.id == chunk_id, Chunk.content_version == version)
            .values(**values)
        )
        written += result.rowcount or 0
    await session.commit()
    return written
