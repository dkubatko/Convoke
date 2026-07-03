from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.core.crypto import encrypt
from app.intent.examples import calibrate_threshold
from app.intent.executor import FireExecutor, handle_confirm_callback
from app.intent.pipeline import IntentSweeper
from app.intent.schemas import IntentVerdict, SlotUpdate
from app.intent.state import apply_verdict, decay_state, is_converged
from app.memory.embeddings import FakeEmbedder
from app.models import (
    AgentRun,
    Bot,
    Chat,
    Message,
    PendingFire,
    TriggerState,
    Workflow,
    WorkflowAssignment,
    WorkflowExample,
)
from app.scheduler.loop import ScheduleLoop
from app.telegram.limiter import SendLimiter

from tests.test_agent import AgentFakeBot

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
REQUIRED = [{"name": "date", "description": "agreed date"}, {"name": "title", "description": ""}]


# ---------- state machine ----------

def verdict(match=True, confidence=0.9, updates=()):
    return IntentVerdict(match=match, confidence=confidence, slot_updates=list(updates))


def test_apply_fills_and_overwrites():
    slots = apply_verdict({}, verdict(updates=[SlotUpdate(name="date", value="Tue 7pm", confidence=0.8)]), NOW, 10)
    assert slots["date"]["value"] == "Tue 7pm"
    slots = apply_verdict(slots, verdict(updates=[SlotUpdate(name="date", value="Wed 8pm", confidence=0.9)]), NOW, 12)
    assert slots["date"]["value"] == "Wed 8pm"  # last-write-wins


def test_retraction_clears_slot():
    slots = {"date": {"value": "Tue", "confidence": 0.9, "message_id": 1, "ts": "x"}}
    slots = apply_verdict(slots, verdict(updates=[SlotUpdate(name="date", value=None, confidence=0.9)]), NOW, 2)
    assert "date" not in slots


def test_low_confidence_update_ignored():
    slots = apply_verdict({}, verdict(updates=[SlotUpdate(name="date", value="maybe Tue?", confidence=0.3)]), NOW, 1)
    assert slots == {}


def test_no_match_leaves_state():
    before = {"date": {"value": "Tue", "confidence": 0.9, "message_id": 1, "ts": "x"}}
    assert apply_verdict(before, verdict(match=False, updates=[SlotUpdate(name="date", value=None, confidence=1)]), NOW, 2) == before


def test_ttl_decay():
    slots = {"date": {"value": "Tue", "confidence": 0.9, "message_id": 1, "ts": "x"}}
    fresh = decay_state(slots, NOW - timedelta(hours=10), NOW, timedelta(hours=36))
    assert fresh == slots
    stale = decay_state(slots, NOW - timedelta(hours=40), NOW, timedelta(hours=36))
    assert stale == {}


def test_convergence_requires_all_slots_confident():
    assert not is_converged(REQUIRED, {"date": {"value": "Tue", "confidence": 0.9}})
    assert not is_converged(
        REQUIRED,
        {"date": {"value": "Tue", "confidence": 0.9}, "title": {"value": "dinner", "confidence": 0.4}},
    )
    assert is_converged(
        REQUIRED,
        {"date": {"value": "Tue", "confidence": 0.9}, "title": {"value": "dinner", "confidence": 0.8}},
    )


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


async def test_prefilter_passes_when_one_message_matches(db_sessionmaker, monkeypatch):
    """A burst where only one line resembles the examples must still reach the
    classifier — scoring is per message, not per rendered window."""
    _, chat, _ = await _intent_setup(db_sessionmaker)
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    seen_windows: list[str] = []

    async def fake_classify(session, wf, tstate, rendered):
        seen_windows.append(rendered)
        return verdict(match=False)

    monkeypatch.setattr(sweeper, "_classify", fake_classify)

    async with db_sessionmaker() as s:
        # threshold high: whole-window dilution would fail; message-level max passes
        from app.models import Workflow as Wf
        wf = (await s.execute(select(Wf))).scalar_one()
        wf.threshold = 0.95
        s.add(_msg(chat.id, 1, "completely unrelated chatter about taxes", minutes_ago=6))
        s.add(_msg(chat.id, 2, "more noise here", minutes_ago=5))
        s.add(_msg(chat.id, 3, "let's schedule dinner", minutes_ago=4))  # == example text
        await s.commit()

    assert await sweeper.sweep() == 1
    assert seen_windows, "classifier was never reached — prefilter blocked a matching message"


# ---------- scheduler ----------

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


# ---------- intent sweeper integration ----------

async def _intent_setup(db_sessionmaker, confirm=False):
    bot, chat = await _bot_and_chat(db_sessionmaker)
    async with db_sessionmaker() as s:
        wf = Workflow(
            name="event", type="intent", action_prompt="Create the event",
            trigger_prompt="intent to schedule an event", required_slots=REQUIRED,
            confirm=confirm, cooldown_seconds=3600, threshold=0.1, examples_status="ready",
        )
        s.add(wf)
        await s.flush()
        s.add(WorkflowAssignment(workflow_id=wf.id, chat_id=chat.id))
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


async def test_intent_pipeline_converges_and_fires_once(db_sessionmaker, monkeypatch):
    _, chat, wf_id = await _intent_setup(db_sessionmaker)
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())

    verdicts = iter([
        verdict(updates=[SlotUpdate(name="date", value="Tue 7pm", confidence=0.9)]),
        verdict(updates=[SlotUpdate(name="title", value="dinner", confidence=0.9)]),
    ])

    async def fake_classify(session, wf, tstate, rendered):
        return next(verdicts)

    monkeypatch.setattr(sweeper, "_classify", fake_classify)
    sweeper.settings = sweeper.settings.model_copy(
        update={"intent_min_llm_interval_seconds": 0}
    )

    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, "let's schedule dinner", minutes_ago=5))
        await s.commit()
    assert await sweeper.sweep() == 1  # window 1: partial slots, no fire

    async with db_sessionmaker() as s:
        assert (await s.execute(select(PendingFire))).scalar_one_or_none() is None
        state = (await s.execute(select(TriggerState))).scalar_one()
        assert "date" in state.slots
        assert state.last_stage == "accumulating"
        assert state.last_score is not None  # prefilter ran and passed
        assert state.last_confidence == 0.9
        assert state.last_evaluated_at is not None

    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 2, "call it dinner, Tuesday works", minutes_ago=3))
        await s.commit()
    assert await sweeper.sweep() == 1  # window 2: converges

    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "pending"
        assert fire.slots["date"]["value"] == "Tue 7pm"
        state = (await s.execute(select(TriggerState))).scalar_one()
        assert state.slots == {}  # reset after firing
        assert state.cooldown_until is not None
        assert state.last_stage == "fired"

    # cooldown: further messages don't re-fire
    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 3, "let's schedule dinner again", minutes_ago=1))
        await s.commit()
    await sweeper.sweep()
    async with db_sessionmaker() as s:
        fires = (await s.execute(select(PendingFire))).scalars().all()
        assert len(fires) == 1
        state = (await s.execute(select(TriggerState))).scalar_one()
        assert state.last_stage == "cooldown"


async def test_classifier_error_keeps_cursor_for_retry(db_sessionmaker, monkeypatch):
    """When the classifier fails (model endpoint down), the window must NOT be
    consumed — it should retry once the model is back, not be lost forever."""
    _, chat, _ = await _intent_setup(db_sessionmaker)
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    sweeper.settings = sweeper.settings.model_copy(update={"intent_min_llm_interval_seconds": 0})

    async def failing_classify(session, wf, tstate, rendered):
        return None  # simulates ProviderNotConfigured / endpoint error

    monkeypatch.setattr(sweeper, "_classify", failing_classify)

    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, "let's schedule dinner", minutes_ago=5))
        await s.commit()
    await sweeper.sweep()

    async with db_sessionmaker() as s:
        ts = (await s.execute(select(TriggerState))).scalar_one()
        assert ts.last_tg_message_id == 0, "window was consumed despite classifier failure"

    # model recovers → same window now classifies and converges
    verdicts = iter([verdict(updates=[])])

    async def working_classify(session, wf, tstate, rendered):
        return next(verdicts)

    monkeypatch.setattr(sweeper, "_classify", working_classify)
    await sweeper.sweep()
    async with db_sessionmaker() as s:
        ts = (await s.execute(select(TriggerState))).scalar_one()
        assert ts.last_tg_message_id == 1  # advanced after success


async def test_cooling_workflow_keeps_messages_for_after_cooldown(db_sessionmaker, monkeypatch):
    """A workflow in cooldown must NOT consume matching messages that arrive
    during the cooldown — it should evaluate (and fire on) them once the
    cooldown lifts, even if another workflow evaluated the same window."""
    _, chat, wf_id = await _intent_setup(db_sessionmaker)  # no required slots → fires on match
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    sweeper.settings = sweeper.settings.model_copy(update={"intent_min_llm_interval_seconds": 0})

    async def always_match(session, wf, tstate, rendered):
        return verdict(match=True, confidence=0.9, updates=[])

    monkeypatch.setattr(sweeper, "_classify", always_match)

    # no required slots → a confident match fires immediately
    async with db_sessionmaker() as s:
        wf = await s.get(Workflow, wf_id)
        wf.required_slots = []
        ts = TriggerState(
            workflow_id=wf_id, chat_id=chat.id, thread_key=0, last_tg_message_id=5,
            cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        s.add(ts)
        s.add(_msg(chat.id, 10, "let's schedule dinner", minutes_ago=5))  # arrives during cooldown
        await s.commit()

    await sweeper.sweep()  # in cooldown → must not fire, must not advance
    async with db_sessionmaker() as s:
        assert (await s.execute(select(PendingFire))).scalar_one_or_none() is None
        ts = (await s.execute(select(TriggerState))).scalar_one()
        assert ts.last_tg_message_id == 5, "cooling workflow consumed a during-cooldown message"

    # cooldown lifts → the same message is still pending → fires
    async with db_sessionmaker() as s:
        ts = (await s.execute(select(TriggerState))).scalar_one()
        ts.cooldown_until = datetime.now(timezone.utc) - timedelta(seconds=1)
        await s.commit()
    await sweeper.sweep()
    async with db_sessionmaker() as s:
        assert (await s.execute(select(PendingFire))).scalar_one() is not None


async def test_sweeper_ignores_self_messages_and_hot_windows(db_sessionmaker, monkeypatch):
    _, chat, _ = await _intent_setup(db_sessionmaker)
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    called = False

    async def fake_classify(*a):
        nonlocal called
        called = True
        return verdict()

    monkeypatch.setattr(sweeper, "_classify", fake_classify)

    async with db_sessionmaker() as s:
        m = _msg(chat.id, 1, "let's schedule dinner", minutes_ago=0)  # too fresh (no lull)
        s.add(m)
        await s.commit()
    assert await sweeper.sweep() == 0
    assert not called


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
                       cooldown_seconds=3600, threshold=0.1, examples_status="ready")
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
