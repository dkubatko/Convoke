from aiogram import Bot as AiogramBot
from aiogram.client.default import DefaultBotProperties

# Updates Convoke consumes. Anything else is discarded server-side by Telegram.
ALLOWED_UPDATES = ["message", "edited_message", "my_chat_member", "callback_query"]


def make_bot(token: str) -> AiogramBot:
    # HTML parse mode everywhere: MarkdownV2 escaping is a known tarpit.
    return AiogramBot(token, default=DefaultBotProperties(parse_mode="HTML"))


class BotCache:
    """Caches aiogram Bot clients keyed on (bot_id, encrypted-token). When the
    operator revokes and re-registers a token the ciphertext changes, so the
    stale client is dropped and its session closed instead of failing every
    call with Unauthorized until the next worker restart."""

    def __init__(self) -> None:
        self._bots: dict[int, tuple[str, AiogramBot]] = {}
        self._stale: list[AiogramBot] = []

    _INJECTED = "__injected__"

    def get(self, bot_id: int, token_encrypted: str, token: str) -> AiogramBot:
        cached = self._bots.get(bot_id)
        if cached is not None and cached[0] in (token_encrypted, self._INJECTED):
            return cached[1]
        if cached is not None:
            self._stale.append(cached[1])  # old token; closed at next aclose
        bot = make_bot(token)
        self._bots[bot_id] = (token_encrypted, bot)
        return bot

    def put(self, bot_id: int, bot: AiogramBot) -> None:
        """Inject a pre-built (e.g. fake) client; used by tests."""
        self._bots[bot_id] = (self._INJECTED, bot)

    async def aclose(self) -> None:
        for _, bot in self._bots.values():
            await bot.session.close()
        for bot in self._stale:
            await bot.session.close()
        self._bots.clear()
        self._stale.clear()
