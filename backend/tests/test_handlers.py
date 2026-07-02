from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from aiogram.types import Message as TgMessage
from aiogram.types import Update
from sqlalchemy import select

from app.models import AuthNonce, Bot, Chat, Message
from app.telegram.handlers import handle_update

CHAT_ID = -1001234567890
ADMIN = {"id": 42, "is_bot": False, "first_name": "Alice"}
MEMBER = {"id": 43, "is_bot": False, "first_name": "Mallory"}
BOT_USER = {"id": 999, "is_bot": True, "first_name": "ConvokeBot", "username": "convoke_bot"}
GROUP = {"id": CHAT_ID, "type": "supergroup", "title": "Test Group"}


class FakeBot:
    """Records aiogram calls; get_chat_member returns a configurable status."""

    def __init__(self, member_status: str = "member"):
        self.member_status = member_status
        self.sent: list[TgMessage] = []
        self.answered: list[tuple] = []
        self.edited: list[tuple] = []
        self._next_id = 1000

    async def send_message(self, chat_id, text, reply_markup=None):
        self._next_id += 1
        msg = TgMessage.model_validate(
            {
                "message_id": self._next_id,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": chat_id, "type": "supergroup", "title": "Test Group"},
                "from": BOT_USER,
                "text": text,
            }
        )
        self.sent.append(msg)
        return msg

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answered.append((callback_query_id, text, show_alert))

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=self.member_status)

    async def edit_message_text(self, chat_id, message_id, text):
        self.edited.append((chat_id, message_id, text))


def upd(update_id: int, **payload) -> Update:
    return Update.model_validate({"update_id": update_id, **payload})


def join_update(update_id: int = 1) -> Update:
    return upd(
        update_id,
        my_chat_member={
            "chat": GROUP,
            "from": ADMIN,
            "date": 1_780_000_000,
            "old_chat_member": {"user": BOT_USER, "status": "left"},
            "new_chat_member": {"user": BOT_USER, "status": "member"},
        },
    )


def message_update(update_id: int, message_id: int, text: str, sender=None) -> Update:
    return upd(
        update_id,
        message={
            "message_id": message_id,
            "date": 1_780_000_100,
            "chat": GROUP,
            "from": sender or ADMIN,
            "text": text,
        },
    )


def callback_update(update_id: int, nonce: str, sender=None) -> Update:
    return upd(
        update_id,
        callback_query={
            "id": f"cb{update_id}",
            "from": sender or ADMIN,
            "chat_instance": "ci",
            "data": f"auth:{nonce}",
        },
    )


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(
            tg_bot_id=999,
            username="convoke_bot",
            name="ConvokeBot",
            token_encrypted="x",
            can_read_all_group_messages=True,
        )
        s.add(row)
        await s.commit()
        return row


async def run_update(db_sessionmaker, fake_bot, bot_row, update):
    async with db_sessionmaker() as s:
        row = await s.get(Bot, bot_row.id)
        await handle_update(s, fake_bot, row, update)
        await s.commit()


async def test_join_creates_pending_chat_and_auth_prompt(db_sessionmaker, bot_row):
    fake = FakeBot()
    await run_update(db_sessionmaker, fake, bot_row, join_update())

    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
        assert chat.status == "pending_auth"
        assert chat.title == "Test Group"
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
        assert nonce.used_at is None
        assert nonce.tg_message_id == fake.sent[0].message_id
        # the prompt itself is persisted as a self message
        self_msg = (await s.execute(select(Message))).scalar_one()
        assert self_msg.source == "self"
    assert "authorize" in fake.sent[0].text.lower()


async def test_messages_before_authorization_are_not_stored(db_sessionmaker, bot_row):
    fake = FakeBot()
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    await run_update(db_sessionmaker, fake, bot_row, message_update(2, 10, "secret pre-auth"))

    async with db_sessionmaker() as s:
        texts = [m.text for m in (await s.execute(select(Message))).scalars()]
        assert "secret pre-auth" not in texts


async def test_non_admin_cannot_authorize(db_sessionmaker, bot_row):
    fake = FakeBot(member_status="member")
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce, sender=MEMBER))

    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
        assert chat.status == "pending_auth"
    assert fake.answered[-1][2] is True  # alert shown


async def test_admin_authorizes_chat(db_sessionmaker, bot_row):
    fake = FakeBot(member_status="administrator")
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce))

    async with db_sessionmaker() as s:
        chat = (await s.execute(select(Chat))).scalar_one()
        assert chat.status == "authorized"
        assert chat.authorized_by_user_id == ADMIN["id"]
        used = (await s.execute(select(AuthNonce))).scalar_one()
        assert used.used_at is not None
    assert fake.edited  # prompt message was edited


async def test_used_nonce_is_rejected(db_sessionmaker, bot_row):
    fake = FakeBot(member_status="administrator")
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce))
    await run_update(db_sessionmaker, fake, bot_row, callback_update(3, nonce.nonce))

    assert "expired" in fake.answered[-1][1].lower()


async def test_authorized_chat_stores_messages_idempotently(db_sessionmaker, bot_row):
    fake = FakeBot(member_status="administrator")
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce))

    msg = message_update(3, 20, "hello world")
    await run_update(db_sessionmaker, fake, bot_row, msg)
    await run_update(db_sessionmaker, fake, bot_row, msg)  # crash-replay

    async with db_sessionmaker() as s:
        rows = [
            m
            for m in (await s.execute(select(Message))).scalars()
            if m.source == "live"
        ]
        assert len(rows) == 1
        assert rows[0].text == "hello world"
        assert rows[0].sender_name == "Alice"


async def test_edited_message_updates_text(db_sessionmaker, bot_row):
    fake = FakeBot(member_status="administrator")
    await run_update(db_sessionmaker, fake, bot_row, join_update())
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(AuthNonce))).scalar_one()
    await run_update(db_sessionmaker, fake, bot_row, callback_update(2, nonce.nonce))
    await run_update(db_sessionmaker, fake, bot_row, message_update(3, 20, "hello world"))

    edited = upd(
        4,
        edited_message={
            "message_id": 20,
            "date": 1_780_000_100,
            "edit_date": 1_780_000_200,
            "chat": GROUP,
            "from": ADMIN,
            "text": "hello edited",
        },
    )
    await run_update(db_sessionmaker, fake, bot_row, edited)

    async with db_sessionmaker() as s:
        row = (
            await s.execute(select(Message).where(Message.tg_message_id == 20))
        ).scalar_one()
        assert row.text == "hello edited"
        assert row.edited_at is not None
