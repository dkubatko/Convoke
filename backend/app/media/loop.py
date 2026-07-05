"""Worker loop: turn pending media attachments into text.

Bytes are downloaded from Telegram into memory, described/transcribed, and
dropped — nothing is persisted except the text. When a description lands
after the covering chunk already closed, the chunk is marked stale and the
memory loop re-renders + re-embeds it (the same machinery message edits use).

Each tick is three phases: plan (one session: pick due work, mark skips,
snapshot plain work items), understand (no session: up to
media_describe_concurrency downloads + model calls in parallel), apply (a
fresh session: persist text / retry bookkeeping). Ingestion never waits on
any of this — the inbox consumer only writes rows.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, get_provider
from app.core.config import get_settings
from app.core.crypto import decrypt
from app.core.runtime_settings import effective_settings
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


@dataclass
class Understood:
    description: str | None = None
    transcript: str | None = None


@dataclass
class _Work:
    """A session-free snapshot of one attachment to understand — the parallel
    phase must never touch ORM state."""

    att_id: int
    chat_id: int
    tg_message_id: int
    kind: str
    file_id: str | None
    import_path: str | None
    mime: str | None
    size_bytes: int | None
    duration_s: int | None
    thumb_file_id: str | None
    caption: str
    bot_id: int
    token_encrypted: str


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
        self._base = get_settings()
        self.settings = self._base

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

        # Phase 1 — plan: pick due attachments, persist skips, snapshot work.
        work: list[_Work] = []
        async with self.sessionmaker() as session:
            self.settings = await effective_settings(session, self._base)
            concurrency = max(1, self.settings.media_describe_concurrency)
            rows = (
                await session.execute(
                    select(MessageAttachment, Bot)
                    .join(Chat, Chat.id == MessageAttachment.chat_id)
                    .join(Bot, Bot.id == Chat.bot_id)
                    .where(MessageAttachment.status == "pending")
                    .order_by(MessageAttachment.id)
                    .limit(concurrency * 4)
                )
            ).all()
            due = [(att, bot) for att, bot in rows if self._due(att, now)]
            if not due:
                return
            vision = await self._resolve(session, "vision")
            transcription = await self._resolve(session, "transcription")
            video = await self._resolve(session, "video")
            for att, bot_row in due[:concurrency]:
                skip_reason = self._skip_reason(att, vision, transcription)
                if skip_reason:
                    att.status = "skipped"
                    att.error = skip_reason
                    continue
                work.append(
                    _Work(
                        att_id=att.id,
                        chat_id=att.chat_id,
                        tg_message_id=att.tg_message_id,
                        kind=att.kind,
                        file_id=att.file_id,
                        import_path=att.import_path,
                        mime=att.mime,
                        size_bytes=att.size_bytes,
                        duration_s=att.duration_s,
                        thumb_file_id=att.thumb_file_id,
                        caption=await self._caption(session, att),
                        bot_id=bot_row.id,
                        token_encrypted=bot_row.token_encrypted,
                    )
                )
            await session.commit()
        if not work:
            return

        # Phase 2 — understand: parallel downloads + model calls, no session.
        results = await asyncio.gather(
            *(self._understand(w, vision, transcription, video) for w in work),
            return_exceptions=True,
        )

        # Phase 3 — apply: persist outcomes in a fresh session.
        async with self.sessionmaker() as session:
            atts = {
                a.id: a
                for a in (
                    await session.execute(
                        select(MessageAttachment).where(
                            MessageAttachment.id.in_([w.att_id for w in work])
                        )
                    )
                ).scalars()
            }
            for w, result in zip(work, results):
                att = atts.get(w.att_id)
                if att is None:
                    continue  # message deleted mid-flight
                if isinstance(result, BaseException):
                    att.attempts += 1
                    att.last_attempt_at = now
                    att.error = f"{type(result).__name__}: {str(result)[:300]}"
                    if att.attempts >= MAX_ATTEMPTS:
                        att.status = "failed"
                    log.warning(
                        "attachment %d (%s) attempt %d failed: %s",
                        att.id, att.kind, att.attempts, att.error,
                    )
                    continue
                att.description = result.description
                att.transcript = result.transcript
                att.status = "described"
                att.error = None
                att.described_at = now
                self._discard_import_bytes(att)
                await mark_chunks_stale(session, att.chat_id, att.tg_message_id)
                log.info("described attachment %d (%s) in chat %d", att.id, att.kind, att.chat_id)
            await session.commit()

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

    async def _understand(
        self,
        w: _Work,
        vision: ConnectedModel | None,
        transcription: ConnectedModel | None,
        video: ConnectedModel | None,
    ) -> Understood:
        if w.kind in IMAGE_KINDS:
            data = await self._load_bytes(w)
            description = await self.describer.describe_image(
                vision, data, w.mime or _image_mime(w.kind), w.caption
            )
            return Understood(description=description)
        if w.kind in AUDIO_KINDS:
            data = await self._load_bytes(w)
            mime = w.mime or "audio/ogg"
            transcript = await self.describer.transcribe(
                transcription, data, _AUDIO_FILENAMES.get(mime, "audio.ogg"), mime
            )
            return Understood(transcript=transcript)
        return await self._understand_video(w, vision, transcription, video)

    async def _understand_video(
        self,
        w: _Work,
        vision: ConnectedModel | None,
        transcription: ConnectedModel | None,
        video: ConnectedModel | None,
    ) -> Understood:
        """video / video_note. Native path when a video-capable model is
        assigned; otherwise thumbnail + ffmpeg-sampled frames + audio
        transcript (each part best-effort). >20MB can never be downloaded —
        thumbnail-only, annotated as such."""
        out = Understood()
        size_ok = (w.size_bytes or 0) <= self.settings.media_max_download_bytes
        data = await self._load_bytes(w) if size_ok else None
        mime = w.mime or "video/mp4"

        if transcription is not None and data:
            out.transcript = await self.describer.transcribe(
                transcription, data, "video.mp4", mime
            )

        if video is not None and data:
            out.description = await self.describer.describe_video_native(
                video, data, mime, w.caption
            )
        elif vision is not None:
            frames: list[bytes] = []
            if w.thumb_file_id:
                frames.append(await self._download(self._bot(w), w.thumb_file_id))
            if data:
                frames.extend(
                    await self.describer.sample_frames(
                        data, self.settings.video_sample_frames, w.duration_s
                    )
                )
            if frames:
                out.description = await self.describer.describe_frames(
                    vision, frames, w.caption, out.transcript
                )
                if not data:
                    out.description = (
                        f"{out.description} (large video — described from thumbnail only)"
                    )
        if not out.description and not out.transcript:
            raise RuntimeError("no thumbnail to describe and file too large to transcribe")
        return out

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

    def _bot(self, w: _Work):
        return self.bots.get(w.bot_id, w.token_encrypted, decrypt(w.token_encrypted))

    async def _load_bytes(self, w: _Work) -> bytes:
        """Live media downloads from Telegram by file_id; import media reads
        the transient local copy under imports_dir."""
        if w.file_id is not None:
            return await self._download(self._bot(w), w.file_id)
        path = Path(self.settings.imports_dir) / w.import_path
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
