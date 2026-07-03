from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.runtime import execute_run, split_reply
from app.core.crypto import encrypt
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, Bot, Chat, Message, ModelProvider, Note
from app.telegram.limiter import SendLimiter

from tests.test_handlers import (
    ADMIN,
    FakeBot,
    callback_update,
    join_update,
    message_update,
    run_update,
    upd,
)


class AgentFakeBot(FakeBot):
    def __init__(self, member_status: str = "administrator"):
        super().__init__(member_status)
        self.actions: list[str] = []

    async def send_chat_action(self, chat_id, action, **kwargs):
        self.actions.append(action)


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(
            tg_bot_id=999,
            username="convoke_bot",
            name="ConvokeBot",
            token_encrypted=encrypt("123:fake"),
            can_read_all_group_messages=True,
        )
        s.add(row)
        await s.commit()
        return row


async def authorize_chat(db_sessionmaker, fake, bot_row):
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        from app.models import AuthNonce

        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce))
    async with db_sessionmaker() as s:
        return (await s.execute(select(Chat))).scalar_one()


async def test_mention_creates_agent_run(db_sessionmaker, bot_row):
    fake = AgentFakeBot()
    await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(
        db_sessionmaker, fake, bot_row, message_update(3, 20, "hey @convoke_bot what's up")
    )
    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.trigger == "mention"
        assert run.status == "pending"


async def test_reply_to_bot_creates_agent_run(db_sessionmaker, bot_row):
    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    reply = upd(
        3,
        message={
            "message_id": 21,
            "date": 1_780_000_200,
            "chat": {"id": chat.tg_chat_id, "type": "supergroup", "title": "Test Group"},
            "from": ADMIN,
            "text": "tell me more",
            "reply_to_message": {
                "message_id": 5,
                "date": 1_780_000_100,
                "chat": {"id": chat.tg_chat_id, "type": "supergroup"},
                "from": {"id": 999, "is_bot": True, "first_name": "ConvokeBot"},
                "text": "earlier bot reply",
            },
        },
    )
    await run_update(db_sessionmaker, fake, bot_row, reply)
    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.trigger == "reply"


async def test_plain_message_creates_no_run(db_sessionmaker, bot_row):
    fake = AgentFakeBot()
    await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, message_update(3, 20, "just chatting"))
    async with db_sessionmaker() as s:
        assert (await s.execute(select(AgentRun))).scalar_one_or_none() is None


def test_split_reply_short_passthrough():
    assert split_reply("hello") == ["hello"]


def test_split_reply_splits_on_newlines():
    text = "\n".join(f"line {i} " + "x" * 100 for i in range(80))
    parts = split_reply(text, limit=2000)
    assert 1 < len(parts) <= 3
    assert all(len(p) <= 2000 for p in parts)


async def test_execute_run_with_test_model(db_sessionmaker, bot_row, monkeypatch):
    """Full run: pending run → TestModel (calls every tool) → reply sent,
    persisted as self, run marked done."""
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime

    monkeypatch.setattr(runtime, "build_model", lambda provider: TestModel())

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(
        db_sessionmaker, fake, bot_row, message_update(3, 20, "@convoke_bot remember I like tea")
    )
    async with db_sessionmaker() as s:
        s.add(ModelProvider(role="agent", base_url="http://unused", model_name="test"))
        await s.commit()
        run_id = (await s.execute(select(AgentRun.id))).scalar_one()

    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
        assert run.response_text
        # TestModel exercised the remember tool → a note exists
        assert (await s.execute(select(Note))).scalars().first() is not None
        self_msgs = [
            m
            for m in (await s.execute(select(Message).where(Message.source == "self"))).scalars()
            if m.chat_id == chat.id
        ]
        assert len(self_msgs) >= 2  # auth prompt + agent reply
    assert "typing" in fake.actions


async def test_execute_run_without_provider_records_error(db_sessionmaker, bot_row):
    fake = AgentFakeBot()
    await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(
        db_sessionmaker, fake, bot_row, message_update(3, 20, "@convoke_bot hello")
    )
    async with db_sessionmaker() as s:
        run_id = (await s.execute(select(AgentRun.id))).scalar_one()

    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "error"
        assert "No agent model configured" in run.error
