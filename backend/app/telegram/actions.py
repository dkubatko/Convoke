"""Chat actions the agent queues during a run — polls, locations, reactions,
closing its polls — performed by execute_run after the reply text and media
land (same post-hoc contract as OutgoingMedia: a declined or failed run
leaves no trace in the chat).
"""

import html
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from aiogram import Bot as AiogramBot
from aiogram.types import InputPollOption, ReactionTypeEmoji
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.memory.store import mark_chunks_stale
from app.models import Chat, Message, Poll
from app.telegram.content import apply_poll_snapshot
from app.telegram.sender import persist_sent

log = logging.getLogger("convoke.agent")

# Emoji Telegram accepts as a bot's reaction (Bot API ReactionTypeEmoji). A
# chat can narrow this further; that surfaces as a failed delivery.
REACTION_EMOJI = frozenset(
    "❤ 👍 👎 🔥 🥰 👏 😁 🤔 🤯 😱 🤬 😢 🎉 🤩 🤮 💩 🙏 👌 🕊 🤡 🥱 🥴 😍 🐳 ❤‍🔥 🌚 🌭 💯 "
    "🤣 ⚡ 🍌 🏆 💔 🤨 😐 🍓 🍾 💋 🖕 😈 😴 😭 🤓 👻 👨‍💻 👀 🎃 🙈 😇 😨 🤝 ✍ 🤗 🫡 🎅 🎄 "
    "☃ 💅 🤪 🗿 🆒 💘 🙉 🦄 😘 💊 🙊 😎 👾 🤷‍♂ 🤷 🤷‍♀ 😡".split()
)


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def normalize_emoji(emoji: str) -> str:
    # Models write "❤️" (with the U+FE0F presentation selector); Telegram's
    # list has the bare form.
    return emoji.strip().replace("️", "")


@dataclass
class OutgoingAction:
    kind: str  # "poll" | "location" | "venue" | "reaction" | "close_poll"
    params: dict = field(default_factory=dict)

    def label(self) -> str:
        """How the action reads in a run's episode summary."""
        p = self.params
        if self.kind == "poll":
            return f"sent poll «{p['question']}»"
        if self.kind == "venue":
            return f"sent venue {p['title']}"
        if self.kind == "location":
            return "sent a location"
        if self.kind == "reaction":
            return f"reacted {p['emoji']} to #{p['message_id']}"
        return f"closed poll #{p['message_id']}"

    def failure_label(self) -> str:
        """What a failure notice says couldn't be done."""
        return {
            "poll": "send the poll",
            "location": "send the location",
            "venue": "send the location",
            "close_poll": "close the poll",
        }.get(self.kind, "do that")


async def perform(
    session: AsyncSession,
    bot: AiogramBot,
    chat: Chat,
    action: OutgoingAction,
    thread_id: int | None,
) -> None:
    """Perform one action and persist what it changed. Raises on failure —
    the caller records it."""
    p = action.params
    if action.kind == "poll":
        # The bot-wide HTML parse mode applies to poll text too (question,
        # options, description): escape, so a "<" can't break the send.
        sent = await bot.send_poll(
            chat.tg_chat_id,
            question=_escape(p["question"]),
            options=[InputPollOption(text=_escape(o)) for o in p["options"]],
            is_anonymous=p["anonymous"],
            allows_multiple_answers=p["multiple_answers"],
            description=_escape(p["description"]) if p.get("description") else None,
            close_date=p.get("close_date"),
            message_thread_id=thread_id,
        )
        persist_sent(session, chat, sent)
    elif action.kind == "location":
        sent = await bot.send_location(
            chat.tg_chat_id, latitude=p["latitude"], longitude=p["longitude"],
            message_thread_id=thread_id,
        )
        persist_sent(session, chat, sent)
    elif action.kind == "venue":
        sent = await bot.send_venue(
            chat.tg_chat_id, latitude=p["latitude"], longitude=p["longitude"],
            title=p["title"], address=p["address"], message_thread_id=thread_id,
        )
        persist_sent(session, chat, sent)
    elif action.kind == "reaction":
        await bot.set_message_reaction(
            chat.tg_chat_id, p["message_id"], reaction=[ReactionTypeEmoji(emoji=p["emoji"])]
        )
    elif action.kind == "close_poll":
        tg_poll = await bot.stop_poll(chat.tg_chat_id, p["message_id"])
        row = (
            await session.execute(
                select(Poll).join(Message, Message.id == Poll.message_id).where(
                    Message.chat_id == chat.id, Message.tg_message_id == p["message_id"]
                )
            )
        ).scalar_one_or_none()
        if row is not None:
            apply_poll_snapshot(row, tg_poll, datetime.now(timezone.utc))
            await mark_chunks_stale(session, chat.id, p["message_id"])
    else:
        raise ValueError(f"unknown action {action.kind!r}")
