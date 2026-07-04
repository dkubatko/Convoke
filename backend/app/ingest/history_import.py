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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

import ijson
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Chat, Chunk, ChunkState, ImportJob, Message

log = logging.getLogger("convoke.import")

INSERT_BATCH = 500
MIN_LIVE_FOR_OVERLAP_CHECK = 20


@dataclass
class ExportMeta:
    name: str | None = None
    type: str | None = None
    chat_id: int | None = None


@dataclass
class ExportMessage:
    tg_message_id: int
    sender_name: str
    sender_id: int | None
    text: str
    sent_at: datetime
    thread_id: int | None = None
    reply_to_tg_message_id: int | None = None


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


def parse_export_message(item: dict) -> ExportMessage | None:
    if item.get("type") != "message":
        return None
    text = flatten_text(item.get("text", ""))
    if not text.strip():
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


async def run_import(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: int, path: Path
) -> None:
    try:
        await _run_import(sessionmaker, job_id, path)
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


async def _run_import(
    sessionmaker: async_sessionmaker[AsyncSession], job_id: int, path: Path
) -> None:
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
            return
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
        batch.append(
            Message(
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
        )
        if len(batch) >= INSERT_BATCH:
            await flush(batch)
            batch = []
            await asyncio.sleep(0)  # keep the event loop responsive
    if batch:
        await flush(batch)

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


async def reset_chat_memory(session: AsyncSession, chat_id: int) -> None:
    """Drop chunks + cursor so the memory loop re-chunks the full history."""
    await session.execute(delete(Chunk).where(Chunk.chat_id == chat_id))
    await session.execute(delete(ChunkState).where(ChunkState.chat_id == chat_id))


async def delete_import(session: AsyncSession, job: ImportJob) -> int:
    """Surgically remove everything a (possibly poisoned) import brought in."""
    result = await session.execute(
        delete(Message).where(
            Message.import_job_id == job.id, Message.chat_id == job.chat_id
        )
    )
    await reset_chat_memory(session, job.chat_id)
    job.status = "rejected"
    job.detail = (job.detail or "") + " [deleted by operator]"
    return result.rowcount or 0
