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

from app.media.render import message_body
from app.memory.chunker import render_message
from app.models import IntentEpisode, Message, Workflow
from app.intent.state import render_slots

DETECT_PROMPT = """\
You watch a group chat for this intent:
"{trigger_prompt}"

Details (slots) to extract as the conversation converges:
{slots_desc}

Conversation, oldest first, lines numbered [m1], [m2], … — lines before the
marker are earlier context; lines tagged [bot] are your own assistant's
messages; a reply points to its target as (replying to [mN]), or quotes it
when the original isn't shown:
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

Conversation, oldest first, lines numbered [m1], [m2], … — lines before the
marker are earlier context; lines tagged [bot] are your own assistant's
messages; a reply points to its target as (replying to [mN]), or quotes it
when the original isn't shown:
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

Extract slot updates ONLY for values the group actually converged on, not
proposals still under discussion. Use EXACTLY the slot names listed above —
never invent new names. Emit value=null to retract a slot the group walked
back.
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
    context: list[Message], window: list[Message], targets: dict[int, Message] | None = None
) -> str:
    # Lines are numbered [m1]… so a reply to a VISIBLE message is a pure
    # pointer — "(replying to [m3])" — never duplicated content. Replies to
    # anything off-screen get the full quoted original instead.
    msgs = [*context, *window]
    idx = {m.tg_message_id: i + 1 for i, m in enumerate(msgs)}
    lines: list[str] = []
    for i, m in enumerate(msgs):
        if i == len(context):
            lines.append("--- new messages to classify ---")
        lines.append(_render_line(m, idx, targets))
    return "\n".join(lines)


def _render_line(
    m: Message,
    idx: dict[int, int] | None = None,
    targets: dict[int, Message] | None = None,
) -> str:
    num = (idx or {}).get(m.tg_message_id)
    prefix = f"[m{num}] " if num else ""
    if m.source == "self":
        ts = m.sent_at.strftime("%Y-%m-%d %H:%M")
        line = f"{prefix}[bot] [{ts}]: {m.text}"
    else:
        line = prefix + render_message(m)
    rid = m.reply_to_tg_message_id
    if not rid:
        return line
    j = (idx or {}).get(rid)
    if j is not None:
        return f"{line} (replying to [m{j}])"
    target = (targets or {}).get(rid)
    if target is None:
        return line
    q = message_body(target).replace("\n", " ")
    if len(q) > 140:
        q = q[:140] + "…"
    return f'{line}\n  ↳ (this replies to {target.sender_name or "Unknown"}, earlier: "{q}")'


def render_episodes(episodes: list[IntentEpisode]) -> str:
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
            lines.append("   gathered: " + "; ".join(
                f"{name}={info['value']}" for name, info in sorted(ep.slots.items())
            ))
        if ep.status in ("fired", "satisfied") and ep.execution_summary:
            lines.append(f"   An automation already ran for this: {ep.execution_summary}")
    return "\n".join(lines)


def build_detect_prompt(
    workflow: Workflow,
    context: list[Message],
    window: list[Message],
    quoted: dict[int, Message] | None = None,
) -> str:
    return DETECT_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        slots_desc=slots_desc(workflow),
        transcript=render_transcript(context, window, quoted),
    )


def build_attribution_prompt(
    workflow: Workflow,
    episodes: list[IntentEpisode],
    context: list[Message],
    window: list[Message],
    quoted: dict[int, Message] | None = None,
) -> str:
    return ATTRIBUTION_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        slots_desc=slots_desc(workflow),
        episodes=render_episodes(episodes),
        transcript=render_transcript(context, window, quoted),
    )


def build_recheck_prompt(
    workflow: Workflow, episode: IntentEpisode, since: list[Message]
) -> str:
    return RECHECK_PROMPT.format(
        trigger_prompt=workflow.trigger_prompt,
        summary=episode.summary or "(no summary)",
        slots=render_slots(episode.slots or {}),
        transcript="\n".join(_render_line(m) for m in since) or "(nothing)",
    )
