"""Polls, structured content (locations, checklists…) and generic files:
ingestion, live updates, rendering, the get_poll tool, and history import."""

from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.deps import AgentDeps
from app.agents.tools import get_poll
from app.ingest.history_import import parse_export_message
from app.media.inspect import file_text
from app.media.render import message_body
from app.memory.embeddings import FakeEmbedder
from app.models import Bot, Chat, Chunk, ChunkState, Message, MessageAttachment, Poll, PollVote
from tests.test_handlers import (
    ADMIN,
    BOT_USER,
    GROUP,
    MEMBER,
    FakeBot,
    authorize,
    media_update,
    run_update,
    upd,
)

POLL = {
    "id": "p-1",
    "question": "др дина число",
    "description": "чачлык",
    "options": [
        {"text": "6 января сб", "voter_count": 0, "persistent_id": "0"},
        {"text": "7 января воскр", "voter_count": 0, "persistent_id": "1"},
        {"text": "13 января сб", "voter_count": 0, "persistent_id": "2"},
    ],
    "total_voter_count": 0,
    "is_closed": False,
    "is_anonymous": False,
    "type": "regular",
    "allows_multiple_answers": True,
    "allows_revoting": True,
    "members_only": False,
}


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(tg_bot_id=BOT_USER["id"], username="convoke_bot", name="ConvokeBot",
                  token_encrypted="x", can_read_all_group_messages=True)
        s.add(row)
        await s.commit()
        return row


def poll_snapshot(counts, closed=False, poll=POLL):
    return {
        **poll,
        "options": [{**o, "voter_count": c} for o, c in zip(poll["options"], counts)],
        "total_voter_count": max(counts) if counts else 0,
        "is_closed": closed,
    }


async def setup(db_sessionmaker, bot_row) -> FakeBot:
    fake = FakeBot(member_status="administrator")
    await authorize(db_sessionmaker, fake, bot_row)
    return fake


async def stored(db_sessionmaker, tg_message_id) -> Message | None:
    async with db_sessionmaker() as s:
        return (
            await s.execute(select(Message).where(Message.tg_message_id == tg_message_id))
        ).scalar_one_or_none()


async def tool_ctx(db_sessionmaker):
    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=1)
    return SimpleNamespace(deps=deps)


# --- polls -----------------------------------------------------------------


async def test_member_poll_is_stored_with_unknown_votes(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, poll=POLL))

    row = await stored(db_sessionmaker, 20)
    assert row is not None and row.poll is not None
    p = row.poll
    assert (p.tg_poll_id, p.sent_by_bot, p.multiple_answers, p.anonymous) == ("p-1", False, True, False)
    # A member's open poll: Telegram never reports its votes, so counts are
    # unknown — not zero.
    assert p.counts_at is None and p.total_voters is None
    assert all(o["votes"] is None for o in p.options)
    assert message_body(row) == (
        "[poll «др дина число» (чачлык), multiple answers: "
        "6 января сб / 7 января воскр / 13 января сб]"
    )

    out = await get_poll(await tool_ctx(db_sessionmaker), 20)
    assert "by Alice" in out and "open" in out
    assert "Votes: unknown" in out


async def test_bot_poll_tracks_snapshots_and_individual_votes(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 30, poll=POLL, sender=BOT_USER))
    row = await stored(db_sessionmaker, 30)
    assert row.poll.sent_by_bot and row.poll.total_voters == 0  # known zero at sending

    answer = lambda uid, user, ids: upd(uid, poll_answer={  # noqa: E731
        "poll_id": "p-1", "user": user, "option_ids": [int(i) for i in ids],
        "option_persistent_ids": ids,
    })
    await run_update(db_sessionmaker, fake, bot_row, answer(4, ADMIN, ["2"]))
    await run_update(db_sessionmaker, fake, bot_row, answer(5, MEMBER, ["0", "2"]))
    await run_update(db_sessionmaker, fake, bot_row, upd(6, poll=poll_snapshot([1, 0, 2])))

    out = await get_poll(await tool_ctx(db_sessionmaker), 30)
    assert "sent by you" in out
    assert "• 13 января сб — 2: Alice, Mallory" in out
    assert "• 6 января сб — 1: Mallory" in out
    assert "• 7 января воскр — 0" in out

    # A changed vote replaces the row; a retracted one deletes it.
    await run_update(db_sessionmaker, fake, bot_row, answer(7, MEMBER, ["1"]))
    await run_update(db_sessionmaker, fake, bot_row, answer(8, ADMIN, []))
    async with db_sessionmaker() as s:
        votes = {v.voter_id: v.option_ids for v in (await s.execute(select(PollVote))).scalars()}
    assert votes == {MEMBER["id"]: ["1"]}


async def test_closed_poll_shows_final_tally_and_refreshes_memory(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, poll=POLL))
    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
        s.add(Chunk(chat_id=chat.id, msg_tg_id_start=10, msg_tg_id_end=25, text="x",
                    stale=False))
        await s.commit()

    # Stopping a member's poll is the one time Telegram reports its votes.
    await run_update(db_sessionmaker, fake, bot_row, upd(4, poll=poll_snapshot([0, 1, 3], closed=True)))

    row = await stored(db_sessionmaker, 20)
    assert row.poll.closed and row.poll.counts_at is not None
    assert "closed: 6 января сб (0) / 7 января воскр (1) / 13 января сб (3)" in message_body(row)
    async with db_sessionmaker() as s:
        assert (await s.execute(select(Chunk))).scalar_one().stale is True


async def test_poll_update_for_unknown_poll_is_ignored(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, upd(3, poll={**poll_snapshot([1, 0, 0]), "id": "nope"}))
    async with db_sessionmaker() as s:
        assert (await s.execute(select(Poll))).first() is None


async def test_poll_option_added_and_deleted(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, poll=POLL))
    ref = {"message_id": 20, "date": 1_780_000_100, "chat": GROUP}
    await run_update(db_sessionmaker, fake, bot_row, media_update(
        4, 21, poll_option_added={"option_persistent_id": "9", "option_text": "14 января воскр",
                                  "poll_message": ref}))
    await run_update(db_sessionmaker, fake, bot_row, media_update(
        5, 22, poll_option_deleted={"option_persistent_id": "0", "option_text": "6 января сб",
                                    "poll_message": ref}))

    row = await stored(db_sessionmaker, 20)
    assert [o["text"] for o in row.poll.options] == ["7 января воскр", "13 января сб", "14 января воскр"]
    assert await stored(db_sessionmaker, 21) is None  # the service message itself isn't content


async def test_reply_to_unseen_poll_stores_the_poll(db_sessionmaker, bot_row):
    """Last night's case: a reply to a poll Convoke never stored."""
    fake = await setup(db_sessionmaker, bot_row)
    original = {"message_id": 20, "date": 1_780_000_000, "chat": GROUP, "from": MEMBER, "poll": POLL}
    await run_update(db_sessionmaker, fake, bot_row, media_update(
        3, 21, reply_to=original, **{"text": "бот здесь правильные даты?"}))

    target = await stored(db_sessionmaker, 20)
    assert target is not None and target.poll.question == "др дина число"


# --- structured content ------------------------------------------------------


async def test_static_content_types_render(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    cases = {
        20: ({"location": {"latitude": 55.751244, "longitude": 37.618423}},
             "[location 55.75124, 37.61842]"),
        21: ({"venue": {"location": {"latitude": 55.75, "longitude": 37.62}, "title": "Café Pushkin",
                        "address": "Tverskoy Blvd 26A"},
              "location": {"latitude": 55.75, "longitude": 37.62}},
             "[venue: Café Pushkin, Tverskoy Blvd 26A (55.75000, 37.62000)]"),
        22: ({"contact": {"phone_number": "+15550001", "first_name": "Gabe", "last_name": "K"}},
             "[contact: Gabe K, +15550001]"),
        23: ({"dice": {"emoji": "🎲", "value": 5}}, "[🎲 dice roll: 5]"),
    }
    for i, (msg_id, (payload, _)) in enumerate(cases.items()):
        await run_update(db_sessionmaker, fake, bot_row, media_update(10 + i, msg_id, **payload))
    for msg_id, (_, expected) in cases.items():
        assert message_body(await stored(db_sessionmaker, msg_id)) == expected


async def test_live_location_moves_without_touching_memory(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    live = {"latitude": 55.0, "longitude": 37.0, "live_period": 3600}
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, location=live))
    moved = {"message_id": 20, "date": 1_780_000_100, "edit_date": 1_780_000_200, "chat": GROUP,
             "from": ADMIN, "location": {**live, "latitude": 55.5}}
    await run_update(db_sessionmaker, fake, bot_row, upd(4, edited_message=moved))

    row = await stored(db_sessionmaker, 20)
    assert row.content["latitude"] == 55.5
    assert row.edited_at is None  # a position report, not an edit
    assert message_body(row) == "[live location 55.50000, 37.00000]"


async def test_checklist_ticks_and_additions(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    checklist = {"title": "Шашлык", "tasks": [{"id": 1, "text": "мясо"}, {"id": 2, "text": "угли"}]}
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, checklist=checklist))
    ref = {"message_id": 20, "date": 1_780_000_100, "chat": GROUP}
    await run_update(db_sessionmaker, fake, bot_row, media_update(
        4, 21, sender=MEMBER,
        checklist_tasks_done={"checklist_message": ref, "marked_as_done_task_ids": [2]}))
    await run_update(db_sessionmaker, fake, bot_row, media_update(
        5, 22, checklist_tasks_added={"checklist_message": ref, "tasks": [{"id": 3, "text": "вино"}]}))

    row = await stored(db_sessionmaker, 20)
    assert message_body(row) == "[checklist «Шашлык»: ☐ мясо / ✓ угли (Mallory) / ☐ вино]"


async def test_generic_file_is_recorded_not_described(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    doc = {"file_id": "d-1", "file_unique_id": "u-d-1", "file_name": "menu.pdf",
           "mime_type": "application/pdf", "file_size": 2_400_000}
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, document=doc))

    row = await stored(db_sessionmaker, 20)
    att = row.attachment
    assert (att.kind, att.file_name, att.status) == ("file", "menu.pdf", "skipped")
    assert message_body(row) == "[file menu.pdf, 2.3 MB]"


async def test_service_messages_still_store_nothing(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, new_chat_members=[MEMBER]))
    assert await stored(db_sessionmaker, 20) is None


async def test_late_message_refreshes_the_covering_chunk(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
        s.add(ChunkState(chat_id=chat.id, last_tg_message_id=50))
        s.add(Chunk(chat_id=chat.id, msg_tg_id_start=10, msg_tg_id_end=40, text="x",
                    stale=False))
        await s.commit()
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, poll=POLL))
    async with db_sessionmaker() as s:
        assert (await s.execute(select(Chunk))).scalar_one().stale is True


# --- files -------------------------------------------------------------------


def test_file_text_formats():
    assert file_text(b"a,b\n1,2", "text/csv", "x.csv") == "a,b\n1,2"
    assert file_text(b'{"k": 1}', "application/octet-stream", "data.json") == '{"k": 1}'
    assert file_text(b"PK\x03\x04", "application/zip", "a.zip") is None


# --- history import ------------------------------------------------------------


def _export(**fields):
    return {"id": 5, "type": "message", "date_unixtime": "1780000000", "from": "Dina",
            "from_id": "user7", "text": "", **fields}


def test_export_poll_counts_known_and_hidden():
    m = parse_export_message(_export(poll={
        "question": "pizza?", "closed": True, "total_voters": 3,
        "answers": [{"text": "yes", "voters": 2, "chosen": True},
                    {"text": "no", "voters": 1, "chosen": False}]}))
    assert m.poll["options"] == [{"id": "0", "text": "yes", "votes": 2},
                                 {"id": "1", "text": "no", "votes": 1}]
    # An exporter who never voted sees only the total: per-option is unknown.
    m = parse_export_message(_export(poll={
        "question": "pizza?", "closed": False, "total_voters": 3,
        "answers": [{"text": "yes", "voters": 0}, {"text": "no", "voters": 0}]}))
    assert [o["votes"] for o in m.poll["options"]] == [None, None]
    assert m.poll["total_voters"] == 3


def test_export_content_types():
    loc = parse_export_message(_export(location_information={
        "latitude": Decimal("55.75"), "longitude": Decimal("37.61")}))
    assert loc.content == {"type": "location", "latitude": 55.75, "longitude": 37.61}
    assert isinstance(loc.content["latitude"], float)  # JSON-storable

    venue = parse_export_message(_export(place_name="Café", address="Main St"))
    assert venue.content == {"type": "venue", "title": "Café", "address": "Main St"}
    assert message_body(SimpleNamespace(attachment=None, poll=None, content=venue.content, text="")) == (
        "[venue: Café, Main St]"
    )

    todo = parse_export_message(_export(todo_list={
        "title": "Trip", "answers": [{"text": "tickets", "id": 1}]}))
    assert message_body(SimpleNamespace(attachment=None, poll=None, content=todo.content, text="")) == (
        "[checklist «Trip»: tickets]"  # completion state unknown in exports
    )

    f = parse_export_message(_export(file="files/a.zip", file_name="a.zip", mime_type="application/zip"))
    assert (f.media.kind, f.media.file_name, f.media.path) == ("file", "a.zip", None)


async def test_poll_row_cascades_with_its_message(db_sessionmaker, bot_row):
    fake = await setup(db_sessionmaker, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, media_update(3, 20, poll=POLL))
    async with db_sessionmaker() as s:
        msg = (await s.execute(select(Message).where(Message.tg_message_id == 20))).scalar_one()
        await s.delete(msg)
        await s.commit()
        assert (await s.execute(select(Poll))).first() is None
        assert (await s.execute(select(MessageAttachment))).first() is None


