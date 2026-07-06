"""Example-utterance generation + prefilter threshold calibration.

The prefilter matches chat windows against synthetic example utterances, not
the trigger prompt (prompts are meta-language; real messages aren't, and are
often in other languages). The strong model generates positives, plus hard
negatives kept for display, at save time.

Calibration is recall-first and model-agnostic: the threshold is a low
percentile of positive cross-similarity — the "softest positive we must not
lose" — so it self-scales to the model's similarity range. Real on-topic
traffic scores against the best positive, flooring above that softest pair, so
anchoring there targets ~100% recall. The permissiveness knob (1–5) slides the
anchor from strict (near the positive floor) to permissive (below it); the
classifier supplies precision either way.
"""

import logging
from datetime import datetime, timezone

from pydantic_ai import Agent
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, build_model, get_provider
from app.core.runtime_settings import effective_settings
from app.intent.schemas import GeneratedExamples
from app.memory.embeddings import (
    GLOBAL_THRESHOLD_CEIL,
    GLOBAL_THRESHOLD_FLOOR,
    Embedder,
)
from app.models import Workflow, WorkflowExample

log = logging.getLogger("convoke.intent.examples")

DEFAULT_PERMISSIVENESS = 4
# Permissiveness (1 strictest … 5 most permissive) → the percentile of positive
# cross-similarity used as the threshold anchor, plus how far BELOW the softest
# positive to push (as a fraction of the positive spread, so it stays
# scale-invariant across model families). Lower anchor / larger sub-floor =
# lower threshold = more recall and more classifier noise.
_PERMISSIVENESS_PCT: dict[int, float] = {1: 0.30, 2: 0.18, 3: 0.10, 4: 0.04, 5: 0.0}
_PERMISSIVENESS_SUBFLOOR: dict[int, float] = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.15, 5: 0.60}

GENERATION_PROMPT = """\
A Telegram group-chat bot watches conversations for this intent:

"{trigger_prompt}"

Data to extract when the intent occurs (slots): {slots}

Generate example chat messages for training a semantic prefilter:
- positives: about {n_pos} realistic short messages members might send while \
this intent is being expressed or converging. Vary phrasing, formality and \
language (include a few in Spanish/Russian/German if plausible for a generic group).
- negatives: about {n_neg} near-misses — same topic area but NOT expressing the intent.

Members also express intent through PHOTOS and VOICE NOTES, which the bot sees \
as text in exactly these shapes:
  [photo: <subject-first description, any visible text quoted>] optional caption
  [voice 0:12: "<transcript>"]
- media_positives: about {n_media_pos} such messages where the MEDIA carries the \
intent — e.g. a photo of tickets, a screenshot of a search result, a voice note \
proposing it. Descriptions lead with the subject.
- media_negatives: about {n_media_neg} media near-misses in the same shapes — \
same topic area but NOT expressing the intent (these calibrate precision).
"""


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _percentile(sorted_values: list[float], q: float) -> float:
    idx = min(len(sorted_values) - 1, max(0, round(q * (len(sorted_values) - 1))))
    return sorted_values[idx]


def calibrate_threshold(
    positive_vecs: list[list[float]],
    negative_vecs: list[list[float]] | None = None,
    floor: float = GLOBAL_THRESHOLD_FLOOR,
    ceil: float = GLOBAL_THRESHOLD_CEIL,
    permissiveness: int = DEFAULT_PERMISSIVENESS,
) -> float:
    """Anchor on `pos_best` — per positive, its best match to another positive.
    A low percentile is the softest positive to keep; `permissiveness` slides it
    down (lower percentile + a sub-floor push scaled to the positive spread) for
    more recall. See the module docstring for why this targets ~100% recall and
    self-scales across models. Negatives are ignored — raising toward them would
    cost the recall this gate exists to protect."""
    del negative_vecs  # informational only; recall-first threshold is positives-driven
    level = min(5, max(1, int(permissiveness)))
    # Too few positives to characterize a distribution (e.g. the no-strong-model
    # fallback set): sit at the permissive floor rather than over-reject.
    if len(positive_vecs) < 2:
        return floor
    pos_best = sorted(
        max(dot(v, w) for j, w in enumerate(positive_vecs) if j != i)
        for i, v in enumerate(positive_vecs)
    )
    anchor = _percentile(pos_best, _PERMISSIVENESS_PCT[level])
    spread = max(0.0, _percentile(pos_best, 0.25) - pos_best[0])
    thr = anchor - _PERMISSIVENESS_SUBFLOOR[level] * spread
    return max(floor, min(ceil, thr))


async def generate_examples(
    sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder, workflow_id: int
) -> None:
    """Runs in the background after an intent workflow is created/edited.
    Falls back to embedding the trigger prompt itself when no strong model is
    configured yet."""
    async with sessionmaker() as session:
        wf = await session.get(Workflow, workflow_id)
        if wf is None or wf.type != "intent":
            return
        try:
            generated = await _generate(session, wf)
            status = "ready"
        except ProviderNotConfigured:
            generated = GeneratedExamples(positives=[wf.trigger_prompt or ""], negatives=[])
            status = "fallback"
        except Exception:  # noqa: BLE001 — degrade, don't break workflow saves
            log.exception("example generation failed for workflow %s", workflow_id)
            generated = GeneratedExamples(positives=[wf.trigger_prompt or ""], negatives=[])
            status = "fallback"

        # Media-shaped examples join both sides: media positives give media
        # windows same-register anchors to MATCH (raising their scores without
        # touching the bar), media negatives keep the negative calibration
        # anchor honest for that register (precision, not a lower bar).
        positives = generated.positives + generated.media_positives
        negatives = generated.negatives + generated.media_negatives
        pos_vecs = await embedder.embed_passages(positives) if positives else []
        neg_vecs = await embedder.embed_passages(negatives) if negatives else []

        await session.execute(
            delete(WorkflowExample).where(WorkflowExample.workflow_id == workflow_id)
        )
        for text, vec in zip(positives, pos_vecs):
            session.add(
                WorkflowExample(workflow_id=workflow_id, kind="positive", text=text, embedding=vec)
            )
        for text, vec in zip(negatives, neg_vecs):
            session.add(
                WorkflowExample(workflow_id=workflow_id, kind="negative", text=text, embedding=vec)
            )
        permissiveness = (await effective_settings(session)).intent_prefilter_permissiveness
        wf.threshold = calibrate_threshold(pos_vecs, neg_vecs, permissiveness=permissiveness)
        wf.examples_status = status
        wf.updated_at = datetime.now(timezone.utc)
        await session.commit()
        log.info(
            "workflow %s examples %s: %d pos (%d media) / %d neg (%d media), threshold %.3f",
            workflow_id, status, len(positives), len(generated.media_positives),
            len(negatives), len(generated.media_negatives), wf.threshold,
        )


async def _generate(session: AsyncSession, wf: Workflow) -> GeneratedExamples:
    provider = await get_provider(session, "agent")
    slots_desc = (
        "; ".join(f"{s['name']} ({s.get('description', '')})" for s in wf.required_slots or [])
        or "none"
    )
    n_pos = (await effective_settings(session)).intent_example_count
    n_neg = max(4, round(n_pos * 0.4))
    n_media_pos = max(4, n_pos // 3)
    n_media_neg = max(3, n_neg // 3)
    agent = Agent(build_model(provider), output_type=GeneratedExamples, retries=3)
    result = await agent.run(
        GENERATION_PROMPT.format(
            trigger_prompt=wf.trigger_prompt, slots=slots_desc, n_pos=n_pos, n_neg=n_neg,
            n_media_pos=n_media_pos, n_media_neg=n_media_neg,
        )
    )
    return result.output


async def regenerate_unready(
    sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder, older_than_s: int = 180
) -> int:
    """Recover intent workflows without a healthy example set:

    - stuck 'pending' — the generation task runs in the backend process and
      dies with it on restart, which would otherwise leave the detector
      uncalibrated forever;
    - 'fallback' — generated without a strong model (prefilter matches only
      the trigger prompt), retried once an agent model exists, so a workflow
      created before models were configured heals instead of staying degraded.

    A 'ready' set is never touched, and fallback only regenerates when a
    provider exists — good examples can only be replaced by a full
    regeneration, never downgraded."""
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_s)
    async with sessionmaker() as session:
        try:
            await get_provider(session, "agent")
            has_provider = True
        except ProviderNotConfigured:
            has_provider = False
        statuses = ("pending", "fallback") if has_provider else ("pending",)
        unready_ids = (
            (
                await session.execute(
                    select(Workflow.id).where(
                        Workflow.type == "intent",
                        Workflow.examples_status.in_(statuses),
                        Workflow.updated_at < cutoff,
                    )
                )
            )
            .scalars()
            .all()
        )
    for wf_id in unready_ids:
        log.info("workflow %s has no ready example set; regenerating", wf_id)
        await generate_examples(sessionmaker, embedder, wf_id)
    return len(unready_ids)


async def load_positive_vectors(session: AsyncSession, workflow_id: int) -> list[list[float]]:
    rows = (
        (
            await session.execute(
                select(WorkflowExample.embedding).where(
                    WorkflowExample.workflow_id == workflow_id,
                    WorkflowExample.kind == "positive",
                    WorkflowExample.embedding.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    return [list(v) for v in rows]


async def recalibrate_intent_thresholds(session: AsyncSession, permissiveness: int) -> int:
    """Re-derive every intent workflow's threshold from its ALREADY-STORED
    example vectors at the given permissiveness — no model calls, no re-embed.
    This is the seam the permissiveness knob writes through: a handful of dot
    products per workflow, so it runs inline on the settings save. Returns the
    number of workflows retuned."""
    wf_ids = (
        (await session.execute(select(Workflow.id).where(Workflow.type == "intent")))
        .scalars()
        .all()
    )
    retuned = 0
    for wf_id in wf_ids:
        rows = (
            await session.execute(
                select(WorkflowExample.embedding, WorkflowExample.kind).where(
                    WorkflowExample.workflow_id == wf_id,
                    WorkflowExample.embedding.is_not(None),
                )
            )
        ).all()
        pos = [list(e) for e, kind in rows if kind == "positive"]
        if not pos:
            continue  # nothing stored (mid re-embed / unready) — leave as-is
        neg = [list(e) for e, kind in rows if kind == "negative"]
        wf = await session.get(Workflow, wf_id)
        wf.threshold = calibrate_threshold(pos, neg, permissiveness=permissiveness)
        retuned += 1
    return retuned
