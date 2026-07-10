"""Chat-member identity: mapping resolution in render, latest-wins upsert,
operator override, and the members API (rename triggers a re-chunk)."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.members import (
    clean_display_name,
    load_member_names,
    refresh_members_from_messages,
    set_override_name,
    upsert_member,
)
from app.memory.chunker import render_message, render_thread
from app.models import Bot, Chat, ChatMember, Chunk, ChunkState, Message

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _seed_chat(db_sessionmaker) -> int:
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.commit()
        return chat.id


def _msg(chat_id, tg_id, sender_id, sender_name, at):
    return Message(chat_id=chat_id, tg_message_id=tg_id, sender_id=sender_id,
                   sender_name=sender_name, text="hi", sent_at=at)


def test_render_resolves_via_mapping_else_falls_back():
    m = _msg(1, 10, 555, "Данечка ❤️", T0)
    # Empty map -> raw name (fallback when a sender has no member row).
    assert "Данечка ❤️" in render_message(m, {})
    # With a mapping -> canonical name.
    assert "Daniel" in render_message(m, {555: "Daniel"})
    # A sender absent from the provided map -> falls back to the raw name.
    assert "Данечка ❤️" in render_message(m, {999: "Someone"})
    # render_thread threads names through too.
    assert "Daniel" in render_thread([m], {}, {555: "Daniel"})


async def test_upsert_latest_wins_and_handle(db_sessionmaker):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Old Name", T0)
        await upsert_member(
            s, cid, 555, "New Name", T0 + timedelta(days=1), handle="krypthos", update_handle=True
        )
        # An OLDER message must not clobber the newer name.
        await upsert_member(s, cid, 555, "Ancient", T0 - timedelta(days=1))
        # An import (update_handle omitted) must NOT clear the live handle.
        await upsert_member(s, cid, 555, "New Name", T0 + timedelta(days=2), handle=None)
        await s.commit()
        member = await s.get(ChatMember, (cid, 555))
        assert member.auto_name == "New Name"
        assert member.handle == "krypthos"


async def test_handle_cleared_when_username_removed(db_sessionmaker):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "A", T0, handle="krypthos", update_handle=True)
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).handle == "krypthos"
        # A live message with no username authoritatively clears the stale handle.
        await upsert_member(
            s, cid, 555, "A", T0 + timedelta(days=1), handle=None, update_handle=True
        )
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).handle is None


async def test_empty_name_does_not_lock_auto_name(db_sessionmaker):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        # The newest sighting has no name (deleted account / service-ish row)...
        await upsert_member(s, cid, 555, "", T0 + timedelta(days=1))
        # ...an OLDER message that DOES carry a name must still win over the empty.
        await upsert_member(s, cid, 555, "Real Name", T0)
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).auto_name == "Real Name"


async def test_import_never_clobbers_live_name_on_timestamp_tie(db_sessionmaker):
    """An overlapping export contains the SAME newest message live already saw,
    labeled with the exporter's phone-contact name at an identical sent_at —
    the tie must keep the live-derived name (imports compare strictly-newer)."""
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Daniel Kubatko", T0)  # live
        await refresh_members_from_messages(s, cid, {555: ("Мама ❤️", T0)})  # import, same ts
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).auto_name == "Daniel Kubatko"
        # A strictly newer import observation still wins, as before.
        await refresh_members_from_messages(s, cid, {555: ("Newer", T0 + timedelta(days=1))})
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).auto_name == "Newer"


async def test_same_name_resighting_advances_the_basis(db_sessionmaker):
    """Re-seeing an unchanged name must advance name_basis_at, or an import
    timestamped between two live sightings could pass the strictly-newer check."""
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Dan", T0)
        await upsert_member(s, cid, 555, "Dan", T0 + timedelta(days=5))
        await refresh_members_from_messages(
            s, cid, {555: ("Contact Label", T0 + timedelta(days=3))}
        )
        await s.commit()
        assert (await s.get(ChatMember, (cid, 555))).auto_name == "Dan"


def test_clean_display_name_neutralizes_forgery_and_bloat():
    # Newlines collapse: an embedded "\n" would forge transcript lines in the
    # line-oriented render.
    assert clean_display_name("X\nAlice [12:00] #999: I agree") == "X Alice [12:00] #999: I agree"
    assert clean_display_name("  spaced\t\tout  ") == "spaced out"
    assert len(clean_display_name("y" * 10_000)) == 64
    assert clean_display_name(None) == ""


async def test_override_equal_to_auto_name_is_a_noop_clear(db_sessionmaker):
    """Typing the auto name into the override field must not register as a
    change (it would trigger a pointless full memory rebuild)."""
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Daniel", T0)
        member, changed = await set_override_name(s, cid, 555, "Daniel")
        assert not changed
        assert member.override_name is None
        # But typing the auto name while an override exists IS a change (clears it).
        _, changed = await set_override_name(s, cid, 555, "Danny")
        assert changed
        member, changed = await set_override_name(s, cid, 555, "Daniel")
        assert changed
        assert member.override_name is None


async def test_chunk_render_resolves_names(db_sessionmaker):
    """search-hit / re-embed rendering must resolve override names too."""
    from app.memory.store import render_chunk_from_raw

    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(_msg(cid, 10, 555, "Данечка ❤️", T0))
        await upsert_member(s, cid, 555, "Данечка ❤️", T0)
        await set_override_name(s, cid, 555, "Даня")
        s.add(Chunk(chat_id=cid, thread_id=None, msg_tg_id_start=10, msg_tg_id_end=10, text="stale"))
        await s.commit()
    async with db_sessionmaker() as s:
        chunk = (await s.execute(select(Chunk).where(Chunk.chat_id == cid))).scalars().first()
        rendered = await render_chunk_from_raw(s, chunk)
        assert "Даня" in rendered and "Данечка" not in rendered


async def test_override_wins_and_clears(db_sessionmaker):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Daniel", T0)
        await s.commit()
    async with db_sessionmaker() as s:
        assert await load_member_names(s, cid) == {555: "Daniel"}
        await set_override_name(s, cid, 555, "Даня")
        await s.commit()
    async with db_sessionmaker() as s:
        assert await load_member_names(s, cid) == {555: "Даня"}
        await set_override_name(s, cid, 555, "   ")  # blank clears back to auto
        await s.commit()
    async with db_sessionmaker() as s:
        assert await load_member_names(s, cid) == {555: "Daniel"}


async def test_members_api_list_and_rename_stales_memory(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Daniel", T0)
        await upsert_member(s, cid, 777, "Sonya Kim", T0)
        # a chunk + cursor exist; a rename must stale-mark the chunk (in-place
        # refresh under the new name) WITHOUT dropping it or the cursor — the
        # old text stays searchable until the memory loop re-renders it.
        s.add(Chunk(chat_id=cid, thread_id=None, msg_tg_id_start=1, msg_tg_id_end=1, text="x"))
        s.add(ChunkState(chat_id=cid, last_tg_message_id=5))
        await s.commit()

    got = await client.get(f"/api/chats/{cid}/members")
    assert got.status_code == 200
    by_id = {m["sender_id"]: m for m in got.json()}
    assert by_id[555]["display_name"] == "Daniel" and by_id[555]["override_name"] is None

    put = await client.put(f"/api/chats/{cid}/members/555", json={"display_name": "Даня"})
    assert put.status_code == 200
    assert put.json()["display_name"] == "Даня"

    async with db_sessionmaker() as s:
        member = await s.get(ChatMember, (cid, 555))
        assert member.override_name == "Даня"
        chunks = (await s.execute(select(Chunk).where(Chunk.chat_id == cid))).scalars().all()
        cursors = (await s.execute(select(ChunkState).where(ChunkState.chat_id == cid))).scalars().all()
        assert len(chunks) == 1 and chunks[0].stale and chunks[0].content_version == 1
        assert len(cursors) == 1  # cursor untouched — no re-chunk needed

    miss = await client.put(f"/api/chats/{cid}/members/12345", json={"display_name": "X"})
    assert miss.status_code == 404


async def test_members_api_noop_rename_keeps_memory(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Daniel", T0)
        await set_override_name(s, cid, 555, "Даня")  # existing override
        s.add(Chunk(chat_id=cid, thread_id=None, msg_tg_id_start=1, msg_tg_id_end=1, text="x"))
        await s.commit()
    # PUT the SAME override value -> no-op -> memory must NOT be touched.
    r = await client.put(f"/api/chats/{cid}/members/555", json={"display_name": "Даня"})
    assert r.status_code == 200
    async with db_sessionmaker() as s:
        chunks = (await s.execute(select(Chunk).where(Chunk.chat_id == cid))).scalars().all()
        assert len(chunks) == 1 and not chunks[0].stale  # unchanged rename left memory intact


async def test_recent_messages_api_resolves_display_name(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 555, "Данечка ❤️", T0)
        await set_override_name(s, cid, 555, "Даня")
        s.add(_msg(cid, 10, 555, "Данечка ❤️", T0))  # raw name recorded on the row
        await s.commit()
    r = await client.get(f"/api/chats/{cid}/messages")
    assert r.status_code == 200
    shown = [m["sender_name"] for m in r.json()]
    assert shown == ["Даня"]  # Messages tab shows the override, not the raw name


async def test_members_api_batch_update(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 111, "Alice", T0)
        await upsert_member(s, cid, 222, "Bob", T0)
        s.add(Chunk(chat_id=cid, thread_id=None, msg_tg_id_start=1, msg_tg_id_end=1, text="x"))
        await s.commit()
    r = await client.put(
        f"/api/chats/{cid}/members",
        json=[
            {"sender_id": 111, "display_name": "Элис"},
            {"sender_id": 222, "display_name": "Боб"},
        ],
    )
    assert r.status_code == 200
    by_id = {m["sender_id"]: m for m in r.json()}
    assert by_id[111]["display_name"] == "Элис" and by_id[222]["display_name"] == "Боб"
    async with db_sessionmaker() as s:
        chunks = (await s.execute(select(Chunk).where(Chunk.chat_id == cid))).scalars().all()
        assert len(chunks) == 1 and chunks[0].stale  # staled once for the whole batch


async def test_members_api_order_is_stable_across_rename(db_sessionmaker, client):
    cid = await _seed_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        await upsert_member(s, cid, 111, "Aaa", T0)
        await upsert_member(s, cid, 222, "Bbb", T0)
        await upsert_member(s, cid, 333, "Ccc", T0)
        await s.commit()
    order1 = [m["sender_id"] for m in (await client.get(f"/api/chats/{cid}/members")).json()]
    # rename the first member to something that WOULD re-sort if we ordered by display_name
    await client.put(f"/api/chats/{cid}/members", json=[{"sender_id": 111, "display_name": "Zzz"}])
    order2 = [m["sender_id"] for m in (await client.get(f"/api/chats/{cid}/members")).json()]
    assert order1 == order2 == [111, 222, 333]  # stable despite the rename


async def test_bot_flag_toggles_and_stales_memory(db_sessionmaker, client):
    """Marking a member as bot: persists, surfaces in the API, marks the
    chat's chunks stale (renders + embedding input both change), and
    load_bot_sender_ids includes both the flag and the chat's own bot."""
    from app.members import load_bot_sender_ids
    from app.models import Bot, Chat, ChatMember, Chunk

    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=999, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        chat_id = chat.id
        s.add(ChatMember(chat_id=chat_id, sender_id=777, auto_name="TCG Bot"))
        s.add(ChatMember(chat_id=chat_id, sender_id=1, auto_name="Alice"))
        s.add(Chunk(chat_id=chat_id, thread_id=None, msg_tg_id_start=1, msg_tg_id_end=2,
                    text="x", embedding=[0.1] * 4, stale=False, content_version=0))
        await s.commit()

    got = await client.put(f"/api/chats/{chat_id}/members/777",
                           json={"display_name": None, "is_bot": True})
    assert got.status_code == 200 and got.json()["is_bot"] is True

    async with db_sessionmaker() as s:
        from sqlalchemy import select
        chunk = (await s.execute(select(Chunk))).scalar_one()
        assert chunk.stale is True  # memory refreshes under the new provenance
        ids = await load_bot_sender_ids(s, chat_id)
        assert ids == {777, 999}  # flagged member + the chat's own bot account

    # No-op save doesn't re-stale
    async with db_sessionmaker() as s:
        chunk = (await s.execute(select(Chunk))).scalar_one()
        chunk.stale = False
        await s.commit()
    await client.put(f"/api/chats/{chat_id}/members/777",
                     json={"display_name": None, "is_bot": True})
    async with db_sessionmaker() as s:
        from sqlalchemy import select
        assert (await s.execute(select(Chunk))).scalar_one().stale is False
