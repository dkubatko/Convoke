"""Question-directed media re-inspection for the agent's inspect_media tool.

The describe pipeline optimizes for *searchable memory* — short, generic,
embedding-friendly. When a user asks something specific ("what's the third
item on that receipt?"), the stored description often can't answer, so the
agent re-inspects the SOURCE: download by file_id, one vision call carrying
the actual question, discard the bytes. Voice needs no model — the stored
transcript is already the full content.

Hard limits inherited from the pipeline: import-sourced media has no bytes
(describe-then-discard, file_id=NULL) and the Bot API caps downloads at
~20MB (videos degrade to thumbnail + stored transcript).

Generic files ("file" kind) are never described; inspection returns their
text (plain-text formats and PDFs) for the agent to read itself — no model
call, and the question is answered by the agent from the text.
"""

import asyncio
import io
import logging
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import ProviderNotConfigured, get_provider, get_role_reasoning
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.media.describe import Describer
from app.media.loop import AUDIO_KINDS, IMAGE_KINDS, _image_mime
from app.models import Bot, Chat, Message, MessageAttachment
from app.telegram.client import make_bot

log = logging.getLogger("convoke.media")

# Enough for a long document's substance without flooding the run's context.
MAX_FILE_CHARS = 30_000
_TEXT_MIMES = (
    "application/json", "application/xml", "application/x-yaml", "application/yaml",
    "application/javascript", "application/x-sh", "application/sql", "application/csv",
)
_TEXT_SUFFIXES = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml", ".log", ".ini",
    ".toml", ".html", ".htm", ".py", ".js", ".ts", ".sql", ".sh", ".ics", ".vcf", ".srt",
}


def file_text(data: bytes, mime: str | None, name: str | None) -> str | None:
    """A file's text, or None for formats that can't be read as text."""
    mime = (mime or "").lower()
    suffix = PurePosixPath(name or "").suffix.lower()
    if mime == "application/pdf" or suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [(p.extract_text() or "").strip() for p in reader.pages]
        return "\n\n".join(f"--- page {i} ---\n{t}" for i, t in enumerate(pages, 1) if t)
    if mime.startswith("text/") or mime in _TEXT_MIMES or suffix in _TEXT_SUFFIXES:
        return data.decode("utf-8", errors="replace")
    return None


async def _inspect_file(att: MessageAttachment, bot, max_bytes: int) -> str:
    name = att.file_name or "unnamed file"
    if att.file_id is None:
        return f"{name} came from a history import — the file itself isn't available."
    if (att.size_bytes or 0) > max_bytes:
        return f"{name} is too large to download."
    buf = await bot.download(att.file_id)
    data = buf.read() if buf is not None else b""
    text = await asyncio.to_thread(file_text, data, att.mime, att.file_name)
    if text is None:
        return f"{name} ({att.mime or 'unknown type'}) can't be read as text — only text-based files and PDFs can."
    text = text.strip()
    if not text:
        return f"{name} has no extractable text (a scanned PDF or an empty file)."
    note = ""
    if len(text) > MAX_FILE_CHARS:
        note = f"\n\n[truncated: showing the first {MAX_FILE_CHARS} of {len(text)} characters]"
        text = text[:MAX_FILE_CHARS]
    return f"Contents of {name}:\n\n{text}{note}"


async def inspect_attachment(
    session: AsyncSession,
    chat_id: int,
    tg_message_id: int,
    question: str,
    describer: Describer | None = None,
) -> str:
    """Answer `question` from the media on one message. Always returns text —
    errors and unavailability come back as sentences the agent can act on."""
    att = (
        await session.execute(
            select(MessageAttachment).where(
                MessageAttachment.chat_id == chat_id,
                MessageAttachment.tg_message_id == tg_message_id,
            )
        )
    ).scalars().first()
    if att is None:
        return f"#{tg_message_id} has no media attachment."

    stored: list[str] = []
    if att.description:
        stored.append(f"Stored description: {att.description}")
    if att.transcript:
        stored.append(f"Transcript: {att.transcript}")
    stored_text = "\n".join(stored)

    if att.kind in AUDIO_KINDS:
        # The transcript IS the content — no model call needed.
        return stored_text or "Not transcribed yet (or transcription failed)."

    if att.kind == "file":
        bot_row = (
            await session.execute(
                select(Bot).join(Chat, Chat.bot_id == Bot.id).where(Chat.id == chat_id)
            )
        ).scalar_one()
        bot = make_bot(decrypt(bot_row.token_encrypted))
        try:
            return await _inspect_file(att, bot, get_settings().media_max_download_bytes)
        except Exception as e:  # noqa: BLE001 — tool output, never an exception
            log.warning("inspect_media failed for file #%d: %s", tg_message_id, e)
            return f"Reading the file failed ({type(e).__name__})."
        finally:
            await bot.session.close()

    if att.file_id is None:
        return (
            (stored_text + "\n" if stored_text else "")
            + "The source file came from a history import and was discarded after "
            "description — it can't be re-inspected."
        )

    settings = get_settings()
    describer = describer or Describer()
    try:
        vision = await get_provider(session, "vision")
        effort = await get_role_reasoning(session, "vision")
    except ProviderNotConfigured:
        return (
            (stored_text + "\n" if stored_text else "")
            + "No vision model is configured, so the source can't be re-inspected."
        )

    msg = await session.get(Message, att.message_id)
    caption = msg.text if msg else ""
    bot_row = (
        await session.execute(
            select(Bot).join(Chat, Chat.bot_id == Bot.id).where(Chat.id == chat_id)
        )
    ).scalar_one()
    bot = make_bot(decrypt(bot_row.token_encrypted))
    try:
        size_ok = (att.size_bytes or 0) <= settings.media_max_download_bytes

        async def download(file_id: str) -> bytes:
            buf = await bot.download(file_id)
            return buf.read() if buf is not None else b""

        if att.kind in IMAGE_KINDS:
            if not size_ok:
                return (stored_text + "\n" if stored_text else "") + "File too large to download."
            data = await download(att.file_id)
            answer = await describer.answer_about_image(
                vision, data, att.mime or _image_mime(att.kind), question, caption,
                effort=effort,
            )
        else:  # video / video_note / documents: frames + stored transcript
            frames: list[bytes] = []
            if att.thumb_file_id:
                frames.append(await download(att.thumb_file_id))
            note = ""
            if size_ok:
                data = await download(att.file_id)
                frames.extend(
                    await describer.sample_frames(
                        data, settings.video_sample_frames, att.duration_s
                    )
                )
            else:
                note = " (large file — inspected from thumbnail only)"
            if not frames:
                return (
                    (stored_text + "\n" if stored_text else "")
                    + "No frames could be extracted to inspect."
                )
            answer = (
                await describer.answer_about_frames(
                    vision, frames, question, att.transcript, caption, effort=effort
                )
                + note
            )
        return answer + (f"\n\n{stored_text}" if stored_text else "")
    except Exception as e:  # noqa: BLE001 — tool output, never an exception
        log.warning("inspect_media failed for #%d: %s", tg_message_id, e)
        return (
            (stored_text + "\n" if stored_text else "")
            + f"Re-inspection failed ({type(e).__name__}) — the stored text above is "
            "what's available."
        )
    finally:
        await bot.session.close()
