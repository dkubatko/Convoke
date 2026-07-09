"""Regression tests for the audited intent-pipeline fixes: in-flight key
leaks, the executor kill switch, import-time sweep guards, recheck thread
isolation and failure pacing, chat-wide cooldown anchoring, effective-slot
fire payloads, apply-crash containment, unassign cleanup, and API validation.

Reuses the replay harness (ScriptedModel, FakeEmbedder, scripted verdicts)
from tests/test_intent_replay.py.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.intent.executor import FireExecutor, handle_confirm_callback
from app.intent.schemas import RecheckVerdict
from app.models import (
    AgentRun,
    ImportJob,
    IntentCursor,
    IntentEpisode,
    PendingFire,
    Workflow,
    WorkflowAssignment,
)
from app.telegram.limiter import SendLimiter

from tests.test_agent import AgentFakeBot
from tests.test_intent_replay import (
    NOW,
    ON_TOPIC,
    ScriptedModel,
    _add,
    _episodes,
    _fires,
    _msg,
    _satisfy,
    _setup,
    _sweeper,
    attributed,
    detect,
    upd,
)


# ---------- fix 1: a failed plan must not strand _in_flight keys ----------

async def test_planning_failure_does_not_strand_in_flight_keys(db_sessionmaker):
    """If planning a later key raises after an earlier key was registered, the
    registered key must be released — a leaked key means that (workflow, chat,
    thread) is never evaluated again until restart."""
    script = ScriptedModel()
    _, chat, wf_id = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)
    async with db_sessionmaker() as s:
        s.add(IntentCursor(workflow_id=wf_id, chat_id=chat.id, thread_key=55,
                           last_tg_message_id=0))
        await s.commit()
    await _add(
        db_sessionmaker,
        _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)),
        _msg(chat.id, 2, ON_TOPIC, at=NOW - timedelta(minutes=2), thread=55),
    )

    original = sweeper._plan_key
    produced = []

    async def plan_then_boom(*args, **kwargs):
        job = await original(*args, **kwargs)
        if job is not None:
            if produced:  # a key is already registered — fail the later one
                raise RuntimeError("planning boom")
            produced.append(job)
        return job

    sweeper._plan_key = plan_then_boom
    assert await sweeper.sweep(now=NOW) == 0  # the whole chat's plan failed
    assert sweeper._in_flight == set()  # nothing stranded

    sweeper._plan_key = original
    script.push(detect(relation="unrelated"), detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW) == 2  # both keys evaluate again


# ---------- fix 2: executor kill switch (disabled / unassigned) ----------

async def test_disabled_workflow_cancels_pending_fire(db_sessionmaker):
    script = ScriptedModel()
    bot, chat, wf_id = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    assert len(await _fires(db_sessionmaker)) == 1

    async with db_sessionmaker() as s:
        (await s.get(Workflow, wf_id)).enabled = False
        await s.commit()

    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, AgentFakeBot())
    await executor._tick()

    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "cancelled"
        assert (await s.execute(select(AgentRun))).scalar_one_or_none() is None
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "candidate" and ep.fired_at is None  # reverted


async def test_unassigned_workflow_cancels_pending_fire(db_sessionmaker):
    script = ScriptedModel()
    bot, chat, wf_id = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)

    async with db_sessionmaker() as s:
        await s.execute(delete(WorkflowAssignment).where(
            WorkflowAssignment.workflow_id == wf_id))
        await s.commit()

    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, AgentFakeBot())
    await executor._tick()

    async with db_sessionmaker() as s:
        assert (await s.execute(select(PendingFire))).scalar_one().status == "cancelled"
        assert (await s.execute(select(AgentRun))).scalar_one_or_none() is None


async def test_confirm_click_after_disable_cancels(db_sessionmaker):
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
        (await s.get(Workflow, wf_id)).enabled = False
        await s.commit()
    async with db_sessionmaker() as s:
        nonce = (await s.execute(select(PendingFire))).scalar_one().confirm_nonce
        await handle_confirm_callback(s, fake, f"wf:y:{nonce}", "Alice", "cb")
        await s.commit()

    await executor._tick()
    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "cancelled"
        assert (await s.execute(select(AgentRun))).scalar_one_or_none() is None
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "candidate"  # can re-converge if re-enabled


# ---------- fix 3: sweep skips chats with a running import ----------

async def test_sweep_skips_chat_with_active_import(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    async with db_sessionmaker() as s:
        s.add(ImportJob(chat_id=chat.id, filename="export.json", status="ingesting"))
        await s.commit()

    # No verdict scripted: an LLM call during the import would fail loudly.
    assert await sweeper.sweep(now=NOW) == 0

    async with db_sessionmaker() as s:
        (await s.execute(select(ImportJob))).scalar_one().status = "done"
        await s.commit()
    script.push(detect(relation="unrelated"))
    assert await sweeper.sweep(now=NOW) == 1  # evaluated once the import settled


# ---------- fixes 4 + 5: recheck thread isolation and failure pacing ----------

async def _park_second_topic(db_sessionmaker, script, sweeper, chat):
    """Fire once, then park a second topic 10 min into a 60-min cooldown."""
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    await _satisfy(db_sessionmaker, "posted the plan")
    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " friday!",
                                     at=NOW + timedelta(minutes=10)))
    script.push(attributed(relation="new_instance", summary="friday dinner"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))
    eps = await _episodes(db_sessionmaker)
    assert eps[1].status == "converged"  # parked


async def test_recheck_ignores_other_threads(db_sessionmaker):
    """Chatter in ANOTHER thread while an episode is parked must not trigger a
    recheck — only the episode's own thread is evidence about its topic."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script)
    await _park_second_topic(db_sessionmaker, script, sweeper, chat)

    # Messages arrive in thread 55 only (no cursor there: seeded, not work).
    await _add(db_sessionmaker, _msg(chat.id, 3, "unrelated side talk",
                                     at=NOW + timedelta(minutes=30), thread=55))

    # Cooldown lifts: the parked episode's thread was silent → direct fire,
    # no recheck verdict scripted — an LLM call here would fail the test.
    assert await sweeper.sweep(now=NOW + timedelta(minutes=70)) == 1
    assert len(await _fires(db_sessionmaker)) == 2
    assert not script.queue


async def test_recheck_failure_marks_error_and_throttles(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script, intent_min_llm_interval_seconds=120)
    await _park_second_topic(db_sessionmaker, script, sweeper, chat)
    await _add(db_sessionmaker, _msg(chat.id, 3, "hmm, still on?",
                                     at=NOW + timedelta(minutes=30)))

    script.push(None)  # model down during the recheck
    assert await sweeper.sweep(now=NOW + timedelta(minutes=70)) == 0
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_stage == "classifier_error"  # spinner not left live
        assert cursor.last_llm_at is not None  # retries are paced

    # 10s later: inside the min-LLM interval → throttled, no model call.
    assert await sweeper.sweep(now=NOW + timedelta(minutes=70, seconds=10)) == 0
    async with db_sessionmaker() as s:
        assert (await s.execute(select(IntentCursor))).scalar_one().last_stage == "throttled"

    # Past the interval the recheck runs and fires.
    script.push(RecheckVerdict(still_wanted=True, reason="still on"))
    assert await sweeper.sweep(now=NOW + timedelta(minutes=75)) == 1
    assert len(await _fires(db_sessionmaker)) == 2


# ---------- fix 6: confirm timeout honors the runtime tunable ----------

async def test_confirm_timeout_honors_runtime_tunable(db_sessionmaker):
    from app.core.runtime_settings import set_override

    bot, chat, wf_id = await _setup(db_sessionmaker, confirm=True)
    real_now = datetime.now(timezone.utc)
    async with db_sessionmaker() as s:
        s.add(PendingFire(workflow_id=wf_id, chat_id=chat.id, slots={},
                          status="confirm_wait", confirm_nonce="n",
                          created_at=real_now - timedelta(minutes=30)))
        await s.commit()

    executor = FireExecutor(db_sessionmaker, SendLimiter())
    executor._bots.put(bot.id, AgentFakeBot())
    await executor._tick()
    async with db_sessionmaker() as s:
        # under the 60-min env default a 30-min wait is not stale
        assert (await s.execute(select(PendingFire))).scalar_one().status == "confirm_wait"
        await set_override(s, "confirm_timeout_minutes", 5)
        await s.commit()

    await executor._tick()
    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "cancelled" and fire.error == "confirmation timed out"


# ---------- fix 7: cooldown anchors chat-wide, open or closed ----------

async def test_cooldown_spans_threads(db_sessionmaker):
    """The UI promises workflow-level 'at most once per N' — a fire in the
    main thread must cool down a convergence in another thread."""
    script = ScriptedModel()
    _, chat, wf_id = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script)
    async with db_sessionmaker() as s:
        s.add(IntentCursor(workflow_id=wf_id, chat_id=chat.id, thread_key=55,
                           last_tg_message_id=0))
        await s.commit()

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    assert len(await _fires(db_sessionmaker)) == 1

    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " in the thread",
                                     at=NOW + timedelta(minutes=10), thread=55))
    script.push(detect(summary="thread dinner"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))
    assert len(await _fires(db_sessionmaker)) == 1  # cooled down, not fired
    eps = await _episodes(db_sessionmaker)
    assert eps[1].thread_key == 55 and eps[1].status == "converged"  # parked


async def test_cooldown_survives_episode_close(db_sessionmaker):
    """Closing the satisfied episode must not lift the cooldown early — the
    anchor is max(fired_at) over ALL the workflow's episodes in the chat."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker, cooldown=3600)
    sweeper = _sweeper(db_sessionmaker, script)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)
    await _satisfy(db_sessionmaker, "posted the plan")
    async with db_sessionmaker() as s:
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        ep.status = "closed"
        ep.close_reason = "done"
        ep.closed_at = NOW + timedelta(minutes=5)
        await s.commit()

    await _add(db_sessionmaker, _msg(chat.id, 2, ON_TOPIC + " friday!",
                                     at=NOW + timedelta(minutes=10)))
    script.push(detect(summary="friday dinner"))
    await sweeper.sweep(now=NOW + timedelta(minutes=11))
    assert len(await _fires(db_sessionmaker)) == 1  # still cooling down
    eps = await _episodes(db_sessionmaker)
    assert eps[1].status == "converged"  # parked, not fired


# ---------- fix 9: fire payload comes from the effective slot dict ----------

async def test_fire_payload_excludes_decayed_slots(db_sessionmaker):
    """A no-slot workflow that gathered a stray detail hours ago: by fire time
    the detail has decayed out of the effective dict the convergence decision
    used — the PendingFire payload must not resurrect it."""
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)  # no required slots
    sweeper = _sweeper(db_sessionmaker, script, intent_tracking_idle_hours=48)

    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC + " someday",
                                     at=NOW - timedelta(minutes=2)))
    script.push(detect(conf=0.6, summary="vague dinner", updates=[upd("note", "sometime", 0.9)]))
    await sweeper.sweep(now=NOW)
    assert not await _fires(db_sessionmaker)

    # 16h later (grace 6h + 10h → 0.9 × 0.85¹⁰ ≈ 0.18, below the floor): a
    # confident continuation fires — with the stale note decayed out.
    later = NOW + timedelta(hours=16)
    await _add(db_sessionmaker, _msg(chat.id, 2, "ok, tonight, let's do it",
                                     at=later - timedelta(minutes=2)))
    script.push(attributed(summary="dinner tonight"))
    await sweeper.sweep(now=later)
    fires = await _fires(db_sessionmaker)
    assert len(fires) == 1 and fires[0].slots == {}


# ---------- fix 10: an apply crash must not become a classifier loop ----------

async def test_apply_crash_keeps_llm_stamp_and_marks_error(db_sessionmaker):
    script = ScriptedModel()
    _, chat, _ = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script, intent_min_llm_interval_seconds=120)

    async def boom(*args, **kwargs):
        raise TypeError("naive/aware datetime mixup")

    sweeper._apply_detect = boom
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    assert await sweeper.sweep(now=NOW) == 0
    async with db_sessionmaker() as s:
        cursor = (await s.execute(select(IntentCursor))).scalar_one()
        assert cursor.last_stage == "classifier_error"
        assert cursor.last_llm_at is not None  # the stamp survived the rollback
        assert cursor.last_tg_message_id == 0  # window not consumed

    # Next sweep inside the interval: throttled — the crash can't re-spend a
    # full classifier call every 5s sweep.
    assert await sweeper.sweep(now=NOW + timedelta(seconds=10)) == 0
    async with db_sessionmaker() as s:
        assert (await s.execute(select(IntentCursor))).scalar_one().last_stage == "throttled"


# ---------- fix 13: API validation ----------

async def test_workflow_api_field_constraints(client):
    valid = {"name": "  daily digest  ", "type": "scheduled",
             "action_prompt": "post a summary", "cron": "0 9 * * *"}
    r = await client.post("/api/workflows", json=valid)
    assert r.status_code == 201
    assert r.json()["name"] == "daily digest"  # stripped

    for bad in (
        {**valid, "name": ""},
        {**valid, "name": "   "},
        {**valid, "name": "x" * 201},
        {**valid, "cooldown_seconds": -1},
        {**valid, "dedup_window_hours": 0},
    ):
        assert (await client.post("/api/workflows", json=bad)).status_code == 422


# ---------- fix 14: unassign closes episodes and cancels live fires ----------

async def test_unassign_closes_episodes_and_cancels_fires(db_sessionmaker):
    from app.api.workflows import _set_assignments

    script = ScriptedModel()
    _, chat, wf_id = await _setup(db_sessionmaker)
    sweeper = _sweeper(db_sessionmaker, script)
    await _add(db_sessionmaker, _msg(chat.id, 1, ON_TOPIC, at=NOW - timedelta(minutes=2)))
    script.push(detect(summary="dinner"))
    await sweeper.sweep(now=NOW)  # fired episode + pending fire

    async with db_sessionmaker() as s:
        wf = await s.get(Workflow, wf_id)
        await _set_assignments(s, wf, [])
        await s.commit()

    async with db_sessionmaker() as s:
        fire = (await s.execute(select(PendingFire))).scalar_one()
        assert fire.status == "cancelled" and fire.error == "workflow unassigned"
        ep = (await s.execute(select(IntentEpisode))).scalar_one()
        assert ep.status == "closed" and ep.close_reason == "superseded"
