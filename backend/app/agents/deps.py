from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.memory.embeddings import Embedder
from app.telegram.media import OutgoingMedia


@dataclass
class AgentDeps:
    sessionmaker: async_sessionmaker[AsyncSession]
    embedder: Embedder
    chat_id: int
    run_id: int
    # Set for workflow-triggered runs; lets tools look up what this workflow
    # already did in this chat.
    workflow_id: int | None = None
    # Media the agent asked to attach to its reply (attach_media /
    # attach_media_url); consumed by execute_run after the model run finishes
    # — delivery is post-hoc, so tools can only accumulate.
    media: list[OutgoingMedia] = field(default_factory=list)
    # (tg_message_id, thread_id) override from set_reply_target: the text
    # reply anchors to this message in its thread; media follows the thread
    # but never carries the reply link.
    reply_target: tuple[int, int | None] | None = None
