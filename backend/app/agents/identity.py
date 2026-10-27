"""The bot's per-chat identity: the operator-editable head of its instructions.

The agent's instructions are identity + system rules. The identity (who the
bot is, how it talks) is per-chat text an operator can rewrite on the chat's
Identity tab; the system rules (formatting contract, transcript conventions,
tool guidance — runtime.SYSTEM_RULES) stay in code and invisible, so editing
a personality can never break delivery or tool use.
"""

import re

MAX_IDENTITY_CHARS = 8000
MAX_NAMES = 10
MAX_NAME_CHARS = 32

# Placeholders an identity may use; filled per run. Plain substitution, not
# str.format: operator text with other braces must pass through untouched.
PLACEHOLDERS = {
    "{name}": "the bot's display name",
    "{username}": "the bot's @username (without @)",
    "{chat_title}": "this chat's title",
}

DEFAULT_IDENTITY = """\
You are {name} (@{username}), an assistant participating in the Telegram group \
chat "{chat_title}".

- Answer directly and conversationally; this is a group chat, keep it brief.
- If you genuinely can't help, say so briefly rather than guessing at facts you \
don't know."""


def render_identity(identity: str | None, *, name: str, username: str, chat_title: str) -> str:
    text = (identity or "").strip() or DEFAULT_IDENTITY
    for key, value in (("{name}", name), ("{username}", username), ("{chat_title}", chat_title)):
        text = text.replace(key, value)
    return text


def normalize_names(names: list[str] | None) -> list[str]:
    """Trimmed, non-empty, de-duplicated (case-insensitively), in order."""
    out: list[str] = []
    for n in names or []:
        n = " ".join(str(n).split())
        if n and n.lower() not in {o.lower() for o in out}:
            out.append(n)
    return out


def mentions_name(text: str, names: list[str] | None) -> bool:
    """True when `text` contains one of the names as a whole word, any case —
    "Бот, скажи" and "спроси бот" match; "робот" and "ботинки" don't."""
    for name in names or []:
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            return True
    return False
