from aiogram import Bot as AiogramBot
from aiogram.client.default import DefaultBotProperties

# Updates Convoke consumes. Anything else is discarded server-side by Telegram.
ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member", "callback_query"]


def make_bot(token: str) -> AiogramBot:
    # HTML parse mode everywhere: MarkdownV2 escaping is a known tarpit.
    return AiogramBot(token, default=DefaultBotProperties(parse_mode="HTML"))
