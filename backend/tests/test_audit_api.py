"""API-contract fixes: the single-chat endpoint, interrupted uploads not
stranding a phantom `pending` import, phantom thread rows, and input caps."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from starlette.datastructures import UploadFile

from app.core.config import get_settings
from app.models import Bot, Chat, ChatMember, ChatThread, ImportJob, Message

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _seed_chat(db_sessionmaker, **chat_kwargs) -> int:
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup",
                    title="Test Group", status="authorized", **chat_kwargs)
        s.add(chat)
        await s.commit()
        return chat.id


# ---------- GET /chats/{chat_id} ----------


async def test_get_single_chat(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    got = await client.get(f"/api/chats/{cid}")
    assert got.status_code == 200
    body = got.json()
    assert body["id"] == cid
    assert body["title"] == "Test Group"
    assert body["status"] == "authorized"


async def test_get_single_chat_missing_404(client):
    got = await client.get("/api/chats/999999")
    assert got.status_code == 404


# ---------- interrupted import upload ----------


async def test_interrupted_upload_fails_job_and_unblocks(
    db_sessionmaker, client, tmp_path, monkeypatch
):
    """A body stream that dies mid-upload must not strand the job as `pending`
    — that phantom would 409-block every future import until a restart."""
    get_settings().imports_dir = str(tmp_path / "imports")
    cid = await _seed_chat(db_sessionmaker)

    async def broken_read(self, size=-1):
        raise RuntimeError("client disconnected mid-stream")

    orig_read = UploadFile.read
    monkeypatch.setattr(UploadFile, "read", broken_read)
    with pytest.raises(RuntimeError):
        await client.post(f"/api/chats/{cid}/import", files={"file": ("export.json", b"{}")})
    monkeypatch.setattr(UploadFile, "read", orig_read)

    async with db_sessionmaker() as s:
        job = (await s.execute(select(ImportJob))).scalar_one()
        assert job.status == "failed"
        assert job.detail
        assert job.finished_at is not None

    # The failed job no longer blocks: a fresh import is accepted, not 409.
    monkeypatch.setattr("app.api.chats.spawn", lambda coro, *, name=None: coro.close())
    resp = await client.post(f"/api/chats/{cid}/import", files={"file": ("export.json", b"{}")})
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending"


# ---------- phantom thread rows ----------


async def test_put_unknown_thread_key_rejected(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker, is_forum=True)
    async with db_sessionmaker() as s:
        s.add(Message(chat_id=cid, tg_message_id=1, thread_id=77, sender_name="A",
                      text="hi", sent_at=T0))
        # A captured-but-empty topic (service message only) has a row, no messages.
        s.add(ChatThread(chat_id=cid, thread_key=88, title="Movies"))
        await s.commit()

    # An arbitrary integer backed by neither messages nor a row → 404, no row minted.
    resp = await client.put(f"/api/chats/{cid}/threads/12345", json={"monitored": False})
    assert resp.status_code == 404
    async with db_sessionmaker() as s:
        assert await s.get(ChatThread, (cid, 12345)) is None

    # General (0) is always legit, even before any message lands there.
    assert (await client.put(f"/api/chats/{cid}/threads/0", json={"monitored": False})).status_code == 200
    # A key with messages works.
    assert (await client.put(f"/api/chats/{cid}/threads/77", json={"title": "Games"})).status_code == 200
    # So does a message-less captured topic.
    assert (await client.put(f"/api/chats/{cid}/threads/88", json={"monitored": False})).status_code == 200


# ---------- input length caps ----------


async def test_thread_title_length_capped(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    over = await client.put(f"/api/chats/{cid}/threads/0", json={"title": "x" * 129})
    assert over.status_code == 422
    ok = await client.put(f"/api/chats/{cid}/threads/0", json={"title": "x" * 128})
    assert ok.status_code == 200


async def test_member_display_name_length_capped(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(ChatMember(chat_id=cid, sender_id=42, auto_name="Alice"))
        await s.commit()

    single = await client.put(f"/api/chats/{cid}/members/42", json={"display_name": "x" * 65})
    assert single.status_code == 422
    batch = await client.put(
        f"/api/chats/{cid}/members", json=[{"sender_id": 42, "display_name": "x" * 65}]
    )
    assert batch.status_code == 422
    ok = await client.put(f"/api/chats/{cid}/members/42", json={"display_name": "Ali"})
    assert ok.status_code == 200
    assert ok.json()["display_name"] == "Ali"
