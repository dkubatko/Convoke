from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory.embeddings import Embedder


@dataclass
class AgentDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    embedder: Embedder
    chat_id: int
    run_id: int
    # Set for workflow-triggered runs; lets tools look up what this workflow
    # already did in this chat.
    workflow_id: int | None = None
