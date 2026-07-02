"""Example-utterance generation + prefilter threshold calibration.

The prefilter matches chat windows against synthetic example utterances, not
the trigger prompt: prompts are meta-language ("when there is intent to…"),
while real messages look like "does Tue 7pm work?" — often in other languages.
The strong model generates positives and hard negatives at save time; the
threshold lands between how positives cluster and how close the best negative
gets, so it stays loose (recall-first — the classifier supplies precision).
"""

import logging
from datetime import datetime, timezone

from pydantic_ai import Agent
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.models import ProviderNotConfigured, build_model, get_provider
from app.intent.schemas import GeneratedExamples
from app.memory.embeddings import Embedder
from app.models import Workflow, WorkflowExample

log = logging.getLogger("convoke.intent.examples")

DEFAULT_THRESHOLD = 0.80
THRESHOLD_FLOOR = 0.70
THRESHOLD_CEIL = 0.92

GENERATION_PROMPT = """\
A Telegram group-chat bot watches conversations for this intent:

"{trigger_prompt}"

Data to extract when the intent occurs (slots): {slots}

Generate example chat messages for training a semantic prefilter:
- positives: realistic short messages members might send while this intent is \
being expressed or converging. Vary phrasing, formality and language (include \
a few in Spanish/Russian/German if plausible for a generic group).
- negatives: near-misses — same topic area but NOT expressing the intent.
"""


def dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def calibrate_threshold(
    positive_vecs: list[list[float]], negative_vecs: list[list[float]]
) -> float:
    """Midpoint between how positives cluster among themselves and how close
    the best negative gets to any positive (vectors are normalized)."""
    if not positive_vecs:
        return DEFAULT_THRESHOLD
    pos_sims = []
    for i, v in enumerate(positive_vecs):
        best = max(
            (dot(v, w) for j, w in enumerate(positive_vecs) if j != i), default=None
        )
        if best is not None:
            pos_sims.append(best)
    pos_floor = min(pos_sims) if pos_sims else DEFAULT_THRESHOLD
    neg_ceiling = max(
        (dot(n, p) for n in negative_vecs for p in positive_vecs), default=None
    )
    if neg_ceiling is None:
        threshold = pos_floor - 0.05
    else:
        threshold = (pos_floor + neg_ceiling) / 2
    return max(THRESHOLD_FLOOR, min(THRESHOLD_CEIL, threshold))


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

        pos_vecs = await embedder.embed_passages(generated.positives) if generated.positives else []
        neg_vecs = await embedder.embed_passages(generated.negatives) if generated.negatives else []

        await session.execute(
            delete(WorkflowExample).where(WorkflowExample.workflow_id == workflow_id)
        )
        for text, vec in zip(generated.positives, pos_vecs):
            session.add(
                WorkflowExample(workflow_id=workflow_id, kind="positive", text=text, embedding=vec)
            )
        for text, vec in zip(generated.negatives, neg_vecs):
            session.add(
                WorkflowExample(workflow_id=workflow_id, kind="negative", text=text, embedding=vec)
            )
        wf.threshold = calibrate_threshold(pos_vecs, neg_vecs)
        wf.examples_status = status
        wf.updated_at = datetime.now(timezone.utc)
        await session.commit()
        log.info(
            "workflow %s examples %s: %d pos / %d neg, threshold %.3f",
            workflow_id, status, len(generated.positives), len(generated.negatives), wf.threshold,
        )


async def _generate(session: AsyncSession, wf: Workflow) -> GeneratedExamples:
    provider = await get_provider(session, "agent")
    slots_desc = (
        "; ".join(f"{s['name']} ({s.get('description', '')})" for s in wf.required_slots or [])
        or "none"
    )
    agent = Agent(build_model(provider), output_type=GeneratedExamples)
    result = await agent.run(
        GENERATION_PROMPT.format(trigger_prompt=wf.trigger_prompt, slots=slots_desc)
    )
    return result.output


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
