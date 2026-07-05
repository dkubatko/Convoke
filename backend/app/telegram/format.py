"""Telegram HTML sanitizer for agent replies.

Telegram's HTML parse mode accepts a small tag set and rejects the ENTIRE
message (400) on anything else — an unclosed <b>, a stray </i>, a <div>. The
agent is instructed to format with the allowed subset, but a model's output
is never trusted as valid markup: to_telegram_html() rebuilds it so allowed
tags pass through balanced and everything else renders literally."""

import html
from html.parser import HTMLParser

# tag → attributes allowed through (Telegram ignores all others)
ALLOWED: dict[str, tuple[str, ...]] = {
    "b": (), "strong": (),
    "i": (), "em": (),
    "u": (), "ins": (),
    "s": (), "strike": (), "del": (),
    "code": (), "pre": (),
    "blockquote": (),
    "a": ("href",),
    "span": ("class",),  # only class="tg-spoiler" survives the check below
    "tg-spoiler": (),
}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.stack: list[str] = []

    def _literal(self) -> None:
        self.out.append(html.escape(self.get_starttag_text() or ""))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":  # Telegram has no <br>; models emit it anyway
            self.out.append("\n")
            return
        if tag not in ALLOWED:
            self._literal()
            return
        if tag == "span" and dict(attrs).get("class") != "tg-spoiler":
            self._literal()
            return
        kept = " ".join(
            f'{name}="{html.escape(value or "", quote=True)}"'
            for name, value in attrs
            if name in ALLOWED[tag]
        )
        self.out.append(f"<{tag}{' ' + kept if kept else ''}>")
        self.stack.append(tag)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "br":
            self.out.append("\n")
        else:
            self._literal()

    def handle_endtag(self, tag: str) -> None:
        if tag not in self.stack:
            return  # stray close of a never-opened (or disallowed) tag: drop
        while self.stack:  # close intervening unclosed tags to keep nesting valid
            open_tag = self.stack.pop()
            self.out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    def handle_data(self, data: str) -> None:
        self.out.append(html.escape(data))


def to_telegram_html(text: str) -> str:
    """Rebuild text as Telegram-valid HTML: whitelisted tags balanced,
    everything else escaped. Plain text passes through merely escaped."""
    s = _Sanitizer()
    s.feed(text)
    s.close()
    while s.stack:  # auto-close anything the model left open
        s.out.append(f"</{s.stack.pop()}>")
    return "".join(s.out)
