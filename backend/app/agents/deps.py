from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory.embeddings import Embedder


@dataclass
class AgentDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    embedder: Embedder
    chat_id: int
    run_id: int
