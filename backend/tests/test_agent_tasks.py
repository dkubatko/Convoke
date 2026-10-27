"""Per-chat identity and agent-scheduled tasks: schedule rules, the tools,
firing (incl. missed runs), task-run instructions, and the chat-page APIs."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.agents.runtime as runtime
from app.agents.deps import AgentDeps
from app.agents.identity import DEFAULT_IDENTITY, render_identity
from app.agents.runtime import execute_run
from app.agents.tools import cancel_task, list_tasks, schedule_task
from app.core.crypto import encrypt
from app.memory.embeddings import FakeEmbedder
from app.models import AgentRun, AgentTask, Bot, Chat
from app.scheduler.loop import ScheduleLoop
from app.scheduler.tasks import MAX_ACTIVE_PER_CHAT, ScheduleError, cron_next, plan_schedule
from app.telegram.limiter import SendLimiter
from tests.test_actions import ActionFakeBot
from tests.test_agent import add_agent_model, authorize_chat
from tests.test_agent_media import media_model
from tests.test_handlers import BOT_USER, message_update, run_update

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def bot_row(db_sessionmaker):
    async with db_sessionmaker() as s:
        row = Bot(tg_bot_id=BOT_USER["id"], username="convoke_bot", name="ConvokeBot",
                  token_encrypted=encrypt("123:fake"), can_read_all_group_messages=True)
        s.add(row)
        await s.commit()
        return row


# --- schedule rules ---------------------------------------------------------------


def test_one_off_limits():
    run_at, cron, ends, first = plan_schedule(NOW, NOW + timedelta(days=2), None, None)
    assert (run_at, cron, ends, first) == (NOW + timedelta(days=2), None, None, NOW + timedelta(days=2))
    for bad in (NOW - timedelta(minutes=5), NOW + timedelta(seconds=20), NOW + timedelta(days=366)):
        with pytest.raises(ScheduleError):
            plan_schedule(NOW, bad, None, None)
    with pytest.raises(ScheduleError):  # exactly one of at / repeat
        plan_schedule(NOW, NOW + timedelta(days=1), "0 9 * * *", None)
    with pytest.raises(ScheduleError):
        plan_schedule(NOW, None, None, None)


def test_recurring_limits():
    _, cron, ends, first = plan_schedule(NOW, None, "0 * * * *", None)
    assert cron == "0 * * * *" and first == NOW + timedelta(hours=1)
    assert ends == NOW + timedelta(days=365)  # default horizon
    for bad in ("*/30 * * * *", "0,30 9 1 * *", "not a cron", "0 9 * *"):
        with pytest.raises(ScheduleError):
            plan_schedule(NOW, None, bad, None)
    with pytest.raises(ScheduleError):  # ends beyond a year
        plan_schedule(NOW, None, "0 9 * * 1", NOW + timedelta(days=400))
    _, _, ends, _ = plan_schedule(NOW, None, "0 9 * * 1", NOW + timedelta(days=30))
    assert ends == NOW + timedelta(days=30)


# --- tools ------------------------------------------------------------------------


async def _member_run(db_sessionmaker, bot_row, fake, thread_id=None):
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    await run_update(db_sessionmaker, fake, bot_row, message_update(30, 20, "@convoke_bot remind me"))
    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        run.thread_id = thread_id
        await s.commit()
    ctx = SimpleNamespace(deps=AgentDeps(sessionmaker=db_sessionmaker, embedder=FakeEmbedder(),
                                         chat_id=chat.id, run_id=run.id))
    return chat, run, ctx


async def test_schedule_list_cancel(db_sessionmaker, bot_row):
    chat, run, ctx = await _member_run(db_sessionmaker, bot_row, ActionFakeBot(), thread_id=77)
    soon = datetime.now(timezone.utc) + timedelta(days=1)
    out = await schedule_task(ctx, "Remind Alice to bring the grill", at=soon)
    assert out.startswith("Scheduled task #")
    out = await schedule_task(ctx, "Post the week's events", repeat="0 9 * * 1")
    assert "recurring (0 9 * * 1 in UTC)" in out
    assert "at most once an hour" in await schedule_task(ctx, "spam", repeat="*/5 * * * *")

    async with db_sessionmaker() as s:
        tasks = (await s.execute(select(AgentTask).order_by(AgentTask.id))).scalars().all()
    assert [t.thread_id for t in tasks] == [77, 77]  # replies land where it was asked
    assert tasks[0].created_by_name == "Alice" and tasks[0].created_run_id == run.id

    listing = await list_tasks(ctx)
    assert "#%d once" % tasks[0].id in listing and "grill" in listing
    assert await cancel_task(ctx, tasks[0].id) == f"Cancelled task #{tasks[0].id}."
    assert "No task #999" in await cancel_task(ctx, 999)


async def test_task_chains_are_depth_bounded_and_capped(db_sessionmaker, bot_row):
    chat, run, ctx = await _member_run(db_sessionmaker, bot_row, ActionFakeBot())
    soon = datetime.now(timezone.utc) + timedelta(hours=2)
    async with db_sessionmaker() as s:
        parent = AgentTask(chat_id=chat.id, task="early traffic check", run_at=soon,
                           next_fire_at=soon, depth=1, created_by_name="Dina",
                           created_by_user_id=7)
        s.add(parent)
        await s.flush()
        r = await s.get(AgentRun, run.id)
        r.trigger, r.task_id = "task", parent.id
        await s.commit()
        parent_id = parent.id
    # A task's run may schedule its follow-up, one level deeper, same member.
    assert (await schedule_task(ctx, "final leave-now reminder", at=soon)).startswith("Scheduled")
    async with db_sessionmaker() as s:
        child = (await s.execute(select(AgentTask).where(AgentTask.id != parent_id))).scalar_one()
        assert (child.depth, child.created_by_name, child.created_by_user_id) == (2, "Dina", 7)
        p = await s.get(AgentTask, parent_id)
        p.depth = 3  # already at the limit
        await s.commit()
    assert "follow-ups deep" in await schedule_task(ctx, "one more", at=soon)

    async with db_sessionmaker() as s:
        (await s.get(AgentRun, run.id)).trigger = "workflow"
        await s.commit()
    assert "workflow runs can't create tasks" in await schedule_task(ctx, "x", at=soon)

    async with db_sessionmaker() as s:
        (await s.get(AgentRun, run.id)).trigger = "mention"
        for i in range(MAX_ACTIVE_PER_CHAT):
            s.add(AgentTask(chat_id=chat.id, task=f"t{i}", run_at=soon, next_fire_at=soon))
        await s.commit()
    assert "already has 25 active tasks" in await schedule_task(ctx, "one more", at=soon)


def test_timezone_wall_clock_and_dst():
    la = "America/Los_Angeles"
    # Naive local time → correct offset on each side of the Nov 1 DST change.
    run_at, *_ = plan_schedule(NOW, datetime(2026, 10, 31, 0, 0), None, None, la)
    assert run_at == datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc)  # PDT −7
    run_at, *_ = plan_schedule(NOW, datetime(2026, 11, 3, 0, 0), None, None, la)
    assert run_at == datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)  # PST −8
    # Daily midnight in LA stays midnight local across the change.
    _, cron, _, first = plan_schedule(NOW, None, "0 0 * * *", None, la)
    assert first == datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
    assert cron_next(cron, datetime(2026, 11, 1, 8, 0, tzinfo=timezone.utc), la) == (
        datetime(2026, 11, 2, 8, 0, tzinfo=timezone.utc)
    )
    with pytest.raises(ScheduleError):
        plan_schedule(NOW, None, "0 0 * * *", None, "California")


# --- firing -----------------------------------------------------------------------


async def test_scheduler_fires_advances_and_skips(db_sessionmaker, bot_row):
    chat = await authorize_chat(db_sessionmaker, ActionFakeBot(), bot_row)
    async with db_sessionmaker() as s:
        s.add_all([
            AgentTask(chat_id=chat.id, thread_id=5, task="one-off", run_at=NOW, next_fire_at=NOW),
            AgentTask(chat_id=chat.id, task="hourly", cron="0 * * * *",
                      ends_at=NOW + timedelta(days=1), next_fire_at=NOW),
            AgentTask(chat_id=chat.id, task="last", cron="0 * * * *",
                      ends_at=NOW + timedelta(minutes=30), next_fire_at=NOW),
            AgentTask(chat_id=chat.id, task="late", run_at=NOW - timedelta(hours=3),
                      next_fire_at=NOW - timedelta(hours=3)),
            AgentTask(chat_id=chat.id, task="paused", run_at=NOW, next_fire_at=NOW, enabled=False),
            AgentTask(chat_id=chat.id, task="future", run_at=NOW + timedelta(hours=1),
                      next_fire_at=NOW + timedelta(hours=1)),
        ])
        await s.commit()

    assert await ScheduleLoop(db_sessionmaker).tick(now=NOW) == 3

    async with db_sessionmaker() as s:
        t = {x.task: x for x in (await s.execute(select(AgentTask))).scalars()}
        runs = {r.request_text: r for r in (await s.execute(select(AgentRun))).scalars()}
    assert (t["one-off"].status, t["one-off"].next_fire_at) == ("done", None)
    assert runs["one-off"].trigger == "task" and runs["one-off"].thread_id == 5
    assert runs["one-off"].status == "pending"
    assert t["hourly"].status == "active"
    assert t["hourly"].next_fire_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=1)
    assert t["last"].status == "ended"  # next fire would pass its end
    assert runs["late"].status == "skipped" and "missed" in runs["late"].error
    assert t["late"].status == "done"
    assert "paused" not in runs and "future" not in runs


async def test_task_run_instructions_and_identity(db_sessionmaker, bot_row, monkeypatch):
    fake = ActionFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        c = await s.get(Chat, chat.id)
        c.identity = "You are {name}, the sarcastic pickle of {chat_title}."
        task = AgentTask(chat_id=chat.id, task="Remind everyone about the BBQ",
                         run_at=NOW, created_by_name="Dina")
        s.add(task)
        await s.flush()
        s.add(AgentRun(chat_id=chat.id, trigger="task", task_id=task.id, status="done",
                       request_text=task.task, response_text="BBQ is Saturday! 🔥"))
        run = AgentRun(chat_id=chat.id, trigger="task", task_id=task.id, request_text=task.task)
        s.add(run)
        await s.commit()
        run_id = run.id

    seen = {}
    real_build = runtime.build_agent

    def capture(model, instructions, *a, **kw):
        seen["instructions"] = instructions
        return real_build(model, instructions, *a, **kw)

    monkeypatch.setattr(runtime, "build_agent", capture)
    monkeypatch.setattr(runtime, "build_model", lambda provider: media_model([], "NO_ACTION: nothing new"))
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, run_id)

    ins = seen["instructions"]
    assert ins.startswith("You are ConvokeBot, the sarcastic pickle of Test Group.")
    assert "scheduled task #" in ins and "'Dina'" in ins
    assert "BBQ is Saturday!" in ins  # earlier run shown to avoid repeats
    assert "Format for readability" in ins  # hidden system rules still apply
    async with db_sessionmaker() as s:
        assert (await s.get(AgentRun, run_id)).status == "declined"  # tasks may stand down


def test_identity_placeholders_and_default():
    text = render_identity(None, name="Bot", username="b0t", chat_title="Jar")
    assert text.startswith('You are Bot (@b0t), an assistant participating in the Telegram group chat "Jar"')
    # Other braces in operator text pass through untouched (no str.format).
    assert render_identity("Use {braces} for {name}", name="X", username="x", chat_title="c") == (
        "Use {braces} for X"
    )


# --- chat-page APIs ---------------------------------------------------------------------


async def test_identity_api(db_sessionmaker, bot_row, client):
    chat = await authorize_chat(db_sessionmaker, ActionFakeBot(), bot_row)
    got = (await client.get(f"/api/chats/{chat.id}/identity")).json()
    assert got["identity"] == DEFAULT_IDENTITY and got["customized"] is False

    put = (await client.put(f"/api/chats/{chat.id}/identity", json={"identity": "Be terse."})).json()
    assert put["identity"] == "Be terse." and put["customized"] is True

    # Saving the default verbatim (or blank) resets — the default isn't frozen in.
    put = (await client.put(f"/api/chats/{chat.id}/identity", json={"identity": DEFAULT_IDENTITY})).json()
    assert put["customized"] is False
    async with db_sessionmaker() as s:
        assert (await s.get(Chat, chat.id)).identity is None


async def test_tasks_api_pause_resume_delete(db_sessionmaker, bot_row, client):
    chat = await authorize_chat(db_sessionmaker, ActionFakeBot(), bot_row)
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    async with db_sessionmaker() as s:
        rec = AgentTask(chat_id=chat.id, task="weekly", cron="0 9 * * 1",
                        ends_at=datetime.now(timezone.utc) + timedelta(days=60),
                        next_fire_at=past, enabled=False)
        once = AgentTask(chat_id=chat.id, task="gone", run_at=past, next_fire_at=past, enabled=False)
        s.add_all([rec, once])
        await s.flush()
        s.add(AgentRun(chat_id=chat.id, trigger="task", task_id=rec.id, status="done",
                       response_text="posted"))
        await s.commit()
        rec_id, once_id = rec.id, once.id

    listed = (await client.get(f"/api/chats/{chat.id}/tasks")).json()
    by_id = {t["id"]: t for t in listed}
    assert by_id[rec_id]["kind"] == "recurring" and by_id[rec_id]["recent_runs"][0]["response_text"] == "posted"

    # Resuming a recurring task recomputes its next fire from now — no burst.
    r = (await client.patch(f"/api/chats/{chat.id}/tasks/{rec_id}", json={"enabled": True})).json()
    assert r["enabled"] is True
    assert datetime.fromisoformat(r["next_fire_at"].replace("Z", "+00:00")) > datetime.now(timezone.utc)
    # A one-off whose time passed can't be resumed.
    assert (await client.patch(f"/api/chats/{chat.id}/tasks/{once_id}", json={"enabled": True})).status_code == 422

    assert (await client.delete(f"/api/chats/{chat.id}/tasks/{rec_id}")).status_code == 204
    async with db_sessionmaker() as s:
        assert await s.get(AgentTask, rec_id) is None
        run = (await s.execute(select(AgentRun).where(AgentRun.status == "done"))).scalar_one()
        assert run.task_id is None or run.task_id == rec_id  # SQLite doesn't enforce SET NULL
    assert (await client.get("/api/chats/999/tasks")).status_code == 404


# --- names the bot answers to -------------------------------------------------------


def test_mentions_name_whole_word_any_case():
    from app.agents.identity import mentions_name, normalize_names

    names = normalize_names([" Бот ", "bot", "Bot", "", "Огурчик"])
    assert names == ["Бот", "bot", "Огурчик"]
    for text in ("Бот, скажи", "спроси бот что-нибудь", "БОТ!", "hey bot?", "огурчик где ты"):
        assert mentions_name(text, names), text
    for text in ("робот пришёл", "ботинки", "работа", "robotics", "botany"):
        assert not mentions_name(text, names), text
    assert not mentions_name("Бот, привет", None)


async def test_name_invokes_agent_and_may_stand_down(db_sessionmaker, bot_row, monkeypatch, client):
    fake = ActionFakeBot()
    chat = await authorize_chat(db_sessionmaker, fake, bot_row)
    put = await client.put(f"/api/chats/{chat.id}/identity", json={"identity": None, "names": ["Бот", "бот", " Bot "]})
    assert put.json()["names"] == ["Бот", "Bot"]

    await run_update(db_sessionmaker, fake, bot_row, message_update(40, 41, "этот робот опять"))
    await run_update(db_sessionmaker, fake, bot_row, message_update(42, 43, "бот, как дела?"))
    async with db_sessionmaker() as s:
        runs = (await s.execute(select(AgentRun))).scalars().all()
        await add_agent_model(s)
        await s.commit()
    assert [(r.trigger, r.trigger_tg_message_id) for r in runs] == [("name", 43)]

    seen = {}
    real_build = runtime.build_agent

    def capture(model, instructions, *a, **kw):
        seen["instructions"] = instructions
        return real_build(model, instructions, *a, **kw)

    monkeypatch.setattr(runtime, "build_agent", capture)
    monkeypatch.setattr(runtime, "build_model", lambda provider: media_model([], "NO_ACTION: not for me"))
    await execute_run(db_sessionmaker, FakeEmbedder(), SendLimiter(), fake, runs[0].id)
    assert "contains one of your names" in seen["instructions"]
    assert "Members also call you by name: 'Бот', 'Bot'." in seen["instructions"]
    async with db_sessionmaker() as s:
        assert (await s.get(AgentRun, runs[0].id)).status == "declined"

    too_long = await client.put(f"/api/chats/{chat.id}/identity", json={"names": ["x" * 40]})
    assert too_long.status_code == 422
