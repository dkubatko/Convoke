"""Telegram Desktop export ingestion.

Two streaming passes over the uploaded JSON (ijson — a large export must
never be json.loads'd whole): pass 1 reads top-level metadata, pass 2 streams
messages. Between them, a validation scorecard decides whether this export
plausibly belongs to the target chat. An admin can forge an export — the
proportionate control is provenance (import_job_id) + surgical delete, not
cryptography Telegram doesn't offer.
"""

import asyncio
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import ijson
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.models import Chat, Chunk, ChunkState, ImportJob, Message, MessageAttachment

log = logging.getLogger("convoke.import")

INSERT_BATCH = 500
MIN_LIVE_FOR_OVERLAP_CHECK = 20

# Telegram export media_type → attachment kind ("photo" arrives as a separate key).
_EXPORT_MEDIA_KINDS = {
    "video_file": "video",
    "animation": "video",
    "video_message": "video_note",
    "voice_message": "voice",
    "audio_file": "audio",
    "sticker": "sticker",
}


@dataclass
class ExportMeta:
    name: str | None = None
    type: str | None = None
    chat_id: int | None = None


@dataclass
class ExportMedia:
    kind: str
    path: str | None  # relative to the export root; None = not included in export
    mime: str | None = None
    duration_s: int | None = None
    width: int | None = None
    height: int | None = None
    sticker_emoji: str | None = None


@dataclass
class ExportMessage:
    tg_message_id: int
    sender_name: str
    sender_id: int | None
    text: str
    sent_at: datetime
    thread_id: int | None = None
    reply_to_tg_message_id: int | None = None
    media: ExportMedia | None = None


@dataclass
class ValidationResult:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    score: int = 0


def flatten_text(raw) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def parse_sender_id(from_id) -> int | None:
    if not isinstance(from_id, str):
        return None
    digits = "".join(ch for ch in from_id if ch.isdigit())
    return int(digits) if digits else None


def _export_file_path(raw) -> str | None:
    """Export values are relative paths — or a '(File not included. …)'
    placeholder when media wasn't exported."""
    if isinstance(raw, str) and raw and not raw.startswith("("):
        return raw
    return None


def parse_export_media(item: dict) -> ExportMedia | None:
    duration = item.get("duration_seconds")
    duration = int(duration) if isinstance(duration, (int, float, str)) and str(duration).isdigit() else None
    if "photo" in item:
        return ExportMedia(
            kind="photo",
            path=_export_file_path(item.get("photo")),
            width=item.get("width"),
            height=item.get("height"),
        )
    if "file" in item:
        media_type = item.get("media_type")
        mime = item.get("mime_type")
        kind = _EXPORT_MEDIA_KINDS.get(media_type)
        if kind is None:
            if not (mime or "").startswith("image/"):
                return None  # generic documents stay out of scope
            kind = "image_document"
        return ExportMedia(
            kind=kind,
            path=_export_file_path(item.get("file")),
            mime=mime,
            duration_s=duration,
            width=item.get("width"),
            height=item.get("height"),
            sticker_emoji=item.get("sticker_emoji"),
        )
    return None


def parse_export_message(item: dict) -> ExportMessage | None:
    if item.get("type") != "message":
        return None
    text = flatten_text(item.get("text", ""))
    media = parse_export_media(item)
    if not text.strip() and media is None:
        return None
    try:
        msg_id = int(item["id"])
    except (KeyError, TypeError, ValueError):
        return None
    if "date_unixtime" in item:
        sent_at = datetime.fromtimestamp(int(item["date_unixtime"]), tz=timezone.utc)
    elif "date" in item:
        sent_at = datetime.fromisoformat(item["date"]).replace(tzinfo=timezone.utc)
    else:
        return None
    reply_to = item.get("reply_to_message_id")
    return ExportMessage(
        tg_message_id=msg_id,
        sender_name=str(item.get("from") or ""),
        sender_id=parse_sender_id(item.get("from_id")),
        text=text,
        sent_at=sent_at,
        reply_to_tg_message_id=int(reply_to) if isinstance(reply_to, int) else None,
        media=media,
    )


def read_export_meta(path: Path) -> ExportMeta:
    meta = ExportMeta()
    with path.open("rb") as f:
        for prefix, event, value in ijson.parse(f):
            if prefix == "name" and event == "string":
                meta.name = value
            elif prefix == "type" and event == "string":
                meta.type = value
            elif prefix == "id" and event == "number":
                meta.chat_id = int(value)
            elif prefix == "messages" and event == "start_array":
                break  # metadata precedes messages; stop before the heavy part
    return meta


def iter_export_messages(path: Path):
    with path.open("rb") as f:
        for item in ijson.items(f, "messages.item"):
            parsed = parse_export_message(item)
            if parsed is not None:
                yield parsed


def normalized_id_candidates(export_chat_id: int) -> set[int]:
    """Export ids are bare internal ids; Bot API sees -100-prefixed supergroup
    ids and negated basic-group ids."""
    return {export_chat_id, -export_chat_id, int(f"-100{export_chat_id}")}


@dataclass
class ExportScan:
    total: int = 0
    matches: int = 0
    contradictions: int = 0
    senders: set[int] = field(default_factory=set)


def scan_export(path: Path, live_by_id: dict[int, tuple[str, int | None]]) -> ExportScan:
    """One streaming pass: overlap comparison happens against exactly the ids
    live history knows about (the export's chronological tail), never a
    prefix sample — a prefix would miss the overlap in large exports."""
    scan = ExportScan()
    for m in iter_export_messages(path):
        scan.total += 1
        if m.sender_id is not None and len(scan.senders) < 100_000:
            scan.senders.add(m.sender_id)
        live = live_by_id.get(m.tg_message_id)
        if live is None or not live[0] or not m.text:
            continue
        live_text, live_sender = live
        if live_text == m.text and (live_sender is None or live_sender == m.sender_id):
            scan.matches += 1
        else:
            scan.contradictions += 1
    return scan


async def load_live_index(
    session: AsyncSession, chat_id: int
) -> tuple[dict[int, tuple[str, int | None]], set[int]]:
    rows = (
        await session.execute(
            select(Message.tg_message_id, Message.text, Message.sender_id).where(
                Message.chat_id == chat_id, Message.source == "live"
            )
        )
    ).all()
    by_id = {r.tg_message_id: (r.text, r.sender_id) for r in rows}
    senders = {r.sender_id for r in rows if r.sender_id is not None}
    return by_id, senders


def validate_export(
    chat: Chat,
    meta: ExportMeta,
    scan: ExportScan,
    live_count: int,
    live_senders: set[int],
) -> ValidationResult:
    """Scorecard: id match (2pts), title fuzzy (1), live-overlap (2), member
    intersection (1). Needs ≥2 points; text contradictions or zero overlap
    against substantial live history are hard rejects."""
    result = ValidationResult(ok=False)

    if meta.chat_id is not None and chat.tg_chat_id in normalized_id_candidates(meta.chat_id):
        result.score += 2
        result.reasons.append("chat id matches")
    elif meta.chat_id is not None:
        result.reasons.append(f"export chat id {meta.chat_id} does not match this chat")

    if meta.name and chat.title:
        ratio = SequenceMatcher(None, meta.name.lower(), chat.title.lower()).ratio()
        if ratio >= 0.6:
            result.score += 1
            result.reasons.append(f"title similar ({ratio:.2f})")

    if scan.contradictions > max(1, scan.matches // 10):
        result.reasons.append(
            f"{scan.contradictions} overlapping messages contradict live history — rejected"
        )
        return result
    if scan.matches >= 3:
        result.score += 2
        result.reasons.append(f"{scan.matches} overlapping messages match live history")
    elif live_count >= MIN_LIVE_FOR_OVERLAP_CHECK and scan.matches == 0:
        result.reasons.append(
            "no overlap with live history despite substantial live history — rejected"
        )
        return result

    if scan.senders & live_senders:
        result.score += 1
        result.reasons.append("participants overlap with live history")

    result.ok = result.score >= 2
    if not result.ok:
        result.reasons.append(f"insufficient evidence (score {result.score}/2)")
    return result


def job_media_dir(job_id: int) -> Path:
    return Path(get_settings().imports_dir) / f"job_{job_id}_media"


def extract_export_zip(path: Path, job_id: int) -> tuple[Path, Path]:
    """Extract a Telegram export ZIP into the job's media dir; returns
    (result.json path, export root the media paths are relative to).
    ZipFile.extract sanitizes absolute/traversal member names."""
    root = job_media_dir(job_id)
    with zipfile.ZipFile(path) as z:
        z.extractall(root)
    candidates = sorted(root.rglob("result.json"), key=lambda p: len(p.parts))
    if not candidates:
        raise ValueError("ZIP contains no result.json — not a Telegram export")
    return candidates[0], candidates[0].parent


async def run_import(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: int, path: Path
) -> None:
    ok = False
    try:
        if await asyncio.to_thread(zipfile.is_zipfile, path):
            json_path, media_root = await asyncio.to_thread(extract_export_zip, path, job_id)
        else:
            json_path, media_root = path, None
        ok = await _run_import(sessionmaker, job_id, json_path, media_root)
    except Exception as e:  # noqa: BLE001 — job must record its own failure
        log.exception("import job %s failed", job_id)
        async with sessionmaker() as session:
            job = await session.get(ImportJob, job_id)
            if job is not None:
                job.status = "failed"
                job.detail = f"{type(e).__name__}: {e}"
                job.finished_at = datetime.now(timezone.utc)
                await session.commit()
    finally:
        path.unlink(missing_ok=True)
        if not ok:  # rejected/failed: nothing references the extracted media
            await asyncio.to_thread(shutil.rmtree, job_media_dir(job_id), True)


async def _run_import(
    sessionmaker: async_sessionmaker[AsyncSession],
    job_id: int,
    path: Path,
    media_root: Path | None,
) -> bool:
    async with sessionmaker() as session:
        job = await session.get(ImportJob, job_id)
        chat = await session.get(Chat, job.chat_id)
        job.status = "validating"
        await session.commit()

        meta = await asyncio.to_thread(read_export_meta, path)
        live_by_id, live_senders = await load_live_index(session, chat.id)
        scan = await asyncio.to_thread(scan_export, path, live_by_id)
        verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
        job.detail = "; ".join(verdict.reasons)
        if not verdict.ok:
            job.status = "rejected"
            job.finished_at = datetime.now(timezone.utc)
            await session.commit()
            return False
        job.status = "ingesting"
        await session.commit()

    # Ingest: skip ids that already exist (live overlap or re-run).
    async with sessionmaker() as session:
        existing_ids = set(
            (
                await session.execute(
                    select(Message.tg_message_id).where(Message.chat_id == chat.id)
                )
            ).scalars()
        )

    total = ingested = 0
    batch: list[Message] = []
    referenced_files: set[Path] = set()

    async def flush(batch: list[Message]) -> None:
        async with sessionmaker() as session:
            session.add_all(batch)
            job_row = await session.get(ImportJob, job_id)
            job_row.messages_total = total
            job_row.messages_ingested = ingested
            await session.commit()

    for m in iter_export_messages(path):
        total += 1
        if m.tg_message_id in existing_ids:
            continue
        existing_ids.add(m.tg_message_id)
        ingested += 1
        row = Message(
            chat_id=chat.id,
            tg_message_id=m.tg_message_id,
            reply_to_tg_message_id=m.reply_to_tg_message_id,
            sender_id=m.sender_id,
            sender_name=m.sender_name,
            text=m.text,
            sent_at=m.sent_at,
            source="import",
            import_job_id=job_id,
        )
        if m.media is not None:
            row.attachment = _import_attachment(
                m.media, chat.id, m.tg_message_id, job_id, media_root, referenced_files
            )
        batch.append(row)
        if len(batch) >= INSERT_BATCH:
            await flush(batch)
            batch = []
            await asyncio.sleep(0)  # keep the event loop responsive
    if batch:
        await flush(batch)

    # The extracted export also holds files nothing references (result.json,
    # not-imported media, contact photos…) — keep only what attachments need.
    if media_root is not None:
        await asyncio.to_thread(_prune_unreferenced, job_media_dir(job_id), referenced_files)

    # Imported history predates the chunk cursor — rebuild memory for the chat.
    async with sessionmaker() as session:
        await reset_chat_memory(session, chat.id)
        # The intent sweeper must never treat imported history as fresh
        # conversation: in a chat with no live traffic yet its cursor is 0,
        # and without this bump every historical window would be evaluated —
        # potentially firing workflows on conversations from months ago.
        from sqlalchemy import func as sa_func

        from app.models import ChatEvalState

        max_tg_id = (
            await session.execute(
                select(sa_func.max(Message.tg_message_id)).where(Message.chat_id == chat.id)
            )
        ).scalar() or 0
        eval_state = await session.get(ChatEvalState, chat.id)
        if eval_state is None:
            session.add(ChatEvalState(chat_id=chat.id, last_tg_message_id=max_tg_id))
        else:
            eval_state.last_tg_message_id = max(eval_state.last_tg_message_id, max_tg_id)
        job_row = await session.get(ImportJob, job_id)
        job_row.status = "done"
        job_row.messages_total = total
        job_row.messages_ingested = ingested
        job_row.finished_at = datetime.now(timezone.utc)
        await session.commit()
    log.info("import job %s done: %d/%d messages ingested", job_id, ingested, total)
    return True


def _import_attachment(
    media: ExportMedia,
    chat_id: int,
    tg_message_id: int,
    job_id: int,
    media_root: Path | None,
    referenced_files: set[Path],
) -> MessageAttachment:
    """Import media has no Telegram file_id: bytes live under the job's media
    dir (path stored relative to imports_dir) until described, then deleted.
    Media the export didn't include is `skipped` — nothing to describe, ever."""
    att = MessageAttachment(
        chat_id=chat_id,
        tg_message_id=tg_message_id,
        kind=media.kind,
        file_unique_id=f"import:{job_id}:{tg_message_id}",
        mime=media.mime,
        duration_s=media.duration_s,
        width=media.width,
        height=media.height,
        sticker_emoji=media.sticker_emoji,
    )
    file = (media_root / media.path).resolve() if media_root and media.path else None
    if file is not None and file.is_file():
        imports_dir = Path(get_settings().imports_dir).resolve()
        att.import_path = str(file.relative_to(imports_dir))
        att.size_bytes = file.stat().st_size
        referenced_files.add(file)
    else:
        att.status = "skipped"
        att.error = "media not included in the export"
    return att


def _prune_unreferenced(root: Path, referenced: set[Path]) -> None:
    if not root.is_dir():
        return
    for p in sorted(root.rglob("*"), reverse=True):  # deepest first: files, then their dirs
        if p.is_file() and p.resolve() not in referenced:
            p.unlink(missing_ok=True)
        elif p.is_dir():
            try:
                p.rmdir()  # only succeeds when emptied
            except OSError:
                pass


async def reset_chat_memory(session: AsyncSession, chat_id: int) -> None:
    """Drop chunks + cursor so the memory loop re-chunks the full history."""
    await session.execute(delete(Chunk).where(Chunk.chat_id == chat_id))
    await session.execute(delete(ChunkState).where(ChunkState.chat_id == chat_id))


async def delete_import(session: AsyncSession, job: ImportJob) -> int:
    """Surgically remove everything a (possibly poisoned) import brought in."""
    # Explicit (not FK-cascade) so sqlite tests without the FK pragma agree
    # with Postgres.
    await session.execute(
        delete(MessageAttachment).where(
            MessageAttachment.message_id.in_(
                select(Message.id).where(
                    Message.import_job_id == job.id, Message.chat_id == job.chat_id
                )
            )
        )
    )
    result = await session.execute(
        delete(Message).where(
            Message.import_job_id == job.id, Message.chat_id == job.chat_id
        )
    )
    await reset_chat_memory(session, job.chat_id)
    await asyncio.to_thread(shutil.rmtree, job_media_dir(job.id), True)
    job.status = "rejected"
    job.detail = (job.detail or "") + " [deleted by operator]"
    return result.rowcount or 0
