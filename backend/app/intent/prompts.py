"""Prompt builders for the intent classifier's three calls.

Detect: no episode of this intent is being tracked — is one starting?
Attribution: episodes exist — do the new messages continue one (possibly one
already handled by the agent), start a distinctly new occurrence, or neither?
Recheck: a converged episode waited out a rate-limit cooldown — given what
was said since, is acting still wanted?

The transcript interleaves the bot's own messages tagged [bot] so the
classifier can read the dialogue ("the bot already confirmed 8pm; the user is
thanking it") — bot messages are never window/prefilter input, context only.
"""

from datetime import datetime, timedelta

from app.memory.chunker import render_message, reply_annotation
from app.models import IntentEpisode, Message, Workflow
from app.intent.state import decayed_slots, render_slots

DETECT_PROMPT = """\
You watch a group chat for this intent:
"{trigger_prompt}"

Details (slots) to extract as the conversation converges:
{slots_desc}

Conversation, oldest first. Each line is "Sender [time] #id: text"; lines
before the marker are earlier context; lines tagged [bot] are your own
assistant's messages; a reply points to its target as (replying to #id), or
quotes the original when it isn't shown:
{transcript}

Classify the NEW messages against the intent:
- "unrelated": nothing to do with it.
- "ambiguous": plausibly about it — worth watching, but not enough to act on.
- "clear": unmistakably expresses this intent, even if the group hasn't
  settled any details yet.

If ambiguous or clear, return topic_summary: 1-2 sentences naming this
specific occurrence (who/what/when), not the intent in general.

Extract slot updates ONLY for values the group actually converged on, not
proposals still under discussion. Use EXACTLY the slot names listed above —
never invent new names. Emit value=null to retract a slot the group walked
back ("actually let's do Wednesday instead").
"""

ATTRIBUTION_PROMPT = """\
You watch a group chat for this intent:
"{trigger_prompt}"

Details (slots) to extract as the conversation converges:
{slots_desc}

Topics of this intent currently being tracked in this chat:
{episodes}

Conversation, oldest first. Each line is "Sender [time] #id: text"; lines
before the marker are earlier context; lines tagged [bot] are your own
assistant's messages; a reply points to its target as (replying to #id), or
quotes the original when it isn't shown:
{transcript}

Classify how the NEW messages relate to the tracked topics:
- "continues_episode": they continue one of the topics above. For a topic
  still in progress this includes confirming, refining, or adding details —
  in particular, when an in-progress topic is missing details and the new
  messages could be supplying them, that is continues_episode, not a new
  instance. For a topic marked ALREADY HANDLED, count ONLY acknowledgments,
  thanks, or restatements that add nothing new. Set episode_ref to that
  topic's number.
- "new_instance": they start a separate occurrence of the intent — a
  different subject, occasion, or timeframe — OR they bring new or changed
  substance to an already-handled topic. When unsure between the two for a
  handled topic, choose new_instance: acting twice is visible and can be
  cancelled; a dropped topic is silent.
- "unrelated": neither.

Return an updated topic_summary (1-2 sentences) for the topic the messages
belong to. Set topic_concluded=true only if the group explicitly dropped or
finished the topic themselves ("nah, forget it", "we already took care of it").
That includes a pivot: when they drop a tracked topic in the same breath that
starts a new one ("forget the hike, let's do a picnic instead" — a
new_instance), set topic_concluded=true so the dropped topic is closed.

Extract slot updates ONLY for values the group actually converged on, not
proposals still under discussion. Use EXACTLY the slot names listed above —
never invent new names. Emit value=null to retract a slot the group walked
back.

Every gathered value is shown with its current confidence; one marked
"needs Y" does not yet count toward acting. By default leave gathered
values alone — re-emit one ONLY when the new messages carry signal about
it. When they do, the re-emit is mandatory: state the value and confidence
you now believe. A restatement, confirmation, or the group proceeding on a
value raises its confidence; hedging, questioning, or reopening it lowers
its confidence while KEEPING the value; emit value=null only when the group
explicitly walked it back or deferred the decision.
"""

RECHECK_PROMPT = """\
An automation for this intent:
"{trigger_prompt}"

was about to act on this topic:
{summary}

with these gathered details:
{slots}

but waited out a rate limit. Since then the group said:
{transcript}

Is acting on this topic STILL wanted? Answer still_wanted=false only if the
later messages show the group resolved, cancelled, or abandoned it.
"""


def slots_desc(workflow: Workflow) -> str:
    return (
        "\n".join(
            f"- {s['name']}: {s.get('description', '')}" for s in workflow.required_slots or []
        )
        or "(none — the intent itself is the only requirement)"
    )


def render_transcript(
    context: list[Message],
    window: list[Message],
    targets: dict[int, Message],
    names: dict[int, str],
) -> str:
    # Same #id transcript the agent reads; the marker splits earlier context
    # from the messages to classify, and reply_annotation adds the shared
    # (replying to #id) pointer / quoted original.
    msgs = [*context, *window]
    present = {m.tg_message_id for m in msgs}
    targets = targets or {}
    lines: list[str] = []
    for i, m in enumerate(msgs):
        if i == len(context):
            lines.append("--- new messages to classify ---")
        # render_message tags bot-authored lines [bot] itself (source='self'
        # covers the operating bot — the only bot that appears in live windows).
        lines.append(render_message(m, names) + reply_annotation(m, present, targets, names))
    return "\n".join(lines)


def render_episodes(
    episodes: list[IntentEpisode],
    min_fire_confidence: float,
    now: datetime,
    decay_grace: timedelta,
    decay_per_hour: float,
) -> str:
    lines = []
    for i, ep in enumerate(episodes, start=1):
        state = {
            "candidate": "in progress" if ep.slots else "possibly starting",
            "converged": "ready to act (waiting out a rate limit)",
            "fired": "being acted on right now",
            "satisfied": "ALREADY HANDLED",
        }.get(ep.status, ep.status)
        lines.append(f"{i}. [{state}] {ep.summary or '(no summary yet)'}")
        if ep.slots:
            # On a topic still being gathered, EVERY slot renders with its
            # confidence — one evidence rule covers confirming, demoting,
            # revising, and retracting; a slot shown as a bare value reads as
            # settled and the classifier never re-emits it (the "n/n details"
            # deadlock). Sub-bar slots additionally carry the bar they need.
            # DECAYED confidence, because that's what the fire check uses: a
            # value the group settled hours ago fades, and the raw stored
            # number would hide that it no longer counts. Fired/satisfied
            # topics render plain values: their slot updates are discarded,
            # so asking for a re-confirm there only contradicts the "already
            # ran" line below it.
            gathering = ep.status in ("candidate", "converged")
            shown = decayed_slots(ep.slots, now, decay_grace, decay_per_hour)
            lines.append("   gathered: " + "; ".join(
                f"{name}={info['value']}"
                if not gathering
                else (
                    f"{name}={info['value']} (confidence {info['confidence']:.2f})"
                    if info["confidence"] >= min_fire_confidence
                    else (
                        f"{name}={info['value']} (confidence "
                        f"{info['confidence']:.2f}, needs {min_fire_confidence:.2f})"
                    )
                )
                for name, info in sorted(shown.items())
            ))
        if ep.status in ("fired", "satisfied") and ep.execution_summary:
            lines.append(f"   An automation already ran for this: {ep.execution_summary}")
    return "\n".join(lines)


def build_detect_prompt(
    workflow: Workflow,
    context: list[Message],
    window: list[Message],
    quoted: dict[int, Message],
    names: dict[int, str],
) -> str:
    return DETECT_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        slots_desc=slots_desc(workflow),
        transcript=render_transcript(context, window, quoted, names),
    )


def build_attribution_prompt(
    workflow: Workflow,
    episodes: list[IntentEpisode],
    context: list[Message],
    window: list[Message],
    quoted: dict[int, Message],
    names: dict[int, str],
    min_fire_confidence: float,
    now: datetime,
    decay_grace: timedelta,
    decay_per_hour: float,
) -> str:
    return ATTRIBUTION_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        slots_desc=slots_desc(workflow),
        episodes=render_episodes(episodes, min_fire_confidence, now, decay_grace, decay_per_hour),
        transcript=render_transcript(context, window, quoted, names),
    )


def build_recheck_prompt(
    workflow: Workflow,
    episode: IntentEpisode,
    since: list[Message],
    names: dict[int, str],
) -> str:
    return RECHECK_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        summary=episode.summary or "(no summary)",
        slots=render_slots(episode.slots or {}),
        transcript="\n".join(render_message(m, names) for m in since) or "(nothing)",
    )
