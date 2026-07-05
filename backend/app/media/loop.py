"""Worker loop: turn pending media attachments into text.

Bytes are downloaded from Telegram into memory, described/transcribed, and
dropped — nothing is persisted except the text. When a description lands
after the covering chunk already closed, the chunk is marked stale and the
memory loop re-renders + re-embeds it (the same machinery message edits use).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, get_provider
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.media.describe import Describer
from app.memory.store import mark_chunks_stale
from app.models import Bot, Chat, ConnectedModel, Message, MessageAttachment
from app.telegram.client import BotCache

log = logging.getLogger("convoke.media")

TICK_S = 5
MAX_ATTEMPTS = 3
RETRY_BACKOFF_S = 60  # × attempts

IMAGE_KINDS = ("photo", "sticker", "image_document")
AUDIO_KINDS = ("voice", "audio")
VIDEO_KINDS = ("video", "video_note")

_AUDIO_FILENAMES = {"audio/ogg": "voice.ogg", "audio/mpeg": "audio.mp3", "audio/mp4": "audio.m4a"}


class MediaLoop:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        bots: BotCache | None = None,
        describer: Describer | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bots = bots or BotCache()
        self.describer = describer or Describer()
        self.settings = get_settings()

    async def run(self) -> None:
        try:
            while True:
                try:
                    await self._tick()
                except Exception:  # noqa: BLE001 — the loop must survive transient failures
                    log.exception("media tick failed")
                await asyncio.sleep(TICK_S)
        finally:
            await self.bots.aclose()

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        async with self.sessionmaker() as session:
            rows = (
                await session.execute(
                    select(MessageAttachment, Bot)
                    .join(Chat, Chat.id == MessageAttachment.chat_id)
                    .join(Bot, Bot.id == Chat.bot_id)
                    .where(MessageAttachment.status == "pending")
                    .order_by(MessageAttachment.id)
                    .limit(self.settings.media_describe_batch * 4)
                )
            ).all()
            due = [(att, bot) for att, bot in rows if self._due(att, now)]
            if not due:
                return
            vision = await self._resolve(session, "vision")
            transcription = await self._resolve(session, "transcription")
            video = await self._resolve(session, "video")
            for att, bot_row in due[: self.settings.media_describe_batch]:
                await self._process_one(session, att, bot_row, vision, transcription, video, now)
                await session.commit()  # one attachment per transaction

    @staticmethod
    def _due(att: MessageAttachment, now: datetime) -> bool:
        if att.last_attempt_at is None:
            return True
        last = att.last_attempt_at
        if last.tzinfo is None:  # sqlite hands back naive datetimes
            last = last.replace(tzinfo=timezone.utc)
        return now - last > timedelta(seconds=RETRY_BACKOFF_S * max(att.attempts, 1))

    @staticmethod
    async def _resolve(session: AsyncSession, role: str) -> ConnectedModel | None:
        try:
            return await get_provider(session, role)
        except ProviderNotConfigured:
            return None

    async def _process_one(
        self,
        session: AsyncSession,
        att: MessageAttachment,
        bot_row: Bot,
        vision: ConnectedModel | None,
        transcription: ConnectedModel | None,
        video: ConnectedModel | None,
        now: datetime,
    ) -> None:
        try:
            skip_reason = self._skip_reason(att, vision, transcription)
            if skip_reason:
                att.status = "skipped"
                att.error = skip_reason
                return
            # Import media reads local bytes — no Telegram client involved.
            bot = (
                self.bots.get(bot_row.id, bot_row.token_encrypted, decrypt(bot_row.token_encrypted))
                if att.file_id is not None
                else None
            )
            caption = await self._caption(session, att)
            if att.kind in IMAGE_KINDS:
                data = await self._load_bytes(bot, att)
                att.description = await self.describer.describe_image(
                    vision, data, att.mime or _image_mime(att.kind), caption
                )
            elif att.kind in AUDIO_KINDS:
                data = await self._load_bytes(bot, att)
                mime = att.mime or "audio/ogg"
                att.transcript = await self.describer.transcribe(
                    transcription, data, _AUDIO_FILENAMES.get(mime, "audio.ogg"), mime
                )
            else:
                await self._process_video(bot, att, caption, vision, transcription, video)
            att.status = "described"
            att.error = None
            att.described_at = now
            self._discard_import_bytes(att)
            await mark_chunks_stale(session, att.chat_id, att.tg_message_id)
            log.info("described attachment %d (%s) in chat %d", att.id, att.kind, att.chat_id)
        except Exception as e:  # noqa: BLE001 — bookkeep and retry with backoff
            att.attempts += 1
            att.last_attempt_at = now
            att.error = f"{type(e).__name__}: {str(e)[:300]}"
            if att.attempts >= MAX_ATTEMPTS:
                att.status = "failed"
            log.warning(
                "attachment %d (%s) attempt %d failed: %s", att.id, att.kind, att.attempts, att.error
            )

    async def _process_video(
        self,
        bot,
        att: MessageAttachment,
        caption: str,
        vision: ConnectedModel | None,
        transcription: ConnectedModel | None,
        video: ConnectedModel | None,
    ) -> None:
        """video / video_note. Native path when a video-capable model is
        assigned; otherwise thumbnail + ffmpeg-sampled frames + audio
        transcript (each part best-effort). >20MB can never be downloaded —
        thumbnail-only, annotated as such."""
        size_ok = (att.size_bytes or 0) <= self.settings.media_max_download_bytes
        data = await self._load_bytes(bot, att) if size_ok else None
        mime = att.mime or "video/mp4"

        if transcription is not None and data:
            att.transcript = await self.describer.transcribe(
                transcription, data, "video.mp4", mime
            )

        if video is not None and data:
            att.description = await self.describer.describe_video_native(
                video, data, mime, caption
            )
        elif vision is not None:
            frames: list[bytes] = []
            if att.thumb_file_id:
                frames.append(await self._download(bot, att.thumb_file_id))
            if data:
                frames.extend(
                    await self.describer.sample_frames(
                        data, self.settings.video_sample_frames, att.duration_s
                    )
                )
            if frames:
                att.description = await self.describer.describe_frames(
                    vision, frames, caption, att.transcript
                )
                if not data:
                    att.description = (
                        f"{att.description} (large video — described from thumbnail only)"
                    )
        if not att.description and not att.transcript:
            raise RuntimeError("no thumbnail to describe and file too large to transcribe")

    def _skip_reason(
        self,
        att: MessageAttachment,
        vision: ConnectedModel | None,
        transcription: ConnectedModel | None,
    ) -> str | None:
        if att.file_id is None and att.import_path is None:
            return "media bytes unavailable"  # import-sourced without a copy
        if att.kind in IMAGE_KINDS:
            if vision is None:
                return "no vision model configured"
            if (att.size_bytes or 0) > self.settings.media_max_download_bytes:
                return "file too large to download"
        elif att.kind in AUDIO_KINDS:
            if transcription is None:
                return "no transcription model configured"
            if (att.size_bytes or 0) > self.settings.media_max_download_bytes:
                return "file too large to download"
        elif vision is None and transcription is None:
            return "no vision or transcription model configured"
        return None

    @staticmethod
    async def _caption(session: AsyncSession, att: MessageAttachment) -> str:
        msg = await session.get(Message, att.message_id)
        return msg.text if msg else ""

    async def _load_bytes(self, bot, att: MessageAttachment) -> bytes:
        """Live media downloads from Telegram by file_id; import media reads
        the transient local copy under imports_dir."""
        if att.file_id is not None:
            return await self._download(bot, att.file_id)
        path = Path(self.settings.imports_dir) / att.import_path
        return await asyncio.to_thread(path.read_bytes)

    def _discard_import_bytes(self, att: MessageAttachment) -> None:
        """Describe-then-discard: an import file has served its purpose."""
        if att.import_path:
            (Path(self.settings.imports_dir) / att.import_path).unlink(missing_ok=True)

    @staticmethod
    async def _download(bot, file_id: str) -> bytes:
        buf = await bot.download(file_id)
        return buf.read() if buf is not None else b""


def _image_mime(kind: str) -> str:
    return "image/webp" if kind == "sticker" else "image/jpeg"
