from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.chunker import render_message
from app.memory.embeddings import Embedder
from app.models import Chunk, Message


@dataclass
class SearchHit:
    chunk_id: int
    distance: float
    rendered: str


async def render_chunk_from_raw(session: AsyncSession, chunk: Chunk) -> str:
    """Render a hit from raw message rows (not the embedded text blob) so
    edits are always reflected and speakers/timestamps are never stale."""
    messages = (
        (
            await session.execute(
                select(Message)
                .where(
                    Message.chat_id == chunk.chat_id,
                    Message.tg_message_id >= chunk.msg_tg_id_start,
                    Message.tg_message_id <= chunk.msg_tg_id_end,
                )
                .order_by(Message.tg_message_id)
            )
        )
        .scalars()
        .all()
    )
    return "\n".join(render_message(m) for m in messages)


async def search_chat_history(
    session: AsyncSession, embedder: Embedder, chat_id: int, query: str, k: int = 6
) -> list[SearchHit]:
    if session.bind.dialect.name != "postgresql":
        return []  # vector search is Postgres-only; unit tests hit this path
    qvec = await embedder.embed_query(query)
    rows = (
        await session.execute(
            select(Chunk, Chunk.embedding.cosine_distance(qvec).label("dist"))
            .where(Chunk.chat_id == chat_id, Chunk.embedding.is_not(None))
            .order_by(Chunk.embedding.cosine_distance(qvec))
            .limit(k)
        )
    ).all()
    return [
        SearchHit(chunk_id=chunk.id, distance=float(dist), rendered=await render_chunk_from_raw(session, chunk))
        for chunk, dist in rows
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

    Runs in its own session over seconds of CPU work, so a message edit can
    land mid-batch. The final clear is a guarded UPDATE that only writes when
    content_version is unchanged since read — otherwise the edited chunk stays
    stale and re-embeds next pass instead of being fixed with a stale vector.
    """
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
    rendered: list[tuple[int, int, str, list]] = []  # (id, version_at_read, text, vector)
    for c in chunks:
        text = await render_chunk_from_raw(session, c) if c.stale else c.text
        rendered.append((c.id, c.content_version, text))
    vectors = await embedder.embed_passages([r[2] for r in rendered])

    written = 0
    for (chunk_id, version, text), vector in zip(rendered, vectors):
        result = await session.execute(
            update(Chunk)
            .where(Chunk.id == chunk_id, Chunk.content_version == version)
            .values(embedding=vector, text=text, stale=False)
        )
        written += result.rowcount or 0
    await session.commit()
    return written
