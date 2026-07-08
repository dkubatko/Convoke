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


async def test_no_overlap_but_id_matches_accepts(db_sessionmaker, tmp_path):
    """History exported up to just before the bot joined has zero overlap with
    the live tail — but a matching chat id already rules out a wrong-chat
    upload, so it's accepted as a legitimate backfill rather than rejected."""
    chat = await make_chat(db_sessionmaker, with_live=30)
    # id matches (default); messages are entirely disjoint from live history
    messages = [export_msg(i, f"old {i}") for i in range(1, 10)]
    p = write_export(tmp_path, export_payload(messages=messages))
    meta = read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert scan.matches == 0
    assert verdict.ok
    assert any("no live overlap, but chat id matches" in r for r in verdict.reasons)


async def test_no_overlap_and_wrong_id_still_rejects(db_sessionmaker, tmp_path):
    """Wrong-chat protection stands: zero overlap against substantial live
    history with a chat id that does NOT match is still a hard reject."""
    chat = await make_chat(db_sessionmaker, with_live=30)
    messages = [export_msg(i, f"old {i}") for i in range(1, 10)]
    p = write_export(tmp_path, export_payload(chat_id=999, messages=messages))
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


# ---------- media (ZIP export) ----------

import zipfile

from app.core.config import get_settings
from app.ingest.history_import import delete_import, job_media_dir, parse_export_message
from app.models import MessageAttachment


def export_media_msg(mid: int, *, caption="", photo=None, file=None, media_type=None,
                     mime=None, duration=None, unixtime=1_750_000_000) -> dict:
    item = {
        "id": mid, "type": "message", "date": "2026-06-15T12:00:00",
        "date_unixtime": str(unixtime + mid), "from": "Alice", "from_id": "user42",
        "text": caption,
    }
    if photo is not None:
        item["photo"] = photo
    if file is not None:
        item["file"] = file
        if media_type:
            item["media_type"] = media_type
        if mime:
            item["mime_type"] = mime
        if duration:
            item["duration_seconds"] = duration
    return item


def write_export_zip(tmp_path: Path, payload: dict, media: dict[str, bytes]) -> Path:
    p = tmp_path / "export.zip"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("ChatExport/result.json", json.dumps(payload))
        for rel, data in media.items():
            z.writestr(f"ChatExport/{rel}", data)
    return p


def test_parse_export_message_media_only():
    m = parse_export_message(export_media_msg(1, photo="photos/p1.jpg"))
    assert m is not None and m.text == "" and m.media.kind == "photo"
    v = parse_export_message(export_media_msg(2, file="video_files/v.mp4",
                                              media_type="voice_message", mime="audio/ogg",
                                              duration=12))
    assert v.media.kind == "voice" and v.media.duration_s == 12
    not_included = parse_export_message(
        export_media_msg(3, photo="(File not included. Change data exporting settings to download.)")
    )
    assert not_included.media.path is None
    doc = parse_export_message(export_media_msg(4, file="files/report.pdf", mime="application/pdf"))
    assert doc is None  # non-image document with no text → still dropped


async def test_zip_import_creates_pending_attachments(db_sessionmaker, tmp_path, monkeypatch):
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    payload = export_payload(messages=[
        export_msg(1, "hello text"),
        export_media_msg(2, caption="picnic!", photo="photos/p1.jpg"),
        export_media_msg(3, photo="(File not included. Change data exporting settings to download.)"),
    ])
    p = write_export_zip(tmp_path, payload, {"photos/p1.jpg": b"jpegbytes"})

    async with db_sessionmaker() as s:
        job = ImportJob(chat_id=chat.id, filename="export.zip")
        s.add(job)
        await s.commit()
        job_id = job.id

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        atts = (await s.execute(select(MessageAttachment))).scalars().all()
        by_tg = {a.tg_message_id: a for a in atts}
        assert by_tg[2].status == "pending"
        assert by_tg[2].file_id is None
        assert by_tg[2].import_path is not None
        assert by_tg[2].size_bytes == len(b"jpegbytes")
        assert by_tg[3].status == "skipped"
        assert "not included" in by_tg[3].error
        caption_msg = (
            await s.execute(select(Message).where(Message.tg_message_id == 2))
        ).scalar_one()
        assert caption_msg.text == "picnic!"
    # referenced file kept (relative to imports_dir), result.json pruned
    kept = Path(get_settings().imports_dir) / by_tg[2].import_path
    assert kept.read_bytes() == b"jpegbytes"
    assert not list(job_media_dir(job_id).rglob("result.json"))


async def test_media_loop_describes_import_file_then_discards(db_sessionmaker, tmp_path):
    get_settings().imports_dir = str(tmp_path / "imports")
    from tests.test_media_loop import FakeDescriber, make_loop

    chat = await make_chat(db_sessionmaker, with_live=0)
    media_file = Path(get_settings().imports_dir) / "job_9_media/photos/p1.jpg"
    media_file.parent.mkdir(parents=True, exist_ok=True)
    media_file.write_bytes(b"localjpeg")
    async with db_sessionmaker() as s:
        msg = Message(chat_id=chat.id, tg_message_id=7, sender_name="A", text="",
                      sent_at=datetime(2026, 6, 15, tzinfo=timezone.utc), source="import")
        msg.attachment = MessageAttachment(
            chat_id=chat.id, tg_message_id=7, kind="photo", file_id=None,
            import_path="job_9_media/photos/p1.jpg", file_unique_id="import:9:7",
            status="pending",
        )
        s.add(msg)
        from app.models import ConnectedModel, ModelRoleAssignment
        m = ConnectedModel(name="v", base_url="http://unused", model_name="m",
                           capabilities={"vision": True})
        s.add(m)
        await s.flush()
        s.add(ModelRoleAssignment(role="vision", model_id=m.id))
        # the loop resolves the bot even for import media — give the chat one
        bot_id = chat.bot_id
        await s.commit()

    loop, _ = make_loop(db_sessionmaker, bot_id, FakeDescriber())
    await loop._tick()

    async with db_sessionmaker() as s:
        att = (await s.execute(select(MessageAttachment))).scalar_one()
        assert att.status == "described"
        assert att.description == "described(localjpeg)"
    assert not media_file.exists()  # describe-then-discard


async def test_delete_import_removes_attachments_and_media_dir(db_sessionmaker, tmp_path):
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    payload = export_payload(messages=[export_media_msg(2, photo="photos/p1.jpg")])
    p = write_export_zip(tmp_path, payload, {"photos/p1.jpg": b"jpegbytes"})

    async with db_sessionmaker() as s:
        job = ImportJob(chat_id=chat.id, filename="export.zip")
        s.add(job)
        await s.commit()
        job_id = job.id
    await run_import(db_sessionmaker, job_id, p)
    assert job_media_dir(job_id).is_dir()

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        removed = await delete_import(s, job)
        await s.commit()
        assert removed == 1
        assert (await s.execute(select(MessageAttachment))).scalars().all() == []
    assert not job_media_dir(job_id).exists()
