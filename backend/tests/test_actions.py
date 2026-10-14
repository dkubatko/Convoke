"""Agent chat actions: send_poll, close_poll, react, send_location — tool
validation, post-reply delivery, persistence, and failure handling."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Message as TgMessage
from aiogram.types import Poll as TgPoll
from sqlalchemy import select

from app.agents.deps import AgentDeps
from app.agents.runtime import execute_run
from app.agents.tools import close_poll, react, send_location, send_poll
from app.core.crypto import encrypt
from app.media.render import message_body
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, Bot, Message, Poll, PollVote
from app.telegram.limiter import SendLimiter
from tests.test_agent import add_agent_model, authorize_chat
from tests.test_agent_media import MediaFakeBot, bad_request, media_model
from tests.test_handlers import BOT_USER, MEMBER, media_update, message_update, run_update, upd


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(tg_bot_id=BOT_USER["id"], username="convoke_bot", name="ConvokeBot",
                  token_encrypted=encrypt("123:fake"), can_read_all_group_messages=True)
        s.add(row)
        await s.commit()
        return row


class ActionFakeBot(MediaFakeBot):
    def __init__(self):
        super().__init__()
        self.fail_actions: Exception | None = None

    def _msg(self, chat_id, **content) -> TgMessage:
        self._next_id += 1
        return TgMessage.model_validate({
            "message_id": self._next_id,
            "date": int(datetime.now(timezone.utc).timestamp()),
            "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
            "from": BOT_USER,
            **content,
        })

    def _check(self):
        if self.fail_actions is not None:
            raise self.fail_actions

    async def send_poll(self, chat_id, question, options, **kwargs):
        self._check()
        self.events.append(("poll", question, kwargs))
        return self._msg(chat_id, poll={
            "id": f"bp-{self._next_id + 1}", "question": question,
            "options": [{"text": o.text, "voter_count": 0, "persistent_id": str(i)}
                        for i, o in enumerate(options)],
            "total_voter_count": 0, "is_closed": False,
            "is_anonymous": kwargs["is_anonymous"], "type": "regular",
            "allows_multiple_answers": kwargs["allows_multiple_answers"],
            "allows_revoting": True, "members_only": False,
        })

    async def stop_poll(self, chat_id, message_id):
        self._check()
        self.events.append(("stop_poll", message_id, {}))
        return TgPoll.model_validate({
            "id": "bp-final", "question": "q", "total_voter_count": 2, "is_closed": True,
            "is_anonymous": False, "type": "regular", "allows_multiple_answers": False,
            "allows_revoting": True, "members_only": False,
            "options": [{"text": "a", "voter_count": 2, "persistent_id": "0"},
                        {"text": "b", "voter_count": 0, "persistent_id": "1"}],
        })

    async def send_location(self, chat_id, latitude, longitude, **kwargs):
        self._check()
        self.events.append(("location", (latitude, longitude), kwargs))
        return self._msg(chat_id, location={"latitude": latitude, "longitude": longitude})

    async def send_venue(self, chat_id, latitude, longitude, title, address, **kwargs):
        self._check()
        self.events.append(("venue", title, kwargs))
        loc = {"latitude": latitude, "longitude": longitude}
        return self._msg(chat_id, location=loc,
                         venue={"location": loc, "title": title, "address": address})

    async def set_message_reaction(self, chat_id, message_id, reaction=None, **kwargs):
        self._check()
        self.events.append(("reaction", message_id, reaction[0].emoji))
        return True


async def run_agent(db_sessionmaker, bot_row, fake, monkeypatch, tool_calls, text="here you go",
                    before=None):
    import app.agents.runtime as runtime

    monkeypatch.setattr(runtime, "build_model", lambda provider: media_model(tool_calls, text))
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    if before is not None:
        await before(chat)
    await run_update(db_sessionmaker, fake, bot_row, message_update(30, 20, "@convoke_bot do it"))
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        await s.commit()
        run_id = (await s.execute(select(AgentRun.id))).scalar_one()
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)
    async with db_sessionmaker() as s:
        return chat, await s.get(AgentRun, run_id)


def ctx_for(db_sessionmaker, chat_id):
    return SimpleNamespace(deps=AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                                          chat_id=chat_id, run_id=1))


async def test_send_poll_posts_after_text_and_tracks_votes(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()
    chat, run = await run_agent(db_sessionmaker, bot_row, fake, monkeypatch, [
        ("send_poll", {"question": "Dina's birthday <3", "options": ["Sat 9", "Sat 16"],
                       "multiple_answers": True}),
    ], text="Here's a poll:")
    assert run.status == "done" and run.error is None
    assert [e[0] for e in fake.events[-2:]] == ["text", "poll"]
    poll_event = fake.events[-1]
    assert poll_event[1] == "Dina's birthday &lt;3"  # escaped for the HTML parse mode
    assert poll_event[2]["is_anonymous"] is False  # votes visible by default
    assert run.media == [{"kind": "poll", "source": "action", "ok": True}]

    async with db_sessionmaker() as s:
        poll = (await s.execute(select(Poll))).scalar_one()
        assert poll.sent_by_bot and poll.total_voters == 0
        msg = await s.get(Message, poll.message_id)
        assert msg.source == "self"
        assert "[poll «Dina's birthday &lt;3», multiple answers: Sat 9 / Sat 16]" == message_body(msg)

    # The vote that follows carries only the poll id — the link made at send.
    await run_update(db_sessionmaker, fake, bot_row, upd(40, poll_answer={
        "poll_id": poll.tg_poll_id, "user": MEMBER, "option_ids": [1],
        "option_persistent_ids": ["1"]}))
    async with db_sessionmaker() as s:
        vote = (await s.execute(select(PollVote))).scalar_one()
        assert (vote.voter_id, vote.option_ids) == (MEMBER["id"], ["1"])


async def test_reaction_alone_sends_no_text(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()
    _, run = await run_agent(db_sessionmaker, bot_row, fake, monkeypatch, [
        ("react", {"message_id": 20, "emoji": "❤️"}),  # variation selector normalized
    ], text="NO_TEXT")
    assert run.status == "done" and run.error is None
    # The reaction is the whole answer: it's the run's last event, and no
    # "(no reply)" text precedes it.
    assert fake.events[-1] == ("reaction", 20, "❤")
    assert not any(e[0] == "text" and "(no reply)" in e[1] for e in fake.events)


async def test_rejected_poll_tells_the_chat(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()

    async def arm(chat):
        fake.fail_actions = bad_request()

    _, run = await run_agent(db_sessionmaker, bot_row, fake, monkeypatch, [
        ("send_poll", {"question": "When?", "options": ["Fri", "Sat"]}),
    ], before=arm)
    assert run.status == "done"
    assert run.error == "some actions failed"
    assert run.media == [{"kind": "poll", "source": "action", "ok": False}]
    assert fake.events[-1][0] == "text" and "send the poll" in fake.events[-1][1]


async def test_close_poll_only_for_own_open_polls(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()
    member_poll = {"id": "mp", "question": "q",
                   "options": [{"text": "a", "voter_count": 0, "persistent_id": "0"}],
                   "total_voter_count": 0, "is_closed": False, "is_anonymous": False,
                   "type": "regular", "allows_multiple_answers": False,
                   "allows_revoting": True, "members_only": False}
    bot_poll = {**member_poll, "id": "bp"}

    async def seed(chat):
        await run_update(db_sessionmaker, fake, bot_row, media_update(10, 11, poll=member_poll))
        await run_update(db_sessionmaker, fake, bot_row,
                         media_update(11, 12, poll=bot_poll, sender=BOT_USER))
        ctx = ctx_for(db_sessionmaker, chat.id)
        assert "posted by a member" in await close_poll(ctx, 11)
        assert "is not a poll" not in await close_poll(ctx, 12)

    _, run = await run_agent(db_sessionmaker, bot_row, fake, monkeypatch,
                             [("close_poll", {"message_id": 12})], before=seed)
    assert run.status == "done" and run.error is None
    assert ("stop_poll", 12, {}) in fake.events
    async with db_sessionmaker() as s:
        poll = (await s.execute(select(Poll).where(Poll.tg_message_id == 12))).scalar_one()
        assert poll.closed and poll.total_voters == 2


async def test_send_location_and_venue(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()
    _, run = await run_agent(db_sessionmaker, bot_row, fake, monkeypatch, [
        ("send_location", {"latitude": 55.75, "longitude": 37.62, "place_name": "Café Pushkin",
                           "address": "Tverskoy Blvd 26A"}),
    ])
    assert run.status == "done" and run.error is None
    async with db_sessionmaker() as s:
        msg = (await s.execute(select(Message).where(Message.content.is_not(None)))).scalar_one()
        assert msg.source == "self"
        assert message_body(msg) == "[venue: Café Pushkin, Tverskoy Blvd 26A (55.75000, 37.62000)]"


async def test_tool_validation(db_sessionmaker, bot_row):
    fake = ActionFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, message_update(30, 20, "hello"))
    ctx = ctx_for(db_sessionmaker, chat.id)

    assert "isn't an allowed reaction" in await react(ctx, 20, "🦖")
    assert "not in Convoke's stored history" in await react(ctx, 999, "👍")
    await react(ctx, 20, "👍")
    await react(ctx, 20, "🔥")  # replaces: one reaction per message
    assert [a.params["emoji"] for a in ctx.deps.actions] == ["🔥"]

    assert "1–12 options" in await send_poll(ctx, "q", [])
    assert "distinct" in await send_poll(ctx, "q", ["a", "a"])
    assert "between" in await send_poll(ctx, "q", ["a"], close_at=datetime(2020, 1, 1))
    assert "both place_name and address" in await send_location(ctx, 1.0, 2.0, place_name="X")
    assert "out of range" in await send_location(ctx, 95.0, 2.0)

    for _ in range(4):
        await send_location(ctx, 1.0, 2.0)
    assert "Action limit reached" in await send_location(ctx, 1.0, 2.0)
