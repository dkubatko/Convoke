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
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import ProviderNotConfigured, get_provider
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.media.describe import Describer
from app.media.loop import AUDIO_KINDS, IMAGE_KINDS, _image_mime
from app.models import Bot, Chat, Message, MessageAttachment
from app.telegram.client import make_bot

log = logging.getLogger("convoke.media")


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
                vision, data, att.mime or _image_mime(att.kind), question, caption
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
                    vision, frames, question, att.transcript, caption
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
