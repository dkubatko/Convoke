from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.intent.examples import calibrate_threshold
from app.intent.executor import FireExecutor, handle_confirm_callback
from app.intent.pipeline import IntentSweeper
from app.intent.schemas import DetectVerdict
from app.memory.embeddings import FakeEmbedder
from app.models import (
    AgentRun,
    Bot,
    Chat,
    IntentCursor,
    Message,
    PendingFire,
    Workflow,
    WorkflowAssignment,
    WorkflowExample,
)
from app.scheduler.loop import ScheduleLoop
from app.telegram.limiter import SendLimiter

from tests.test_agent import AgentFakeBot

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
REQUIRED = [{"name": "date", "description": "agreed date"}, {"name": "title", "description": ""}]

# Episode/state-machine behaviour is covered by tests/test_intent_state.py
# (pure functions) and tests/test_intent_replay.py (failure-case replays).
# This file keeps: threshold calibration, prefilter mechanics, scheduler,
# runtime settings plumbing, and the fire executor.


# ---------- threshold calibration ----------

def test_calibrate_threshold_recall_first():
    pos = [[1.0, 0.0], [0.98, 0.02]]
    neg = [[0.85, 0.15]]  # best sim to a positive ≈ 0.85
    t = calibrate_threshold(pos, neg)
    # negatives anchor (0.85 + 0.01) is looser than the positives anchor — wins
    assert t == pytest.approx(0.86, abs=0.01)
    assert 0.70 <= t <= 0.88


def test_calibrate_threshold_capped_when_negatives_hug_positives():
    pos = [[1.0, 0.0], [0.99, 0.01]]
    neg = [[0.995, 0.005]]  # adversarial negative nearly on top of positives
    assert calibrate_threshold(pos, neg) <= 0.88


def test_calibrate_threshold_empty_defaults():
    assert calibrate_threshold([], []) == pytest.approx(0.80)


# ---------- shared setup ----------

async def _bot_and_chat(db_sessionmaker):
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted=encrypt("1:x"),
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", title="G",
                    status="authorized")
        s.add(chat)
        await s.commit()
        return bot, chat


async def _intent_setup(db_sessionmaker, confirm=False):
    bot, chat = await _bot_and_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        wf = Workflow(
            name="event", type="intent", action_prompt="Create the event",
            trigger_prompt="intent to schedule an event", required_slots=REQUIRED,
            confirm=confirm, cooldown_seconds=0, threshold=0.1, examples_status="ready",
        )
        s.add(wf)
        await s.flush()
        s.add(WorkflowAssignment(workflow_id=wf.id, chat_id=chat.id))
        # assignment-time cursor seed (the API does this in production)
        s.add(IntentCursor(workflow_id=wf.id, chat_id=chat.id, thread_key=0,
                           last_tg_message_id=0))
        emb = FakeEmbedder()
        vec = (await emb.embed_passages(["let's schedule dinner"]))[0]
        s.add(WorkflowExample(workflow_id=wf.id, kind="positive",
                              text="let's schedule dinner", embedding=vec))
        await s.commit()
        return bot, chat, wf.id


def _msg(chat_id, tg_id, text, minutes_ago, thread=None):
    return Message(
        chat_id=chat_id, tg_message_id=tg_id, thread_id=thread, sender_id=5,
        sender_name="Alice", text=text,
        sent_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago), source="live",
    )


class _RecordingModel:
    def __init__(self, verdict=None):
        self.prompts: list[str] = []
        self.verdict = verdict or DetectVerdict(relation="unrelated")

    async def __call__(self, session, prompt, output_type):
        self.prompts.append(prompt)
        return self.verdict


def _sweeper(db_sessionmaker, model):
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    sweeper._base = sweeper._base.model_copy(update={"intent_min_llm_interval_seconds": 0})
    sweeper._model_call = model
    return sweeper


# ---------- prefilter mechanics ----------

async def test_prefilter_passes_when_one_message_matches(db_sessionmaker):
    """A burst where only one line resembles the examples must still reach the
    classifier — scoring is per message, not per rendered window."""
    _, chat, _ = await _intent_setup(db_sessionmaker)
    model = _RecordingModel()
    sweeper = _sweeper(db_sessionmaker, model)

    async with db_sessionmaker() as s:
        # threshold high: whole-window dilution would fail; message-level max passes
        wf = (await s.execute(select(Workflow))).scalar_one()
        wf.threshold = 0.95
        s.add(_msg(chat.id, 1, "completely unrelated chatter about taxes", minutes_ago=6))
        s.add(_msg(chat.id, 2, "more noise here", minutes_ago=5))
        s.add(_msg(chat.id, 3, "let's schedule dinner", minutes_ago=4))  # == example text
        await s.commit()

    assert await sweeper.sweep() == 1
    assert model.prompts, "classifier was never reached — prefilter blocked a matching message"


async def test_prefilter_skip_consumes_window(db_sessionmaker):
    _, chat, _ = await _intent_setup(db_sessionmaker)
    model = _RecordingModel()
    sweeper = _sweeper(db_sessionmaker, model)
    async with db_sessionmaker() as s:
        wf = (await s.execute(select(Workflow))).scalar_one()
        wf.threshold = 0.95
        s.add(_msg(chat.id, 1, "completely unrelated chatter about taxes", minutes_ago=6))
        await s.commit()
    assert await sweeper.sweep() == 0
    assert not model.prompts
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_stage == "prefilter_skip"
        assert cursor.last_tg_message_id == 1  # consumed
        assert cursor.last_score is not None


async def test_sweeper_waits_for_lull(db_sessionmaker):
    _, chat, _ = await _intent_setup(db_sessionmaker)
    model = _RecordingModel()
    sweeper = _sweeper(db_sessionmaker, model)
    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, "let's schedule dinner", minutes_ago=0))  # too fresh
        await s.commit()
    assert await sweeper.sweep() == 0
    assert not model.prompts


# ---------- scheduler ----------

async def test_scheduled_workflow_fires_when_due(db_sessionmaker):
    _, chat = await _bot_and_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        wf = Workflow(name="daily", type="scheduled", action_prompt="post a summary",
                      cron="0 9 * * *", next_fire_at=NOW - timedelta(minutes=1))
        s.add(wf)
        await s.flush()
        s.add(WorkflowAssignment(workflow_id=wf.id, chat_id=chat.id))
        await s.commit()
        wf_id = wf.id

    loop = ScheduleLoop(db_sessionmaker)
    fired = await loop.tick(now=NOW)
    assert fired == 1

    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.trigger == "workflow"
        assert run.request_text == "post a summary"
        wf = await s.get(Workflow, wf_id)
        next_at = wf.next_fire_at.replace(tzinfo=timezone.utc)
        assert next_at > NOW

    assert await loop.tick(now=NOW) == 0  # not due again


async def test_scheduled_workflow_initializes_next_fire(db_sessionmaker):
    await _bot_and_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        s.add(Workflow(name="d", type="scheduled", action_prompt="x", cron="*/5 * * * *"))
        await s.commit()
    loop = ScheduleLoop(db_sessionmaker)
    assert await loop.tick(now=NOW) == 0  # first tick only sets next_fire_at
    async with db_sessionmaker() as s:
        wf = (await s.execute(select(Workflow))).scalar_one()
        assert wf.next_fire_at is not None


# ---------- runtime settings plumbing ----------

async def test_global_setting_override_reaches_the_sweeper(db_sessionmaker):
    """A global override is applied to the sweeper's effective settings on the
    next sweep (no restart)."""
    from app.core.runtime_settings import effective_settings, load_overrides, set_override

    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    base = sweeper._base.intent_min_llm_interval_seconds
    async with db_sessionmaker() as s:
        assert (await effective_settings(s, sweeper._base)).intent_min_llm_interval_seconds == base
        await set_override(s, "intent_min_llm_interval_seconds", 9)
        await s.commit()
    async with db_sessionmaker() as s:
        eff = await effective_settings(s, sweeper._base)
        assert eff.intent_min_llm_interval_seconds == 9
        await set_override(s, "intent_min_llm_interval_seconds", base)  # default clears the row
        await s.commit()
        assert "intent_min_llm_interval_seconds" not in await load_overrides(s)


async def test_chat_setting_override_used_by_sweep(db_sessionmaker):
    """A per-chat window override changes when that chat's window is ready."""
    from app.core.runtime_settings import load_chat_overrides, set_chat_override

    _, chat, _ = await _intent_setup(db_sessionmaker)
    model = _RecordingModel()
    sweeper = _sweeper(db_sessionmaker, model)
    # a 3-minute-old message: closed under the default lull, still open at a 600s lull
    async with db_sessionmaker() as s:
        await set_chat_override(s, chat.id, "intent_lull_seconds", 600)
        assert await load_chat_overrides(s, chat.id) == {"intent_lull_seconds": 600}
        s.add(_msg(chat.id, 1, "let's schedule dinner", minutes_ago=3))
        await s.commit()
    assert await sweeper.sweep() == 0  # window not ready under the per-chat 600s lull
    async with db_sessionmaker() as s:
        assert (await s.execute(select(PendingFire))).scalar_one_or_none() is None


async def test_setting_scope_and_range_validation(db_sessionmaker):
    from app.core.runtime_settings import set_chat_override, set_override

    async with db_sessionmaker() as s:
        with pytest.raises(ValueError, match="between"):
            await set_override(s, "intent_min_llm_interval_seconds", 999999)
        with pytest.raises(ValueError, match="unknown"):
            await set_override(s, "not_a_setting", 5)
        with pytest.raises(ValueError, match="not a global"):
            await set_override(s, "intent_lull_seconds", 30)  # chat-scoped
        with pytest.raises(ValueError, match="not a chat"):
            await set_chat_override(s, 1, "intent_min_llm_interval_seconds", 30)  # global


async def test_new_episode_tunables_are_registered():
    """Every episode-lifecycle knob is operator-tunable and maps to a real
    Settings field (a typo here would silently never apply)."""
    from app.core.config import get_settings
    from app.core.runtime_settings import TUNABLES

    keys = {t.key for t in TUNABLES}
    for expected in (
        "intent_classifier_concurrency",
        "intent_candidate_ttl_minutes",
        "intent_candidate_unrelated_k",
        "intent_tracking_idle_hours",
        "intent_decay_grace_hours",
        "intent_decay_per_hour_pct",
        "intent_episode_max_age_days",
        "intent_max_open_episodes",
    ):
        assert expected in keys
    settings = get_settings()
    for t in TUNABLES:
        assert hasattr(settings, t.key), f"tunable {t.key} has no Settings field"


# ---------- fire executor ----------

async def test_fire_without_confirm_creates_agent_run(db_sessionmaker):
    bot, chat, wf_id = await _intent_setup(db_sessionmaker, confirm=False)
    async with db_sessionmaker() as s:
        s.add(PendingFire(workflow_id=wf_id, chat_id=chat.id,
                          slots={"date": {"value": "Tue", "confidence": 1}}))
        await s.commit()

    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, AgentFakeBot())
    await executor._tick()

    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "done"
        run = (await s.execute(select(AgentRun))).scalar_one()
        assert run.trigger == "workflow"
        assert run.workflow_id == wf_id
        assert "Tue" in run.request_text
        assert fire.agent_run_id == run.id


async def test_poison_fire_is_isolated_not_queue_wedging(db_sessionmaker):
    """A confirmation send that always fails must mark ONLY that fire error and
    let a healthy fire in the same tick complete — not roll everything back."""

    class ExplodingBot(AgentFakeBot):
        async def send_message(self, *a, **k):
            raise RuntimeError("bot was kicked from the chat")

    bot, chat, wf_id = await _intent_setup(db_sessionmaker, confirm=True)
    async with db_sessionmaker() as s:
        # second chat + workflow whose fire fires without confirmation (healthy)
        chat2 = Chat(bot_id=bot.id, tg_chat_id=-200, type="supergroup", status="authorized")
        s.add(chat2)
        wf2 = Workflow(name="ok", type="intent", action_prompt="do it",
                       trigger_prompt="t", required_slots=[], confirm=False,
                       cooldown_seconds=0, threshold=0.1, examples_status="ready")
        s.add(wf2)
        await s.flush()
        s.add(PendingFire(workflow_id=wf_id, chat_id=chat.id, slots={}))  # needs confirm → send fails
        s.add(PendingFire(workflow_id=wf2.id, chat_id=chat2.id, slots={}))  # healthy
        await s.commit()

    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, ExplodingBot())
    await executor._tick()

    async with db_sessionmaker() as s:
        fires = {f.workflow_id: f for f in (await s.execute(select(PendingFire))).scalars()}
        assert fires[wf_id].status == "error"  # poison fire isolated
        assert fires[wf2.id].status == "done"  # healthy fire still completed
        assert (await s.execute(select(AgentRun))).scalar_one().chat_id == chat2.id


async def test_confirm_flow(db_sessionmaker):
    bot, chat, wf_id = await _intent_setup(db_sessionmaker, confirm=True)
    async with db_sessionmaker() as s:
        s.add(PendingFire(workflow_id=wf_id, chat_id=chat.id, slots={}))
        await s.commit()

    fake = AgentFakeBot()
    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, fake)
    await executor._tick()

    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "confirm_wait"
        assert fire.confirm_nonce
        nonce = fire.confirm_nonce
    assert fake.sent  # confirmation message posted

    async with db_sessionmaker() as s:
        await handle_confirm_callback(s, fake, f"wf:y:{nonce}", "Alice", "cb1")
        await s.commit()

    await executor._tick()
    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "done"
        assert (await s.execute(select(AgentRun))).scalar_one() is not None


async def test_cancel_flow(db_sessionmaker):
    bot, chat, wf_id = await _intent_setup(db_sessionmaker, confirm=True)
    async with db_sessionmaker() as s:
        s.add(PendingFire(workflow_id=wf_id, chat_id=chat.id, slots={}))
        await s.commit()

    fake = AgentFakeBot()
    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, fake)
    await executor._tick()

    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(PendingFire))).scalar_one().confirm_nonce
        await handle_confirm_callback(s, fake, f"wf:n:{nonce}", "Bob", "cb2")
        await s.commit()

    await executor._tick()
    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "cancelled"
        assert (await s.execute(select(AgentRun))).scalar_one_or_none() is None


async def test_generate_examples_stores_media_register(db_sessionmaker, monkeypatch):
    """Media positives/negatives join the example set and calibration."""
    from sqlalchemy import select

    import app.intent.examples as ex
    from app.intent.schemas import GeneratedExamples
    from app.memory.embeddings import FakeEmbedder
    from app.models import Workflow, WorkflowExample

    async with db_sessionmaker() as s:
        wf = Workflow(name="movies", type="intent", action_prompt="rate it",
                      trigger_prompt="someone mentions a movie", required_slots=[])
        s.add(wf)
        await s.commit()
        wf_id = wf.id

    generated = GeneratedExamples(
        positives=["wanna watch Moana?", "have you seen Dune?"],
        negatives=["my TV is fixed"],
        media_positives=['[photo: movie card for Shrek] wanna see', '[voice 0:05: "movie night?"]'],
        media_negatives=["[photo: a TV showing a football game]"],
    )

    async def fake_generate(session, wf):
        return generated

    monkeypatch.setattr(ex, "_generate", fake_generate)
    await ex.generate_examples(db_sessionmaker, FakeEmbedder(), wf_id)

    async with db_sessionmaker() as s:
        wf = await s.get(Workflow, wf_id)
        assert wf.examples_status == "ready"
        rows = (await s.execute(select(WorkflowExample).where(
            WorkflowExample.workflow_id == wf_id))).scalars().all()
        by_kind = {}
        for r in rows:
            by_kind.setdefault(r.kind, []).append(r.text)
        assert '[photo: movie card for Shrek] wanna see' in by_kind["positive"]
        assert "[photo: a TV showing a football game]" in by_kind["negative"]
        assert len(by_kind["positive"]) == 4 and len(by_kind["negative"]) == 2


async def test_fallback_examples_self_heal_when_provider_appears(db_sessionmaker, monkeypatch):
    """A workflow generated without a strong model sits on trigger-prompt
    fallback examples; once an agent model exists the rescue loop upgrades it.
    Without a provider, fallback is left alone (regenerating would be a
    no-op downgrade) — and a READY set is never touched."""
    from datetime import datetime, timedelta, timezone

    import app.intent.examples as ex
    from app.memory.embeddings import FakeEmbedder
    from app.models import Workflow

    stale = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with db_sessionmaker() as s:
        fb = Workflow(name="movies", type="intent", action_prompt="rate it",
                      trigger_prompt="someone mentions a movie", required_slots=[],
                      examples_status="fallback", updated_at=stale)
        ready = Workflow(name="hikes", type="intent", action_prompt="weather",
                         trigger_prompt="someone proposes a hike", required_slots=[],
                         examples_status="ready", updated_at=stale)
        s.add_all([fb, ready])
        await s.commit()
        fb_id, ready_id = fb.id, ready.id

    # Without a provider: nothing to gain — untouched.
    assert await ex.regenerate_unready(db_sessionmaker, FakeEmbedder()) == 0

    from tests.test_agent import add_agent_model
    async with db_sessionmaker() as s:
        await add_agent_model(s)
        await s.commit()

    async def fake_generate(session, wf):
        from app.intent.schemas import GeneratedExamples
        return GeneratedExamples(positives=["p"], negatives=[], media_positives=["[photo: p]"])

    monkeypatch.setattr(ex, "_generate", fake_generate)
    assert await ex.regenerate_unready(db_sessionmaker, FakeEmbedder()) == 1  # only the fallback one
    async with db_sessionmaker() as s:
        assert (await s.get(Workflow, fb_id)).examples_status == "ready"
        assert (await s.get(Workflow, ready_id)).examples_status == "ready"  # untouched
