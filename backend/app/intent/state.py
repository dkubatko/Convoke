"""Convergence slot state machine — pure functions, Postgres-persisted state.

Slots carry (value, confidence, message_id, ts); last-write-wins, explicit
retractions (value=None) clear a slot, and untouched state decays after a TTL
so a topic resurfacing weeks later starts fresh.
"""

from datetime import datetime, timedelta

from app.intent.schemas import IntentVerdict

MIN_SLOT_CONFIDENCE = 0.6
MIN_FIRE_CONFIDENCE = 0.7


def decay_state(slots: dict, last_match_at: datetime | None, now: datetime, ttl: timedelta) -> dict:
    if last_match_at is not None and now - last_match_at > ttl:
        return {}
    return slots


def apply_verdict(slots: dict, verdict: IntentVerdict, now: datetime, window_last_msg_id: int) -> dict:
    if not verdict.match:
        return slots
    out = dict(slots)
    for update in verdict.slot_updates:
        if update.value is None:
            out.pop(update.name, None)
            continue
        if update.confidence < MIN_SLOT_CONFIDENCE:
            continue
        out[update.name] = {
            "value": update.value,
            "confidence": update.confidence,
            "message_id": window_last_msg_id,
            "ts": now.isoformat(),
        }
    return out


def is_converged(required_slots: list[dict], slots: dict) -> bool:
    """Fires only when every required slot is filled with enough confidence.
    A workflow with no required slots converges on any confident match."""
    if not required_slots:
        return True  # convergence = any confident match (caller checks the verdict)
    for spec in required_slots:
        filled = slots.get(spec["name"])
        if filled is None or filled.get("confidence", 0) < MIN_FIRE_CONFIDENCE:
            return False
    return True


def render_slots(slots: dict) -> str:
    if not slots:
        return "(none)"
    return "\n".join(f"- {name}: {info['value']}" for name, info in sorted(slots.items()))
