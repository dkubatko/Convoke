"""Per-thread monitoring: the threads API, ordinal default names, rename, and
forum-topic title capture."""
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models import Bot, Chat, ChatThread, Message

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _seed_chat(db_sessionmaker) -> int:
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup",
                    status="authorized", is_forum=True)
        s.add(chat)
        await s.commit()
        return chat.id


async def test_threads_api_list_rename_and_toggle(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        for tg, thr, txt in [(1, None, "hi"), (2, None, "yo"), (3, 77, "/roll"), (4, 77, "/spin")]:
            s.add(Message(chat_id=cid, tg_message_id=tg, thread_id=thr,
                          sender_name="A", text=txt, sent_at=T0))
        await s.commit()

    got = await client.get(f"/api/chats/{cid}/threads")
    assert got.status_code == 200
    by_key = {t["thread_key"]: t for t in got.json()}
    assert by_key[0]["default_name"] == "General" and by_key[0]["name"] == "General"
    assert by_key[77]["default_name"] == "Topic #1"  # ordinal for the one topic
    assert by_key[77]["message_count"] == 2
    assert by_key[0]["monitored"] and by_key[77]["monitored"]  # default on
    assert [m["text"] for m in by_key[77]["preview"]] == ["/roll", "/spin"]  # oldest-first

    # Rename (display only) + turn monitoring off.
    put = await client.put(f"/api/chats/{cid}/threads/77", json={"title": "Gacha", "monitored": False})
    assert put.status_code == 200
    t = {x["thread_key"]: x for x in put.json()}[77]
    assert t["name"] == "Gacha" and t["title"] == "Gacha" and t["monitored"] is False

    # Blank title clears back to the ordinal default.
    put = await client.put(f"/api/chats/{cid}/threads/77", json={"title": "   "})
    t = {x["thread_key"]: x for x in put.json()}[77]
    assert t["title"] is None and t["name"] == "Topic #1"
    assert t["monitored"] is False  # unchanged by a title-only update


async def test_capture_thread_title_from_forum_event(db_sessionmaker):
    from app.telegram.handlers import _capture_thread_title

    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        chat = await s.get(Chat, cid)
        created = SimpleNamespace(
            forum_topic_created=SimpleNamespace(name="Movies"),
            forum_topic_edited=None, message_thread_id=88, message_id=88,
        )
        await _capture_thread_title(s, chat, created)
        await s.commit()
    async with db_sessionmaker() as s:
        row = await s.get(ChatThread, (cid, 88))
        assert row is not None and row.title == "Movies" and row.monitored is True

    # A later edit renames it.
    async with db_sessionmaker() as s:
        chat = await s.get(Chat, cid)
        edited = SimpleNamespace(
            forum_topic_created=None,
            forum_topic_edited=SimpleNamespace(name="Films"), message_thread_id=88, message_id=999,
        )
        await _capture_thread_title(s, chat, edited)
        await s.commit()
    async with db_sessionmaker() as s:
        assert (await s.get(ChatThread, (cid, 88))).title == "Films"
