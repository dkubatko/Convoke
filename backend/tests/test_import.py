import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.ingest.history_import import (
    ExportMeta,
    load_live_index,
    read_export_meta,
    run_import,
    scan_export,
    validate_export,
)
from app.models import Bot, Chat, ImportJob, Message

INTERNAL_ID = 1234567890
TG_CHAT_ID = int(f"-100{INTERNAL_ID}")


def export_payload(chat_id=INTERNAL_ID, name="Test Group", messages=None) -> dict:
    return {
        "name": name,
        "type": "private_supergroup",
        "id": chat_id,
        "messages": messages if messages is not None else [],
    }


def export_msg(mid: int, text, sender="Alice", sender_id=42, unixtime=1_750_000_000) -> dict:
    return {
        "id": mid,
        "type": "message",
        "date": "2026-06-15T12:00:00",
        "date_unixtime": str(unixtime + mid),
        "from": sender,
        "from_id": f"user{sender_id}",
        "text": text,
    }


def write_export(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "export.json"
    p.write_text(json.dumps(payload))
    return p


async def make_chat(db_sessionmaker, with_live: int = 0) -> Chat:
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=TG_CHAT_ID, type="supergroup",
                    title="Test Group", status="authorized")
        s.add(chat)
        await s.flush()
        for i in range(with_live):
            s.add(Message(
                chat_id=chat.id, tg_message_id=1000 + i, sender_id=42,
                sender_name="Alice", text=f"live message {i}",
                sent_at=datetime(2026, 6, 20, tzinfo=timezone.utc), source="live",
            ))
        await s.commit()
        return chat


def test_read_export_meta(tmp_path):
    p = write_export(tmp_path, export_payload(messages=[export_msg(1, "hello")]))
    meta = read_export_meta(p)
    assert meta.chat_id == INTERNAL_ID
    assert meta.name == "Test Group"


def test_entity_array_text_flattened(tmp_path):
    payload = export_payload(messages=[
        export_msg(1, ["see ", {"type": "link", "text": "https://x.y"}, " ok"]),
    ])
    p = write_export(tmp_path, payload)
    scan = scan_export(p, {})
    assert scan.total == 1


async def test_id_match_alone_passes_for_fresh_chat(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=0)
    p = write_export(tmp_path, export_payload(messages=[export_msg(1, "hi")]))
    meta = read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert verdict.ok


async def test_wrong_chat_id_and_title_rejected(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=0)
    p = write_export(tmp_path, export_payload(chat_id=999, name="Other Group"))
    meta = read_export_meta(p)
    verdict = validate_export(chat, meta, scan_export(p, {}), 0, set())
    assert not verdict.ok


async def test_contradicting_overlap_hard_rejects(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=30)
    messages = [export_msg(1000 + i, f"FORGED {i}") for i in range(10)]
    p = write_export(tmp_path, export_payload(messages=messages))
    meta = read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert not verdict.ok
    assert any("contradict" in r for r in verdict.reasons)


async def test_no_overlap_with_substantial_live_history_rejects(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=30)
    # id matches but messages are entirely disjoint from live history
    messages = [export_msg(i, f"old {i}") for i in range(1, 10)]
    p = write_export(tmp_path, export_payload(messages=messages))
    meta = read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert not verdict.ok
    assert any("no overlap" in r for r in verdict.reasons)


async def test_matching_overlap_passes(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=30)
    messages = [export_msg(i, f"old {i}") for i in range(1, 10)]
    messages += [export_msg(1000 + i, f"live message {i}") for i in range(5)]
    p = write_export(tmp_path, export_payload(messages=messages))
    meta = read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert verdict.ok
    assert scan.matches == 5


async def test_run_import_end_to_end(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=30)
    messages = [export_msg(i, f"old {i}") for i in range(1, 10)]
    messages += [export_msg(1000 + i, f"live message {i}") for i in range(5)]  # dedup
    messages += [{"id": 500, "type": "service", "action": "pin_message"}]  # skipped
    p = write_export(tmp_path, export_payload(messages=messages))

    async with db_sessionmaker() as s:
        job = ImportJob(chat_id=chat.id, filename="export.json")
        s.add(job)
        await s.commit()
        job_id = job.id

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        assert job.messages_ingested == 9  # 9 old; 5 overlapping deduped
        imported = (
            await s.execute(select(Message).where(Message.source == "import"))
        ).scalars().all()
        assert len(imported) == 9
        assert all(m.import_job_id == job_id for m in imported)
    assert not p.exists()  # upload cleaned up


async def test_rejected_import_ingests_nothing(db_sessionmaker, tmp_path):
    chat = await make_chat(db_sessionmaker, with_live=30)
    messages = [export_msg(1000 + i, f"FORGED {i}") for i in range(10)]
    p = write_export(tmp_path, export_payload(messages=messages))

    async with db_sessionmaker() as s:
        job = ImportJob(chat_id=chat.id, filename="export.json")
        s.add(job)
        await s.commit()
        job_id = job.id

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "rejected"
        count = len(
            (await s.execute(select(Message).where(Message.source == "import"))).scalars().all()
        )
        assert count == 0
