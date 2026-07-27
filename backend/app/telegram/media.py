"""Shared media types for ingestion and delivery.

Lives apart from handlers/sender because both need `extract_attachment` and
handlers already imports sender — a sender→handlers import would cycle.
"""

from dataclasses import dataclass

from aiogram.types import Message as TgMessage

from app.models import MessageAttachment


@dataclass
class OutgoingMedia:
    """One media item the agent asked to attach to its reply, accumulated on
    AgentDeps during the run and delivered by execute_run after the model
    finishes. History items carry the re-sendable file_id (and the source
    attachment's description, so the re-send is never re-described); URL items
    carry only the URL — Telegram's servers fetch it at send time (our worker
    never downloads agent-supplied URLs)."""

    kind: str  # "photo" | "video"
    source: str  # "history" | "url"
    file_id: str | None = None  # history source
    url: str | None = None  # url source (photos only)
    src_tg_message_id: int | None = None
    # Source attachment was already described — the flag, not text truthiness,
    # decides re-describe: a described source with empty vision output must
    # not send its copy back through the pipeline.
    described: bool = False
    description: str | None = None  # copied from the source attachment
    transcript: str | None = None


def extract_attachment(msg: TgMessage) -> MessageAttachment | None:
    """Map the message's media (if any) to an attachment row. chat_id and
    tg_message_id are filled in by the caller. Telegram messages carry at most
    one media item; album members arrive as separate messages sharing
    media_group_id."""
    if msg.photo:
        p = msg.photo[-1]  # renditions are sorted ascending; keep the largest
        return MessageAttachment(
            kind="photo",
            file_id=p.file_id,
            file_unique_id=p.file_unique_id,
            size_bytes=p.file_size,
            width=p.width,
            height=p.height,
            media_group_id=msg.media_group_id,
        )
    if msg.video:
        v = msg.video
        return MessageAttachment(
            kind="video",
            file_id=v.file_id,
            file_unique_id=v.file_unique_id,
            mime=v.mime_type,
            size_bytes=v.file_size,
            width=v.width,
            height=v.height,
            duration_s=v.duration,
            thumb_file_id=v.thumbnail.file_id if v.thumbnail else None,
            media_group_id=msg.media_group_id,
        )
    if msg.voice:
        return MessageAttachment(
            kind="voice",
            file_id=msg.voice.file_id,
            file_unique_id=msg.voice.file_unique_id,
            mime=msg.voice.mime_type,
            size_bytes=msg.voice.file_size,
            duration_s=msg.voice.duration,
        )
    if msg.video_note:
        vn = msg.video_note
        return MessageAttachment(
            kind="video_note",
            file_id=vn.file_id,
            file_unique_id=vn.file_unique_id,
            size_bytes=vn.file_size,
            duration_s=vn.duration,
            thumb_file_id=vn.thumbnail.file_id if vn.thumbnail else None,
        )
    if msg.sticker:
        s = msg.sticker
        return MessageAttachment(
            kind="sticker",
            file_id=s.file_id,
            file_unique_id=s.file_unique_id,
            size_bytes=s.file_size,
            width=s.width,
            height=s.height,
            sticker_emoji=s.emoji,
            thumb_file_id=s.thumbnail.file_id if s.thumbnail else None,
        )
    if msg.animation:  # GIFs; msg.document duplicates this, so check first
        a = msg.animation
        # Own kind, not "video": a file_id of type Animation cannot be re-sent
        # via sendVideo/InputMediaVideo, so attach_media must be able to tell
        # them apart. (Rows ingested before this kind existed say "video" and
        # will fail attach-delivery gracefully — indistinguishable in the DB.)
        return MessageAttachment(
            kind="animation",
            file_id=a.file_id,
            file_unique_id=a.file_unique_id,
            mime=a.mime_type,
            size_bytes=a.file_size,
            width=a.width,
            height=a.height,
            duration_s=a.duration,
            thumb_file_id=a.thumbnail.file_id if a.thumbnail else None,
        )
    if msg.audio:
        return MessageAttachment(
            kind="audio",
            file_id=msg.audio.file_id,
            file_unique_id=msg.audio.file_unique_id,
            mime=msg.audio.mime_type,
            size_bytes=msg.audio.file_size,
            duration_s=msg.audio.duration,
        )
    if msg.document and (msg.document.mime_type or "").startswith("image/"):
        d = msg.document
        return MessageAttachment(
            kind="image_document",
            file_id=d.file_id,
            file_unique_id=d.file_unique_id,
            mime=d.mime_type,
            size_bytes=d.file_size,
            thumb_file_id=d.thumbnail.file_id if d.thumbnail else None,
        )
    return None  # non-image documents, polls, locations… stay out of scope
