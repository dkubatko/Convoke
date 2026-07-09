"""Regression tests for audited import/startup hardening fixes."""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

import app.ingest.history_import as hi
from app.core.config import get_settings
from app.ingest.history_import import (
    ExportMeta,
    job_media_dir,
    normalized_id_candidates,
    parse_export_message,
    run_import,
    scan_export,
    validate_export,
)
from app.models import ChatEvalState, ChatMember, ImportJob, IntentCursor, Message, MessageAttachment
from tests.test_import import (
    INTERNAL_ID,
    export_media_msg,
    export_msg,
    export_payload,
    make_chat,
    write_export,
    write_export_zip,
)


async def make_job(db_sessionmaker, chat, filename="export.json") -> int:
    async with db_sessionmaker() as s:
        job = ImportJob(chat_id=chat.id, filename=filename)
        s.add(job)
        await s.commit()
        return job.id


# ---------- fix 1: media path traversal ----------


async def test_media_path_traversal_cross_job_is_skipped(db_sessionmaker, tmp_path):
    """A crafted relative path reaching ANOTHER job's media dir must be skipped
    (never referenced — the media loop would leak, then delete, its bytes)."""
    get_settings().imports_dir = str(tmp_path / "imports")
    victim = Path(get_settings().imports_dir) / "job_999_media" / "steal.jpg"
    victim.parent.mkdir(parents=True)
    victim.write_bytes(b"victim-bytes")

    chat = await make_chat(db_sessionmaker, with_live=0)
    payload = export_payload(messages=[
        export_media_msg(2, photo="../../job_999_media/steal.jpg"),
    ])
    p = write_export_zip(tmp_path, payload, {})
    job_id = await make_job(db_sessionmaker, chat, "export.zip")

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        att = (await s.execute(select(MessageAttachment))).scalar_one()
        assert att.status == "skipped"
        assert "escapes" in att.error
        assert att.import_path is None
    assert victim.read_bytes() == b"victim-bytes"  # other job's file untouched


async def test_absolute_media_path_is_skipped_not_fatal(db_sessionmaker, tmp_path):
    """An absolute out-of-tree path used to raise from relative_to and kill the
    whole import; now the attachment is skipped and the job completes."""
    get_settings().imports_dir = str(tmp_path / "imports")
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"outside")

    chat = await make_chat(db_sessionmaker, with_live=0)
    payload = export_payload(messages=[export_media_msg(2, photo=str(outside))])
    p = write_export_zip(tmp_path, payload, {})
    job_id = await make_job(db_sessionmaker, chat, "export.zip")

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        att = (await s.execute(select(MessageAttachment))).scalar_one()
        assert att.status == "skipped"
        assert "escapes" in att.error
    assert outside.exists()


# ---------- fix 2: mid-ingest failure still runs the safety net ----------


async def test_mid_ingest_failure_still_fences_chat(db_sessionmaker, tmp_path, monkeypatch):
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    async with db_sessionmaker() as s:
        s.add(IntentCursor(workflow_id=1, chat_id=chat.id, thread_key=0, last_tg_message_id=0))
        await s.commit()

    real_iter = hi.iter_export_messages
    calls = {"n": 0}

    def flaky_iter(path, resolver=None):
        calls["n"] += 1
        if calls["n"] < 2:  # validation scan pass stays intact
            yield from real_iter(path)
            return
        for i, m in enumerate(real_iter(path)):  # ingest pass dies mid-stream
            if i == 4:
                raise RuntimeError("boom mid-ingest")
            yield m

    resets: list[int] = []

    async def fake_reset(session, chat_id):
        resets.append(chat_id)

    monkeypatch.setattr(hi, "iter_export_messages", flaky_iter)
    monkeypatch.setattr(hi, "reset_chat_memory", fake_reset)
    monkeypatch.setattr(hi, "INSERT_BATCH", 3)

    p = write_export(tmp_path, export_payload(messages=[export_msg(i, f"m {i}") for i in range(1, 9)]))
    job_id = await make_job(db_sessionmaker, chat)
    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "failed"
        assert "boom mid-ingest" in job.detail
        committed = (await s.execute(select(Message).where(Message.source == "import"))).scalars().all()
        assert len(committed) == 3  # first batch stayed committed
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 3  # fenced past the committed tail
    assert resets == [chat.id]  # memory reset ran despite the failure


# ---------- fix 3: finalization bumps IntentCursor, not the unread ChatEvalState ----------


async def test_successful_import_bumps_intent_cursors(db_sessionmaker, tmp_path):
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    async with db_sessionmaker() as s:
        s.add(IntentCursor(workflow_id=1, chat_id=chat.id, thread_key=0, last_tg_message_id=0))
        s.add(IntentCursor(workflow_id=2, chat_id=chat.id, thread_key=0, last_tg_message_id=500))
        await s.commit()

    p = write_export(tmp_path, export_payload(messages=[export_msg(i, f"m {i}") for i in range(1, 10)]))
    job_id = await make_job(db_sessionmaker, chat)
    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        by_wf = {
            c.workflow_id: c
            for c in (await s.execute(select(IntentCursor))).scalars()
        }
        assert by_wf[1].last_tg_message_id == 9  # advanced past the imported tail
        assert by_wf[2].last_tg_message_id == 500  # never moved backwards
        # the dead write is gone — nothing reads chat_eval_state
        assert (await s.execute(select(ChatEvalState))).scalars().all() == []


# ---------- fix 4: non-user senders stay out of the member roster ----------


async def test_channel_sender_not_in_member_roster(db_sessionmaker, tmp_path):
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    channel_post = export_msg(2, "channel post", sender="Some Channel")
    channel_post["from_id"] = "channel777"
    p = write_export(tmp_path, export_payload(messages=[export_msg(1, "hi"), channel_post]))
    job_id = await make_job(db_sessionmaker, chat)
    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        members = (await s.execute(select(ChatMember))).scalars().all()
        assert {m.sender_id for m in members} == {42}  # no fake member for the channel
        # Message.sender_id itself is unchanged — provenance stays on the row.
        ch_msg = (await s.execute(select(Message).where(Message.tg_message_id == 2))).scalar_one()
        assert ch_msg.sender_id == 777
        assert ch_msg.sender_name == "Some Channel"


# ---------- fix 5: export parsing robustness ----------


def test_float_duration_parsed():
    v = parse_export_message(export_media_msg(
        2, file="v.ogg", media_type="voice_message", mime="audio/ogg", duration=12.7))
    assert v.media.duration_s == 12
    v2 = parse_export_message(export_media_msg(
        3, file="v.ogg", media_type="voice_message", mime="audio/ogg", duration="9.5"))
    assert v2.media.duration_s == 9
    junk = parse_export_message(export_media_msg(
        4, file="v.ogg", media_type="voice_message", mime="audio/ogg", duration="n/a"))
    assert junk.media.duration_s is None


async def test_negative_export_id_rejects_cleanly(db_sessionmaker):
    assert normalized_id_candidates(-500) == {-500, 500}  # no ValueError
    chat = await make_chat(db_sessionmaker, with_live=0)
    meta = ExportMeta(name="Test Group", type="private_supergroup", chat_id=-INTERNAL_ID)
    verdict = validate_export(chat, meta, hi.ExportScan(), 0, set())
    assert not verdict.ok
    assert any("does not match this chat — rejected" in r for r in verdict.reasons)


# ---------- fix 6: explicit id mismatch is a hard reject ----------


async def test_id_mismatch_outvotes_title_and_participants(db_sessionmaker, tmp_path):
    """Small live history (<20) + matching title + overlapping participants
    used to reach 2 points despite a positively contradicting chat id."""
    chat = await make_chat(db_sessionmaker, with_live=5)
    messages = [export_msg(i, f"other chat {i}") for i in range(1, 6)]  # sender 42 overlaps
    p = write_export(tmp_path, export_payload(chat_id=999, name="Test Group", messages=messages))
    meta = hi.read_export_meta(p)
    async with db_sessionmaker() as s:
        live_by_id, live_senders = await hi.load_live_index(s, chat.id)
    scan = scan_export(p, live_by_id)
    verdict = validate_export(chat, meta, scan, len(live_by_id), live_senders)
    assert not verdict.ok
    assert any("does not match this chat — rejected" in r for r in verdict.reasons)


# ---------- fix 7: dedup race with live ingest ----------


async def test_dedup_race_with_live_ingest_retries_row_by_row(
    db_sessionmaker, tmp_path, monkeypatch
):
    """A live handler inserting a historical id mid-import must not fail the
    whole job on the (chat_id, tg_message_id) unique constraint."""
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)

    real_sleep = asyncio.sleep
    fired = {"done": False}

    async def racing_sleep(delay):
        # run_import sleeps between batches — sneak a "live" row with an id
        # from the NEXT batch in, after the existing_ids snapshot was taken.
        if not fired["done"]:
            fired["done"] = True
            async with db_sessionmaker() as s:
                s.add(Message(chat_id=chat.id, tg_message_id=5, sender_id=99,
                              sender_name="Racer", text="live wins",
                              sent_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
                              source="live"))
                await s.commit()
        await real_sleep(delay)

    monkeypatch.setattr(asyncio, "sleep", racing_sleep)
    monkeypatch.setattr(hi, "INSERT_BATCH", 3)

    p = write_export(tmp_path, export_payload(messages=[export_msg(i, f"m {i}") for i in range(1, 7)]))
    job_id = await make_job(db_sessionmaker, chat)
    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "done", job.detail
        assert job.messages_ingested == 5  # the collided row was skipped
        imported = (await s.execute(select(Message).where(Message.source == "import"))).scalars().all()
        assert {m.tg_message_id for m in imported} == {1, 2, 3, 4, 6}
        live = (await s.execute(select(Message).where(Message.tg_message_id == 5))).scalar_one()
        assert live.source == "live" and live.text == "live wins"


# ---------- fix 8: startup sweep of orphaned import artifacts ----------


async def test_startup_sweep_removes_orphans(db_sessionmaker, tmp_path, monkeypatch):
    from app.main import _sweep_orphaned_import_artifacts

    monkeypatch.setattr("app.main.get_sessionmaker", lambda: db_sessionmaker)
    imports = tmp_path / "imports"
    get_settings().imports_dir = str(imports)

    chat = await make_chat(db_sessionmaker, with_live=0)
    async with db_sessionmaker() as s:
        failed = ImportJob(chat_id=chat.id, filename="a.zip", status="failed")
        done = ImportJob(chat_id=chat.id, filename="b.zip", status="done")
        s.add_all([failed, done])
        await s.commit()
        failed_id, done_id = failed.id, done.id

    for job_id in (failed_id, done_id):
        d = imports / f"job_{job_id}_media"
        d.mkdir(parents=True)
        (d / "x.jpg").write_bytes(b"x")
    stale = imports / "tmp_stale_upload"
    stale.write_bytes(b"spool")
    os.utime(stale, (time.time() - 25 * 3600,) * 2)
    fresh = imports / "tmpfresh"
    fresh.write_bytes(b"spool")

    await _sweep_orphaned_import_artifacts()

    assert not (imports / f"job_{failed_id}_media").exists()  # orphan removed
    assert (imports / f"job_{done_id}_media").is_dir()  # done job's media kept
    assert not stale.exists()  # >24h spool file removed
    assert fresh.exists()  # recent spool file kept


# ---------- fix 9: zip-bomb guard ----------


async def test_zip_bomb_rejected_before_extraction(db_sessionmaker, tmp_path, monkeypatch):
    monkeypatch.setattr(hi, "MAX_EXTRACT_BYTES", 4)
    get_settings().imports_dir = str(tmp_path / "imports")
    chat = await make_chat(db_sessionmaker, with_live=0)
    p = write_export_zip(tmp_path, export_payload(messages=[export_msg(1, "hi")]), {})
    job_id = await make_job(db_sessionmaker, chat, "export.zip")

    await run_import(db_sessionmaker, job_id, p)

    async with db_sessionmaker() as s:
        job = await s.get(ImportJob, job_id)
        assert job.status == "failed"
        assert "GB cap" in job.detail
    assert not job_media_dir(job_id).exists()
