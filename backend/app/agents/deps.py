from dataclasses import dataclass, field

from aiogram.utils.chat_action import ChatActionSender
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
    # attach_media_url / generate_image / edit_image); consumed by execute_run
    # after the model run finishes — delivery is post-hoc, so tools can only
    # accumulate.
    media: list[OutgoingMedia] = field(default_factory=list)
    # (tg_message_id, thread_id) override from set_reply_target: the text
    # reply anchors to this message in its thread; media follows the thread
    # but never carries the reply link.
    reply_target: tuple[int, int | None] | None = None
    # The run's chat-action sender (member-invoked runs only) and the image
    # generations/edits/uploads in flight: while any are, the chat shows
    # "sending photo…" instead of "typing…".
    chat_action: ChatActionSender | None = None
    generating: int = 0

    def image_work(self, delta: int) -> None:
        self.generating += delta
        if self.chat_action is not None:
            self.chat_action.action = "upload_photo" if self.generating else "typing"
