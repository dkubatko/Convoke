"""Runtime hardening: down MCP servers, run deadline, mid-send durability,
provider-resolution failures, and chat-lock/semaphore ordering."""

import asyncio
from datetime import datetime, timezone

from pydantic_ai.models.test import TestModel
from sqlalchemy import select

import app.agents.runtime as runtime
from app.agents.loop import AgentLoop
from app.agents.runtime import execute_run
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, Bot, Chat, IntentEpisode, Message, Workflow
from app.telegram.limiter import SendLimiter

from tests.test_agent import AgentFakeBot, add_agent_model


class DownToolset:
    """A toolset whose connection fails — a configured MCP server that's down."""

    label = "MCPToolset 'mcp_down'"

    async def __aenter__(self):
        raise ConnectionError("connection refused")

    async def __aexit__(self, *exc):
        return None


class FlakySendBot(AgentFakeBot):
    """send_message fails on the given (1-based) call numbers."""

    def __init__(self, fail_on: set[int]):
        super().__init__()
        self.fail_on = fail_on
        self.send_calls = 0

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self.send_calls += 1
        if self.send_calls in self.fail_on:
            raise RuntimeError("telegram down")
        return await super().send_message(chat_id, text, reply_markup=reply_markup, **kwargs)


async def _make_run(db_sessionmaker, *, trigger="mention", with_episode=False) -> int:
    """A pending AgentRun in an authorized chat with an agent model assigned;
    optionally linked to a fired workflow episode."""
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=999, username="cb", name="cb", token_encrypted="x",
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", status="authorized")
        s.add(chat)
        await add_agent_model(s)
        await s.flush()
        workflow_id = None
        if with_episode:
            wf = Workflow(name="events", type="intent", action_prompt="a",
                          trigger_prompt="t", required_slots=[], threshold=0.5)
            s.add(wf)
            await s.flush()
            workflow_id = wf.id
        run = AgentRun(chat_id=chat.id, trigger=trigger, workflow_id=workflow_id,
                       request_text="do the thing")
        s.add(run)
        await s.flush()
        if with_episode:
            s.add(IntentEpisode(workflow_id=workflow_id, chat_id=chat.id, status="fired",
                                summary="dinner at 7", agent_run_id=run.id,
                                fired_at=datetime.now(timezone.utc)))
        await s.commit()
        return run.id


async def test_down_mcp_server_does_not_kill_run(db_sessionmaker, monkeypatch):
    """A toolset whose __aenter__ raises is dropped (with a warning) and the
    run — which needs no tools — still completes and replies."""
    monkeypatch.setattr(runtime, "build_model", lambda provider: TestModel(call_tools=[]))
    run_id = await _make_run(db_sessionmaker)

    fake = AgentFakeBot()
    await execute_run(
        db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id,
        extra_toolsets=[DownToolset()],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
    assert fake.sent  # the reply landed despite the dead server


async def test_live_mcp_tool_survives_a_down_neighbour(db_sessionmaker, monkeypatch):
    """One server down, one up: the survivor's tools stay callable."""
    from fastmcp import FastMCP
    from pydantic_ai.mcp import MCPToolset

    calls: list[str] = []
    demo = FastMCP("demo-calendar")

    @demo.tool
    def create_event(title: str) -> str:
        """Create a calendar event."""
        calls.append(title)
        return f"created {title}"

    monkeypatch.setattr(runtime, "build_model", lambda provider: TestModel())
    run_id = await _make_run(db_sessionmaker)

    await execute_run(
        db_sessionmaker, FakeEmbedder(), SendLimiter(), AgentFakeBot(), run_id,
        extra_toolsets=[DownToolset(), MCPToolset(demo)],
    )

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done", run.error
    assert calls, "the live MCP tool was never called"


async def test_run_deadline_marks_error(db_sessionmaker, monkeypatch):
    """A wedged model call hits the deadline and takes the normal error path:
    run → error with a clear message, failure notice posted."""

    class WedgedModel(TestModel):
        async def request(self, messages, model_settings, model_request_parameters):
            await asyncio.sleep(5)
            return await super().request(messages, model_settings, model_request_parameters)

    monkeypatch.setattr(runtime, "build_model", lambda provider: WedgedModel())
    monkeypatch.setattr(runtime, "AGENT_RUN_DEADLINE_S", 0.05)
    run_id = await _make_run(db_sessionmaker)

    fake = AgentFakeBot()
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "error"
        assert "deadline" in run.error
    assert any("went wrong" in m.text for m in fake.sent)  # user was notified


async def test_mid_send_failure_keeps_delivered_parts_and_episode(db_sessionmaker, monkeypatch):
    """Part 2 of a split reply fails to send: part 1's Message row and the
    run's outcome survive, the episode stays satisfied (the workflow's action
    already happened), and no generic failure notice is posted."""
    two_parts = "\n".join("line " + "x" * 90 for _ in range(60))  # ~5.7k chars → 2 parts
    monkeypatch.setattr(
        runtime, "build_model",
        lambda provider: TestModel(call_tools=[], custom_output_text=two_parts),
    )
    run_id = await _make_run(db_sessionmaker, trigger="workflow", with_episode=True)

    fake = FlakySendBot(fail_on={2})
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done"  # NOT reverted by the send failure
        assert run.response_text == two_parts  # full reply is in the record
        assert "delivery failed after 1 part(s)" in run.error
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "satisfied"  # the action ran; must not re-fire
        # part 1 is persisted as the bot's own memory
        self_msgs = (
            (await s.execute(select(Message).where(Message.source == "self"))).scalars().all()
        )
        assert len(self_msgs) == 1
        assert self_msgs[0].text.startswith("line ")
    assert len(fake.sent) == 1  # part 1 only — no failure notice on top


async def test_zero_parts_delivered_posts_failure_notice(db_sessionmaker, monkeypatch):
    """The very first send fails: the outcome is still recorded (done, reply
    stored, episode satisfied) but the chat gets the failure notice — silence
    would read as a crash."""
    monkeypatch.setattr(
        runtime, "build_model",
        lambda provider: TestModel(call_tools=[], custom_output_text="short reply"),
    )
    run_id = await _make_run(db_sessionmaker, trigger="workflow", with_episode=True)

    fake = FlakySendBot(fail_on={1})  # reply fails; the notice (call 2) succeeds
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "done"
        assert run.response_text == "short reply"
        assert "delivery failed after 0 part(s)" in run.error
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "satisfied"
    assert len(fake.sent) == 1 and "went wrong" in fake.sent[0].text


async def test_provider_resolution_error_does_not_strand_run(db_sessionmaker, monkeypatch):
    """A non-ProviderNotConfigured failure during provider resolution (e.g. a
    DB error) must mark the run error, not leave it 'running' forever."""

    async def boom(session, role):
        raise RuntimeError("db boom")

    monkeypatch.setattr(runtime, "get_provider", boom)
    run_id = await _make_run(db_sessionmaker)

    fake = AgentFakeBot()
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    async with db_sessionmaker() as s:
        run = await s.get(AgentRun, run_id)
        assert run.status == "error"
        assert "db boom" in run.error


async def test_queued_run_does_not_hold_concurrency_slot(monkeypatch):
    """A run queued behind its chat's lock must not occupy a global slot: with
    2 slots, chat A running + chat A queued must leave a slot free for chat B."""
    import app.agents.loop as loop_mod

    started: list[int] = []
    release: dict[int, asyncio.Event] = {i: asyncio.Event() for i in (1, 2, 3)}

    async def fake_execute(sessionmaker, embedder, limiter, bot, run_id):
        started.append(run_id)
        await release[run_id].wait()

    monkeypatch.setattr(loop_mod, "execute_run", fake_execute)
    loop = AgentLoop(None, None, None)
    loop._semaphore = asyncio.Semaphore(2)

    t1 = asyncio.create_task(loop._run_locked(1, 100, None))  # chat A, runs
    await asyncio.sleep(0.01)
    t2 = asyncio.create_task(loop._run_locked(2, 100, None))  # chat A, queued on the lock
    t3 = asyncio.create_task(loop._run_locked(3, 200, None))  # chat B
    await asyncio.sleep(0.05)

    # Chat B got the second slot; chat A's queued run waits WITHOUT a slot.
    assert started == [1, 3]
    for ev in release.values():
        ev.set()
    await asyncio.gather(t1, t2, t3)
    assert sorted(started) == [1, 2, 3]
