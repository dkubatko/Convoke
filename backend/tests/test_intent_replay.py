"""Conversation replays for the episode pipeline — one per failure case the
redesign exists to fix (A–J in the design plan).

Mechanics: scripted Message rows with controlled timestamps, sweeps at chosen
clock points, a FakeEmbedder (identical text → prefilter pass; unrelated text
under a strict threshold → fail), and a ScriptedModel standing in for the
cheap LLM. The ScriptedModel also asserts the MODE of each call (detect vs
attribution vs recheck) via isinstance — sticky-tracking bugs fail loudly.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.core.crypto import encrypt
from app.intent.episodes import finish_run_episode
from app.intent.executor import FireExecutor, handle_confirm_callback
from app.intent.pipeline import IntentSweeper
from app.intent.schemas import AttributionVerdict, DetectVerdict, RecheckVerdict, SlotUpdate
from app.memory.embeddings import FakeEmbedder
from app.models import (
    AgentRun,
    Bot,
    Chat,
    IntentCursor,
    IntentEpisode,
    Message,
    MessageAttachment,
    PendingFire,
    Workflow,
    WorkflowAssignment,
    WorkflowExample,
)
from app.telegram.limiter import SendLimiter

from tests.test_agent import AgentFakeBot

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
SLOT_TIME = [{"name": "time", "description": "the agreed time"}]
ON_TOPIC = "let's schedule dinner"  # == the workflow example → prefilter score 1.0


class ScriptedModel:
    """Stands in for IntentSweeper._model_call. Pops queued verdicts in order,
    asserting each call's expected output type; records every prompt."""

    def __init__(self):
        self.queue: list = []
        self.prompts: list[str] = []

    def push(self, *verdicts):
        self.queue.extend(verdicts)

    async def __call__(self, session, prompt, output_type):
        self.prompts.append(prompt)
        assert self.queue, f"unexpected classifier call:\n{prompt[:200]}"
        verdict = self.queue.pop(0)
        if verdict is None:
            return None  # scripted model failure
        assert isinstance(verdict, output_type), (
            f"expected {output_type.__name__}, scripted {type(verdict).__name__} — "
            f"the pipeline called the wrong mode.\n{prompt[:200]}"
        )
        return verdict


def detect(relation="clear", conf=0.9, summary="dinner tonight", updates=()):
    return DetectVerdict(
        relation=relation, confidence=conf, topic_summary=summary, slot_updates=list(updates)
    )


def attributed(relation="continues_episode", ref=1, conf=0.9, summary="", updates=(), concluded=False):
    return AttributionVerdict(
        relation=relation,
        episode_ref=ref,
        confidence=conf,
        topic_summary=summary,
        slot_updates=list(updates),
        topic_concluded=concluded,
    )


def upd(name, value, conf=0.9):
    return SlotUpdate(name=name, value=value, confidence=conf)


def _msg(chat_id, tg_id, text, at, source="live", thread=None, reply_to=None):
    return Message(
        chat_id=chat_id, tg_message_id=tg_id, thread_id=thread, sender_id=5,
        sender_name="Alice", text=text, sent_at=at, source=source,
        reply_to_tg_message_id=reply_to,
    )


async def _setup(db_sessionmaker, *, required=(), threshold=0.5, confirm=False,
                 cooldown=0, seed_cursor=True):
    """Bot + authorized chat + one intent workflow with a single example
    utterance (ON_TOPIC). At threshold 0.5 the FakeEmbedder cleanly splits
    on-topic openers (≥0.8) from short follow-ups and off-topic chatter
    (≤0.35). The cursor row mimics assignment-time seeding."""
    async with db_sessionmaker() as s:
        bot = Bot(tg_bot_id=1, username="b", name="b", token_encrypted=encrypt("1:x"),
                  can_read_all_group_messages=True)
        s.add(bot)
        await s.flush()
        chat = Chat(bot_id=bot.id, tg_chat_id=-100, type="supergroup", title="G",
                    status="authorized")
        s.add(chat)
        await s.flush()
        wf = Workflow(
            name="event", type="intent", action_prompt="Create the event",
            trigger_prompt="the group is scheduling a meal together",
            required_slots=list(required), confirm=confirm, cooldown_seconds=cooldown,
            threshold=threshold, examples_status="ready",
        )
        s.add(wf)
        await s.flush()
        s.add(WorkflowAssignment(workflow_id=wf.id, chat_id=chat.id))
        vec = (await FakeEmbedder().embed_passages([ON_TOPIC]))[0]
        s.add(WorkflowExample(workflow_id=wf.id, kind="positive", text=ON_TOPIC, embedding=vec))
        if seed_cursor:
            s.add(IntentCursor(workflow_id=wf.id, chat_id=chat.id, thread_key=0,
                               last_tg_message_id=0))
        await s.commit()
        return bot, chat, wf.id


def _sweeper(db_sessionmaker, script: ScriptedModel, **overrides):
    sweeper = IntentSweeper(db_sessionmaker, FakeEmbedder())
    sweeper._base = sweeper._base.model_copy(
        update={"intent_min_llm_interval_seconds": 0, **overrides}
    )
    sweeper._model_call = script
    return sweeper


async def _add(db_sessionmaker, *messages):
    async with db_sessionmaker() as s:
        for m in messages:
            s.add(m)
        await s.commit()


async def _fires(db_sessionmaker):
    async with db_sessionmaker() as s:
        return (await s.execute(select(PendingFire).order_by(PendingFire.id))).scalars().all()


async def _episodes(db_sessionmaker):
    async with db_sessionmaker() as s:
        return (await s.execute(select(IntentEpisode).order_by(IntentEpisode.id))).scalars().all()


async def _satisfy(db_sessionmaker, summary):
    """Shortcut for 'the agent ran and did this' without the executor."""
    async with db_sessionmaker() as s:
        ep = (
            await s.execute(select(IntentEpisode).where(IntentEpisode.status == "fired"))
        ).scalar_one()
        ep.status = "satisfied"
        ep.execution_summary = summary
        await s.commit()
        return ep.id


# ---------- A: continuation blindness ----------

async def test_a_short_followup_reaches_classifier_despite_prefilter(db_sessionmaker):
    """'do we want to meet today?' opens a candidate on an 'ambiguous' verdict;
    the follow-up 'Yes, I can do that at 7' would FAIL the prefilter alone but
    is classified anyway (sticky) and completes the fire."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " today?", at=NOW - timedelta(minutes=2)))
    script.push(detect(relation="ambiguous", conf=0.6, summary="maybe dinner today"))
    assert await sweeper.sweep(now=NOW) == 1
    eps = await _episodes(db_sessionmaker)
    assert len(eps) == 1 and eps[0].status == "candidate"
    assert not await _fires(db_sessionmaker)

    # An AttributionVerdict is scripted: if the prefilter (score ≈ 0 for this
    # text at threshold 0.95) blocked the call, the sweep would evaluate 0.
    await _add(db_sessionmaker, _msg(chat.id, 2, "Yes, I can do that at 7",
                                     at=NOW + timedelta(minutes=3)))
    script.push(attributed(updates=[upd("time", "7pm")], summary="dinner today at 7"))
    assert await sweeper.sweep(now=NOW + timedelta(minutes=4)) == 1

    fires = await _fires(db_sessionmaker)
    assert len(fires) == 1 and fires[0].slots["time"]["value"] == "7pm"
    eps = await _episodes(db_sessionmaker)
    assert eps[0].status == "fired"
    assert "possibly starting" in script.prompts[1]  # candidate was rendered


# ---------- B: same-topic double fire ----------

async def test_b_handled_topic_suppresses_followup_but_not_new_instance(db_sessionmaker):
    """A handled topic is prefilter-gated dedup MEMORY: weak follow-ups are
    suppressed for free (no LLM), strong ones get attributed with the handled
    topic — and the skipped lines — in context."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)  # no-slot: fires on any confident match
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " at 7", at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner at 7"))
    await sweeper.sweep(now=NOW)
    assert len(await _fires(db_sessionmaker)) == 1
    await _satisfy(db_sessionmaker, "Created calendar event 'dinner' today at 7pm")

    # Off-topic interjection AND a weak same-topic follow-up: neither
    # resembles the trigger, so the embedding gate suppresses both for FREE —
    # no verdicts are scripted; an LLM call here would fail the test.
    await _add(
        db_sessionmaker,
        _msg(chat.id, 2, "I love my dog!", at=NOW + timedelta(minutes=2)),
        _msg(chat.id, 3, "Sure, I can do 8 or 9 too", at=NOW + timedelta(minutes=3)),
    )
    await sweeper.sweep(now=NOW + timedelta(minutes=4))
    assert len(await _fires(db_sessionmaker)) == 1  # no double fire, no LLM spent
    assert len(script.prompts) == 1
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_stage == "prefilter_skip" and cursor.last_tg_message_id == 3

    # A STRONG same-topic follow-up passes the gate → attribution runs, sees
    # what was already done AND the skipped weak lines as lead-up context.
    await _add(db_sessionmaker, _msg(chat.id, 4, "sure! " + ON_TOPIC + " at 8 or 9",
                                     at=NOW + timedelta(minutes=6)))
    script.push(attributed(ref=1, summary="dinner tonight, time flexible"))
    await sweeper.sweep(now=NOW + timedelta(minutes=7))
    assert len(await _fires(db_sessionmaker)) == 1  # suppressed knowingly
    assert "An automation already ran for this: Created calendar event" in script.prompts[-1]
    assert "Sure, I can do 8 or 9 too" in script.prompts[-1]  # resurfaced as context
    eps = await _episodes(db_sessionmaker)
    # A handled topic's identity is frozen: the verdict's summary must NOT
    # overwrite it, or one misattribution re-identifies the topic and makes
    # every later misattribution self-confirming.
    assert eps[0].summary == "dinner at 7"

    # But a genuinely NEW occurrence still fires.
    await _add(db_sessionmaker, _msg(chat.id, 5, ON_TOPIC + " next friday too",
                                     at=NOW + timedelta(minutes=10)))
    script.push(attributed(relation="new_instance", summary="second dinner next friday"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))
    assert len(await _fires(db_sessionmaker)) == 2


# ---------- C: interleaving at cap=1 ----------

async def test_c_cap_protects_invested_topics_only(db_sessionmaker):
    """Eviction under the cap keys on SUBSTANCE: candidates and slotless
    protection is earned by gathered details, not by any status label."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(relation="ambiguous", summary="maybe dinner"))
    await sweeper.sweep(now=NOW)

    # A concrete new instance evicts a mere candidate and opens protected
    # (slot below the fire bar so it keeps gathering without firing).
    await _add(db_sessionmaker, _msg(chat.id, 2, "actually, board games friday?",
                                     at=NOW + timedelta(minutes=2)))
    script.push(attributed(relation="new_instance", summary="board games friday",
                           updates=[upd("time", "friday-ish", 0.65)]))
    await sweeper.sweep(now=NOW + timedelta(minutes=3))
    eps = await _episodes(db_sessionmaker)
    assert [e.status for e in eps] == ["closed", "candidate"]
    assert eps[0].close_reason == "superseded"

    # An invested topic is protected: a further VAGUE new instance
    # is not tracked (documented cap=1 limitation).
    await _add(db_sessionmaker, _msg(chat.id, 3, "and a picnic sunday!",
                                     at=NOW + timedelta(minutes=5)))
    script.push(attributed(relation="new_instance", summary="picnic sunday"))
    await sweeper.sweep(now=NOW + timedelta(minutes=6))
    assert len(await _episodes(db_sessionmaker)) == 2  # nothing new opened
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_stage == "cap_full"


async def test_c2_vague_topic_never_squats_the_cap(db_sessionmaker):
    """The Washington case: a slotless topic (vague 'let's hike somewhere
    else') must never block the next concrete topic under the cap."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    # Vague but clearly on-intent → slotless candidate (would-be squatter).
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " somewhere, sometime",
                                     at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner, nothing concrete"))
    await sweeper.sweep(now=NOW)
    eps = await _episodes(db_sessionmaker)
    assert eps[0].status == "candidate" and eps[0].slots == {}

    # A concrete new instance evicts the slotless squatter and FIRES.
    await _add(db_sessionmaker, _msg(chat.id, 2, "new idea: dinner at 7 sharp",
                                     at=NOW + timedelta(minutes=2)))
    script.push(attributed(relation="new_instance", summary="dinner at 7",
                           updates=[upd("time", "7pm")]))
    await sweeper.sweep(now=NOW + timedelta(minutes=3))
    eps = await _episodes(db_sessionmaker)
    assert eps[0].status == "closed" and eps[0].close_reason == "superseded"
    assert eps[1].status == "fired"
    assert len(await _fires(db_sessionmaker)) == 1

    # And a vague new instance opens as a candidate (evictable, fast-expiring).
    await _satisfy(db_sessionmaker, "booked it")
    await _add(db_sessionmaker, _msg(chat.id, 3, ON_TOPIC + " more often, someday",
                                     at=NOW + timedelta(minutes=6)))
    script.push(attributed(relation="new_instance", summary="vague repeat idea"))
    await sweeper.sweep(now=NOW + timedelta(minutes=7))
    eps = await _episodes(db_sessionmaker)
    assert eps[-1].status == "candidate"


# ---------- D: rejected-window evidence survives ----------

async def test_d_candidate_slots_survive_cursor_advance(db_sessionmaker):
    """Evidence from an early low-confidence window (old design: discarded
    forever once the cursor advanced) persists on the candidate episode."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " at 7 maybe?", at=NOW - timedelta(minutes=2)))
    script.push(detect(relation="ambiguous", conf=0.5, summary="tentative dinner",
                       updates=[upd("time", "7pm", 0.8)]))
    await sweeper.sweep(now=NOW)
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 1  # window consumed…
    eps = await _episodes(db_sessionmaker)
    assert eps[0].slots["time"]["value"] == "7pm"  # …but the evidence persists

    await _add(db_sessionmaker, _msg(chat.id, 2, "yes! let's do it", at=NOW + timedelta(minutes=3)))
    script.push(attributed(summary="dinner at 7 confirmed"))
    await sweeper.sweep(now=NOW + timedelta(minutes=4))
    fires = await _fires(db_sessionmaker)
    assert len(fires) == 1 and fires[0].slots["time"]["value"] == "7pm"


# ---------- E: cooldown parks, rechecks, never drops ----------

async def test_e_convergence_during_cooldown_parks_then_fires(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    assert len(await _fires(db_sessionmaker)) == 1
    await _satisfy(db_sessionmaker, "posted the plan")

    # A distinct topic converges 10 min into the 60-min cooldown → parked.
    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " friday!", at=NOW + timedelta(minutes=10)))
    script.push(attributed(relation="new_instance", summary="friday dinner"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))
    assert len(await _fires(db_sessionmaker)) == 1
    eps = await _episodes(db_sessionmaker)
    assert eps[1].status == "converged"  # parked, NOT dropped

    # Cooldown lifts; the chat was silent since parking → fires directly.
    assert await sweeper.sweep(now=NOW + timedelta(minutes=70)) == 1
    assert len(await _fires(db_sessionmaker)) == 2
    assert not script.queue  # no LLM call was needed


async def test_e2_parked_episode_rechecked_when_chat_moved_on(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " friday!", at=NOW + timedelta(minutes=10)))
    script.push(attributed(relation="new_instance", summary="friday dinner"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))

    # The group resolves it themselves during the cooldown.
    await _add(db_sessionmaker, _msg(chat.id, 3, "nvm, I booked friday myself",
                                     at=NOW + timedelta(minutes=30)))
    script.push(attributed(ref=2, concluded=False, summary="friday dinner"))  # absorbed while parked
    await sweeper.sweep(now=NOW + timedelta(minutes=31))

    # Cooldown lifts; messages arrived since parking → recheck says stale.
    script.push(RecheckVerdict(still_wanted=False, reason="group booked it themselves"))
    await sweeper.sweep(now=NOW + timedelta(minutes=70))
    assert len(await _fires(db_sessionmaker)) == 1  # only the original fire
    eps = await _episodes(db_sessionmaker)
    assert eps[1].status == "closed" and eps[1].close_reason == "stale"
    assert "waited out a rate limit" in script.prompts[-1]


# ---------- F: post-fire replies to the agent ----------

async def test_f_bot_reply_in_transcript_and_thanks_suppressed(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " at 8", at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner at 8"))
    await sweeper.sweep(now=NOW)
    await _satisfy(db_sessionmaker, "Scheduled dinner for 8pm")

    # A plain thanks is suppressed for free at the gate (no scripted verdict).
    await _add(
        db_sessionmaker,
        _msg(chat.id, 2, "Event scheduled for 8pm ✅", at=NOW + timedelta(minutes=1), source="self"),
        _msg(chat.id, 3, "perfect, 8pm works, thanks!", at=NOW + timedelta(minutes=2)),
    )
    assert await sweeper.sweep(now=NOW + timedelta(minutes=3)) == 0
    assert len(await _fires(db_sessionmaker)) == 1

    # A strong follow-up reaches attribution — with the bot's own reply
    # rendered in the transcript as a [bot] line.
    await _add(db_sessionmaker, _msg(chat.id, 4, ON_TOPIC + " like this every week?",
                                     at=NOW + timedelta(minutes=5)))
    script.push(attributed(ref=1, summary="dinner at 8, recurring idea floated"))
    assert await sweeper.sweep(now=NOW + timedelta(minutes=6)) == 1

    assert len(await _fires(db_sessionmaker)) == 1  # still no re-fire
    prompt = script.prompts[-1]
    assert "[bot]" in prompt and "Event scheduled for 8pm" in prompt
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 4


# ---------- G: newly assigned workflow skips the backlog ----------

async def test_g_first_sight_seeds_cursor_past_history(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, seed_cursor=False)  # no assignment-time seed
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, *[
        _msg(chat.id, i, ON_TOPIC, at=NOW - timedelta(hours=2, minutes=i)) for i in range(1, 51)
    ])
    assert await sweeper.sweep(now=NOW) == 0  # backlog untouched, no LLM calls
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 50

    # …but a live message after assignment is evaluated, alone.
    await _add(db_sessionmaker, _msg(chat.id, 51, ON_TOPIC + " NOW", at=NOW + timedelta(minutes=1)))
    script.push(detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW + timedelta(minutes=2)) == 1
    # The window (after the marker) is just the tail message, not 50 rows;
    # the lines before it are the bounded lead-up context.
    window_part = script.prompts[0].split("--- new messages to classify ---")[1]
    assert window_part.count("Alice [") == 1 and ON_TOPIC + " NOW" in window_part


# ---------- H: graduated decay in the pipeline ----------

async def test_h_decayed_slot_blocks_convergence(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    # time gathered at NOW, but conversation doesn't converge (low verdict conf)
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " at 7?", at=NOW - timedelta(minutes=2)))
    script.push(detect(conf=0.6, summary="dinner, 7 floated", updates=[upd("time", "7pm", 0.9)]))
    await sweeper.sweep(now=NOW)
    assert not await _fires(db_sessionmaker)

    # 8h later (grace 6h + 2h → 0.9 × 0.85² ≈ 0.65 < 0.7): confident
    # continuation but the stale value can no longer satisfy the fire bar.
    later = NOW + timedelta(hours=8)
    await _add(db_sessionmaker, _msg(chat.id, 2, "ok let's lock it in", at=later - timedelta(minutes=2)))
    script.push(attributed(summary="locking in dinner"))
    await sweeper.sweep(now=later)
    assert not await _fires(db_sessionmaker)
    eps = await _episodes(db_sessionmaker)
    assert eps[0].status == "candidate"

    # The group re-states the time → fresh timestamp → fires.
    await _add(db_sessionmaker, _msg(chat.id, 3, "7pm it is", at=later + timedelta(minutes=2)))
    script.push(attributed(summary="dinner at 7", updates=[upd("time", "7pm")]))
    await sweeper.sweep(now=later + timedelta(minutes=3))
    assert len(await _fires(db_sessionmaker)) == 1


async def test_h2_stale_topic_expires_after_idle_limit(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, required=SLOT_TIME)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)

    # 20h idle (past any leash): the planning pass closes the episode;
    # a DetectVerdict is scripted — attribution mode here would fail loudly.
    later = NOW + timedelta(hours=20)
    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + "?", at=later - timedelta(minutes=2)))
    script.push(detect(relation="unrelated"))
    await sweeper.sweep(now=later)
    eps = await _episodes(db_sessionmaker)
    assert eps[0].status == "closed" and eps[0].close_reason == "expired"


# ---------- replies carry their context across long pauses ----------

async def test_reply_pulls_quoted_original_into_the_prompt(db_sessionmaker):
    """A Telegram reply to a message far outside the transcript window renders
    the quoted original inline — 'sure, that works' replying to yesterday's
    proposal isn't judged blind."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, "wanna hike Saturday at 10?", at=NOW - timedelta(hours=3)))
        for i in range(2, 12):  # push the proposal far beyond the 8-message context
            s.add(_msg(chat.id, i, f"filler chatter {i}", at=NOW - timedelta(hours=2)))
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        cursor.last_tg_message_id = 11  # everything above is already consumed
        await s.commit()

    await _add(db_sessionmaker, _msg(chat.id, 12, ON_TOPIC + " — yes down for that!",
                                     at=NOW - timedelta(minutes=2), reply_to=1))
    script.push(detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW) == 1
    prompt = script.prompts[0]
    assert 'replies to [#1] [2026-07-02 09:00] Alice: "wanna hike Saturday at 10?"' in prompt
    assert "wanna hike Saturday" not in prompt.split("↳")[0]  # truly out of context


async def test_weak_reply_inherits_target_topicality_at_the_gate(db_sessionmaker):
    """'cool!' alone fails the embedding gate; 'cool!' REPLYING to an on-topic
    proposal is scored as target+reply combined and passes. The plain 'cool!'
    control is asserted first."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, ON_TOPIC + " on Saturday!", at=NOW - timedelta(hours=1)))
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        cursor.last_tg_message_id = 1  # the proposal was already evaluated
        await s.commit()

    # Control: a bare "cool!" (no reply link) dies at the gate — no LLM call.
    await _add(db_sessionmaker, _msg(chat.id, 2, "cool!", at=NOW - timedelta(minutes=5)))
    assert await sweeper.sweep(now=NOW - timedelta(minutes=4)) == 0
    async with db_sessionmaker() as s:
        assert (await s.execute(select(IntentCursor))).scalar_one().last_stage == "prefilter_skip"

    # The same weak text AS A REPLY to the proposal passes and is classified.
    await _add(db_sessionmaker, _msg(chat.id, 3, "cool!", at=NOW - timedelta(minutes=2), reply_to=1))
    script.push(detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW) == 1
    assert len(script.prompts) == 1
    # The target sits 2 messages back — VISIBLE in the numbered transcript —
    # so the reply gets a pure pointer, never a duplicated quote.
    assert "(replying to #1)" in script.prompts[0]
    assert "↳" not in script.prompts[0]  # pure pointer, no quoted expansion


# ---------- I: parallel dispatch ----------

async def test_i_classifier_calls_run_concurrently_with_cap(db_sessionmaker):
    class Gauge(ScriptedModel):
        active = 0
        peak = 0

        async def __call__(self, session, prompt, output_type):
            GaugeT = type(self)
            GaugeT.active += 1
            GaugeT.peak = max(GaugeT.peak, GaugeT.active)
            await asyncio.sleep(0.02)
            GaugeT.active -= 1
            return await super().__call__(session, prompt, output_type)

    script = Gauge()
    bot, chat, wf_id = await _setup(db_sessionmaker, threshold=0.1)
    async with db_sessionmaker() as s:
        for i in range(2, 5):  # three more chats on the same workflow
            c = Chat(bot_id=bot.id, tg_chat_id=-100 - i, type="supergroup", status="authorized")
            s.add(c)
            await s.flush()
            s.add(WorkflowAssignment(workflow_id=wf_id, chat_id=c.id))
            s.add(IntentCursor(workflow_id=wf_id, chat_id=c.id, thread_key=0))
            s.add(_msg(c.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
        s.add(_msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
        await s.commit()

    sweeper = _sweeper(db_sessionmaker, script, intent_classifier_concurrency=2)
    script.push(*[detect(relation="unrelated") for _ in range(4)])
    assert await sweeper.sweep(now=NOW) == 4
    assert script.peak == 2  # parallel, but capped


# ---------- J: the feedback loop end-to-end ----------

async def test_j_agent_run_completion_feeds_next_evaluation(db_sessionmaker):
    script = ScriptedModel()
    bot, chat, wf_id = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)

    # Executor turns the fire into an AgentRun, linking the episode…
    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, AgentFakeBot())
    await executor._tick()
    async with db_sessionmaker() as s:
        run = (await s.execute(select(AgentRun))).scalar_one()
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.agent_run_id == run.id and ep.status == "fired"
        # …and the run's completion writes back what was done.
        await finish_run_episode(s, run.id, "Created the calendar event for 7pm", NOW)
        await s.commit()
    async with db_sessionmaker() as s:
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "satisfied"
        assert ep.execution_summary == "Created the calendar event for 7pm"

    await _add(db_sessionmaker, _msg(chat.id, 2, "great! " + ON_TOPIC + " again for 7",
                                     at=NOW + timedelta(minutes=5)))
    script.push(attributed(ref=1))
    await sweeper.sweep(now=NOW + timedelta(minutes=6))
    assert "Created the calendar event for 7pm" in script.prompts[-1]
    assert len(await _fires(db_sessionmaker)) == 1


async def test_j2_cancelled_confirmation_reverts_episode(db_sessionmaker):
    script = ScriptedModel()
    bot, chat, wf_id = await _setup(db_sessionmaker, confirm=True)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)

    fake = AgentFakeBot()
    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, fake)
    await executor._tick()  # posts the ✅/❌ prompt
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(PendingFire))).scalar_one().confirm_nonce
        await handle_confirm_callback(s, fake, f"wf:n:{nonce}", "Bob", "cb")
        await s.commit()
    async with db_sessionmaker() as s:
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "candidate" and ep.fired_at is None  # can re-converge


# ---------- classifier failure semantics (carried over) ----------

async def test_classifier_error_keeps_cursor_and_throttle_budget(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script, intent_min_llm_interval_seconds=120)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(None)  # model down
    assert await sweeper.sweep(now=NOW) == 0
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 0  # window NOT consumed
        assert cursor.last_llm_at is None  # throttle budget NOT spent
        assert cursor.last_stage == "classifier_error"

    # Next sweep retries immediately despite the long throttle interval.
    script.push(detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW + timedelta(seconds=10)) == 1
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_tg_message_id == 1


# ---------- media: descriptions drive intent; pending media holds the window ----------

def _media_msg(chat_id, tg_id, at, status="described", description=None, kind="photo"):
    m = _msg(chat_id, tg_id, "", at)
    m.attachment = MessageAttachment(
        chat_id=chat_id, tg_message_id=tg_id, kind=kind, file_id="f",
        file_unique_id=f"u{tg_id}", status=status, description=description,
    )
    return m


async def test_media_described_photo_fires_workflow(db_sessionmaker):
    """A photo whose description reads like the trigger passes the embedding
    prefilter and the classifier sees the annotated line — the movie-tickets
    case: intent from an image, no text typed."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(
        db_sessionmaker,
        _media_msg(chat.id, 1, at=NOW - timedelta(minutes=2),
                   description="two movie tickets for Dune — let's schedule dinner before, 7pm Friday"),
    )
    script.push(detect(summary="movie + dinner Friday"))
    assert await sweeper.sweep(now=NOW) == 1
    assert len(await _fires(db_sessionmaker)) == 1
    assert "[photo: two movie tickets" in script.prompts[0]  # annotation reached the classifier


async def test_media_pending_description_holds_window_within_grace(db_sessionmaker):
    """While a photo is still being described (and young), the window stays
    open — no LLM call, cursor not advanced. Once described, it evaluates on
    the description."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker,
               _media_msg(chat.id, 1, at=NOW - timedelta(seconds=60), status="pending"))
    # Inside the 120s grace: held — an LLM call here would fail (nothing scripted).
    assert await sweeper.sweep(now=NOW) == 0
    async with db_sessionmaker() as s:
        assert (await s.execute(select(IntentCursor))).scalar_one().last_tg_message_id == 0

    # Description lands (the media loop's job) → next sweep evaluates it.
    async with db_sessionmaker() as s:
        att = (await s.execute(select(MessageAttachment))).scalar_one()
        att.status = "described"
        att.description = "let's schedule dinner — restaurant menu photo"
        await s.commit()
    script.push(detect(summary="dinner"))
    assert await sweeper.sweep(now=NOW + timedelta(seconds=30)) == 1
    assert "restaurant menu photo" in script.prompts[0]


async def test_media_grace_expiry_releases_window(db_sessionmaker):
    """A description that never lands can't stall intent forever: past the
    grace the window is released into the normal pipeline. The bare pending
    placeholder scores nothing against the trigger, so the prefilter
    suppresses it for FREE (no verdict scripted — an LLM call would fail) and
    the cursor moves on."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script, intent_media_grace_seconds=120)

    await _add(db_sessionmaker,
               _media_msg(chat.id, 1, at=NOW - timedelta(seconds=60), status="pending"))
    assert await sweeper.sweep(now=NOW) == 0  # held: young + pending
    async with db_sessionmaker() as s:
        assert (await s.execute(select(IntentCursor))).scalar_one().last_tg_message_id == 0

    assert await sweeper.sweep(now=NOW + timedelta(seconds=300)) == 0  # grace expired
    async with db_sessionmaker() as s:
        # Window consumed (prefilter skip) — the sweeper is not stuck on it.
        assert (await s.execute(select(IntentCursor))).scalar_one().last_tg_message_id == 1
    assert script.prompts == []  # suppressed without spending the model


async def test_media_caption_component_survives_description_dilution(db_sessionmaker):
    """The measured live failure: a strong caption on a photo whose verbose
    description would drag the combined embedding under the threshold. The
    gate scores components separately — the caption alone passes."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)

    noise = ("An iPhone screenshot of Google search results showing a card with "
             "ratings runtime year and various interface elements " * 3)
    msg = _media_msg(chat.id, 1, at=NOW - timedelta(minutes=2), description=noise)
    msg.text = ON_TOPIC  # strong caption, diluted when concatenated
    await _add(db_sessionmaker, msg)

    # Sanity: the combined body alone would fail the gate at this threshold.
    from app.media.render import message_body
    emb = FakeEmbedder()
    combined = (await emb.embed_passages([message_body(msg)]))[0]
    example = (await emb.embed_passages([ON_TOPIC]))[0]
    from app.intent.examples import dot
    assert dot(combined, example) < 0.5  # pre-fix behavior: prefilter_skip

    script.push(detect(summary="movie night"))
    assert await sweeper.sweep(now=NOW) == 1  # component-max let the caption through
    assert len(await _fires(db_sessionmaker)) == 1


# ---------- direct bot invocations: context-only, never a trigger ----------

async def test_direct_invocation_is_context_only_never_fires(db_sessionmaker):
    """An @mention / reply-to-bot message already got its own immediate agent
    run at ingest. The workflow sweeper must not ALSO fire on it: like the
    bot's own sends, it stays visible as context but never opens or advances a
    firing window itself."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)  # no-slot: fires on any confident match
    sweeper = _sweeper(db_sessionmaker, script)

    # A direct invocation whose text WOULD fire if evaluated as work.
    async with db_sessionmaker() as s:
        s.add(_msg(chat.id, 1, ON_TOPIC + " DIRECTMARK", at=NOW - timedelta(minutes=2)))
        s.add(AgentRun(chat_id=chat.id, trigger="mention", trigger_tg_message_id=1,
                       request_text="x"))
        await s.commit()

    # No verdicts scripted: if the sweeper classified the direct message, the
    # ScriptedModel would raise "unexpected classifier call".
    assert await sweeper.sweep(now=NOW) == 0
    assert not await _fires(db_sessionmaker)
    assert not await _episodes(db_sessionmaker)

    # A normal (non-direct) on-topic message DOES open a window — and the
    # earlier direct-invocation line is present in its classifier context.
    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " at 7",
                                     at=NOW + timedelta(minutes=3)))
    script.push(detect(summary="dinner at 7"))
    assert await sweeper.sweep(now=NOW + timedelta(minutes=4)) == 1

    assert len(await _fires(db_sessionmaker)) == 1
    assert "DIRECTMARK" in script.prompts[0]  # the direct line rendered as context
