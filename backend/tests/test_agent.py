from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.agents.runtime import execute_run, split_reply
from app.core.crypto import encrypt
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, Bot, Chat, ConnectedModel, Message, ModelRoleAssignment, Note
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


async def add_agent_model(s, name: str = "agent-model"):
    m = ConnectedModel(
        name=name, base_url="http://unused", model_name="test", capabilities={"chat": True}
    )
    s.add(m)
    await s.flush()
    s.add(ModelRoleAssignment(role="agent", model_id=m.id))


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
    """Full run: pending run → TestModel calls the remember tool → reply sent,
    persisted as self, note written, run marked done.

    Pinned to the remember tool rather than bare TestModel() (which calls every
    tool at once): pydantic-ai executes a turn's tool calls concurrently, each
    on its own session. Under Postgres those are independent connections and
    run fine, but the in-memory sqlite test DB is a single shared connection,
    so five concurrent tool sessions race and remember's commit is
    intermittently lost. The read-only tools add nothing this test asserts."""
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime

    monkeypatch.setattr(runtime, "build_model", lambda provider: TestModel(call_tools=["remember"]))

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(
        db_sessionmaker, fake, bot_row, message_update(3, 20, "@convoke_bot remember I like tea")
    )
    async with db_sessionmaker() as s:
        await add_agent_model(s)
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


async def test_workflow_run_can_decline_with_no_action(db_sessionmaker, bot_row, monkeypatch):
    """A workflow-triggered agent may stand down: a NO_ACTION reply posts
    nothing, the run records `declined`, and the fired episode is satisfied
    with the reason — so the topic won't immediately re-fire and the
    classifier can see why nothing happened."""
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime
    from app.models import IntentEpisode, Workflow

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda provider: TestModel(call_tools=[], custom_output_text="NO_ACTION: the event already exists"),
    )

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        wf = Workflow(name="events", type="intent", action_prompt="create the event",
                      trigger_prompt="t", required_slots=[], threshold=0.5)
        s.add(wf)
        await s.flush()
        run = AgentRun(chat_id=chat.id, trigger="workflow", workflow_id=wf.id,
                       request_text="create the event")
        s.add(run)
        await s.flush()
        s.add(IntentEpisode(workflow_id=wf.id, chat_id=chat.id, status="fired",
                            summary="dinner at 7", agent_run_id=run.id,
                            fired_at=datetime.now(timezone.utc)))
        await s.commit()
        run_id = run.id
        pre_self = len(fake.sent)

    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "declined", run.error
        assert run.response_text.startswith("NO_ACTION")
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "satisfied"
        assert ep.execution_summary == "Decided not to act — the event already exists"
    assert len(fake.sent) == pre_self  # nothing was posted to the chat


async def test_past_workflow_actions_tool(db_sessionmaker, bot_row):
    """The agent's view of what its workflow already did in this chat —
    excluding the action it is executing right now."""
    from types import SimpleNamespace

    from app.agents.deps import AgentDeps
    from app.agents.tools import past_workflow_actions
    from app.models import IntentEpisode, Workflow

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    async with db_sessionmaker() as s:
        wf = Workflow(name="events", type="intent", action_prompt="a", trigger_prompt="t",
                      required_slots=[], threshold=0.5)
        s.add(wf)
        await s.flush()
        s.add(IntentEpisode(
            workflow_id=wf.id, chat_id=chat.id, status="satisfied",
            summary="dinner at 7 in Palo Alto",
            slots={"time": {"value": "7pm", "confidence": 0.9}},
            execution_summary="Created the calendar event for 7pm",
            agent_run_id=41, fired_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ))
        s.add(IntentEpisode(
            workflow_id=wf.id, chat_id=chat.id, status="fired",
            summary="current task", agent_run_id=42,
            fired_at=datetime.now(timezone.utc),
        ))
        await s.commit()
        wf_id = wf.id

    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=42, workflow_id=wf_id)
    out = await past_workflow_actions(SimpleNamespace(deps=deps))
    assert "dinner at 7 in Palo Alto" in out
    assert "time: 7pm" in out
    assert "Created the calendar event for 7pm" in out
    assert "current task" not in out  # the in-flight action is excluded

    deps.workflow_id = None
    out = await past_workflow_actions(SimpleNamespace(deps=deps))
    assert "not triggered by a workflow" in out


async def test_agent_reply_formatting_reaches_telegram(db_sessionmaker, bot_row, monkeypatch):
    """Whitelisted HTML passes through to the send; anything else renders
    literally — the reply can never 400 on Telegram's HTML parser."""
    from pydantic_ai.models.test import TestModel

    import app.agents.runtime as runtime

    monkeypatch.setattr(
        runtime, "build_model",
        lambda provider: TestModel(
            call_tools=[],
            custom_output_text='<b>Dune</b> scores <i>8.0</i><div>raw</div><b>unclosed',
        ),
    )
    fake = AgentFakeBot()
    await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, message_update(3, 20, "@convoke_bot rate dune"))
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        await s.commit()
        run_id = (await s.execute(select(AgentRun.id))).scalar_one()

    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    sent = fake.sent[-1].text
    assert "<b>Dune</b>" in sent and "<i>8.0</i>" in sent
    assert "&lt;div&gt;" in sent  # disallowed tag renders literally
    assert sent.endswith("</b>")  # unclosed tag repaired


async def test_context_annotates_reply_to_off_window_target(db_sessionmaker, bot_row):
    """The recent window quotes a reply target too old to be shown, and marks
    replies to visible messages with a pointer instead of a duplicate."""
    from app.agents.context import assemble_context

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    async with db_sessionmaker() as s:
        s.add(Message(chat_id=chat.id, tg_message_id=1, sender_name="Alice",
                      text="hike Saturday at 10", sent_at=t0))
        # Enough filler that #1 falls outside the 80-message recent window.
        for i in range(10, 95):
            s.add(Message(chat_id=chat.id, tg_message_id=i, sender_name="Bob",
                          text=f"filler {i}", sent_at=t0 + timedelta(minutes=i)))
        s.add(Message(chat_id=chat.id, tg_message_id=95, sender_name="Cara",
                      text="count me in!", sent_at=t0 + timedelta(minutes=95),
                      reply_to_tg_message_id=1))
        s.add(Message(chat_id=chat.id, tg_message_id=96, sender_name="Dan",
                      text="same", sent_at=t0 + timedelta(minutes=96),
                      reply_to_tg_message_id=95))
        await s.commit()

    async with db_sessionmaker() as s:
        chat_row = await s.get(Chat, chat.id)
        ctx = await assemble_context(s, FakeEmbedder(), chat_row, "hike")
    assert '↳ replies to [#1] [2026-07-01 12:00] Alice: "hike Saturday at 10"' in ctx
    assert "(replying to #95)" in ctx  # visible target: pointer, not a quote


async def test_get_messages_tool_fetches_by_id(db_sessionmaker, bot_row):
    from types import SimpleNamespace

    from app.agents.deps import AgentDeps
    from app.agents.tools import get_messages

    fake = AgentFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    t0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
    async with db_sessionmaker() as s:
        s.add(Message(chat_id=chat.id, tg_message_id=10, sender_name="Alice",
                      text="the original plan", sent_at=t0))
        s.add(Message(chat_id=chat.id, tg_message_id=11, sender_name="Bob",
                      text="works for me", sent_at=t0 + timedelta(minutes=1),
                      reply_to_tg_message_id=10))
        await s.commit()

    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat.id, run_id=1)
    out = await get_messages(SimpleNamespace(deps=deps), [11, 10, 999])
    assert "#10: the original plan" in out
    assert "(replying to #10)" in out
    assert "#999: not in Convoke's stored history" in out


def test_extract_tool_calls_resolves_provider_and_flags_retries():
    """Tool calls are captured in order, split into provider + bare tool (MCP
    prefix stripped, our own tools → "built-in"); a call the model had to retry
    (a RetryPromptPart carrying its id) is flagged ok=False."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        RetryPromptPart,
        ToolCallPart,
        ToolReturnPart,
    )

    from app.agents.runtime import extract_tool_calls

    class FakeResult:
        def __init__(self, messages):
            self._messages = messages

        def all_messages(self):
            return self._messages

    messages = [
        ModelResponse(parts=[ToolCallPart(tool_name="movie_ratings_search", args={"title": "Ivan"}, tool_call_id="c1")]),
        ModelRequest(parts=[ToolReturnPart(tool_name="movie_ratings_search", content="{}", tool_call_id="c1")]),
        ModelResponse(parts=[ToolCallPart(tool_name="search_chat_history", args={"q": "x"}, tool_call_id="c2")]),
        ModelRequest(parts=[RetryPromptPart(content="bad args", tool_name="search_chat_history", tool_call_id="c2")]),
    ]
    prefixes = {"movie_ratings": "Movie ratings"}
    builtins = {"search_chat_history"}
    calls = extract_tool_calls(FakeResult(messages), prefixes, builtins)
    assert calls == [
        {"tool": "search", "provider": "Movie ratings", "args": '{"title":"Ivan"}', "ok": True},
        {"tool": "search_chat_history", "provider": "built-in", "args": '{"q":"x"}', "ok": False},
    ]
    # No tools called → empty list (distinct from null "unknown/pre-capture").
    assert extract_tool_calls(FakeResult([]), prefixes, builtins) == []
    # Unknown prefix falls back to a generic provider rather than crashing.
    unknown = [ModelResponse(parts=[ToolCallPart(tool_name="mystery_do", args={}, tool_call_id="c3")])]
    assert extract_tool_calls(FakeResult(unknown), {}, set()) == [
        {"tool": "mystery_do", "provider": "tool", "args": "{}", "ok": True}
    ]


async def test_get_conversation_context_windows_same_thread(db_sessionmaker):
    from types import SimpleNamespace

    from app.agents.deps import AgentDeps
    from app.agents.tools import get_conversation_context, get_messages
    from app.models import ChatThread

    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await s.flush()
        chat_id = chat.id
        t0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
        # Main thread: ids 1..9 odd/even mixed with thread 55 (ids 4,6 belong
        # to the unmonitored thread — a context window must skip them).
        for tg in (1, 2, 3, 5, 7, 8, 9):
            s.add(Message(chat_id=chat_id, tg_message_id=tg, sender_name="A",
                          text=f"main-{tg}", sent_at=t0 + timedelta(minutes=tg)))
        for tg in (4, 6):
            s.add(Message(chat_id=chat_id, tg_message_id=tg, thread_id=55, sender_name="A",
                          text=f"SECRET-{tg}", sent_at=t0 + timedelta(minutes=tg)))
        s.add(ChatThread(chat_id=chat_id, thread_key=55, monitored=False))
        await s.commit()

    deps = AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                     chat_id=chat_id, run_id=1, workflow_id=None)
    ctx = SimpleNamespace(deps=deps)

    # radius=2 around #5: two same-thread neighbours each side, thread 55 skipped
    out = await get_conversation_context(ctx, 5, radius=2)
    assert [f"main-{i}" in out for i in (2, 3, 5, 7, 8)] == [True] * 5
    assert "main-1" not in out and "main-9" not in out
    assert "SECRET" not in out

    # anchor in an unmonitored thread reads as not stored (no existence leak)
    out = await get_conversation_context(ctx, 4, radius=2)
    assert out == "#4: not in Convoke's stored history for this chat."

    # unknown anchor
    out = await get_conversation_context(ctx, 999, radius=2)
    assert "not in Convoke's stored history" in out

    # radius is clamped to the cap (asking for 500 must not blow up)
    out = await get_conversation_context(ctx, 5, radius=500)
    assert "main-1" in out and "main-9" in out and "SECRET" not in out

    # get_messages: unmonitored-thread ids render exactly like missing ones
    out = await get_messages(ctx, [3, 4, 6])
    assert "main-3" in out and "SECRET" not in out
    assert "#4: not in Convoke's stored history for this chat." in out
    assert "#6: not in Convoke's stored history for this chat." in out
