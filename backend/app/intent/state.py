"""Episode state machine — pure functions, no I/O.

An episode is one occurrence of a workflow's intent (see IntentEpisode).
These functions decide slot bookkeeping, graduated decay, convergence,
duplicate fingerprints, and time-based lifecycle closes; the pipeline and
episodes.py apply their results to rows.

Decay model: slot confidences are stored as-written and decayed LAZILY —
`effective = stored × per_hour^(age_hours − grace)` where age is measured
from the slot's OWN write time. A value the group settled hours ago fades
until it can no longer satisfy the fire bar; re-asserting it (the classifier
extracting it again) writes a fresh timestamp and restores full strength. So
a stale half-negotiated plan can't fire days later, without the old TTL
cliff — and without ambient chatter silently re-validating old values.
"""

import hashlib
import json
from datetime import datetime, timedelta

from app.intent.schemas import SlotUpdate

MIN_SLOT_CONFIDENCE = 0.6
MIN_FIRE_CONFIDENCE = 0.7


def decay_factor(
    written_at: datetime, now: datetime, grace: timedelta, per_hour: float
) -> float:
    age = now - written_at
    if age <= grace:
        return 1.0
    hours = (age - grace).total_seconds() / 3600
    return per_hour**hours


def effective_slots(
    slots: dict, now: datetime, grace: timedelta, per_hour: float
) -> dict:
    """Slots with decayed confidences, each aged from its own write time;
    slots that faded below the floor drop out entirely. Stored values are
    never mutated — recompute on each read."""
    out = {}
    for name, info in slots.items():
        ts = info.get("ts")
        written_at = datetime.fromisoformat(ts) if ts else now
        confidence = info.get("confidence", 0) * decay_factor(written_at, now, grace, per_hour)
        if confidence >= MIN_SLOT_CONFIDENCE:
            out[name] = {**info, "confidence": confidence}
    return out


def _canon(name: str) -> str:
    return "".join(c for c in name.lower() if c.isalnum())


def normalize_slot_updates(
    updates: list[SlotUpdate], required_slots: list[dict]
) -> list[SlotUpdate]:
    """Small models sometimes invent slot names ('location_update' for a slot
    declared as 'location'). Map unambiguous variants onto declared names and
    drop the rest — a phantom slot never counts toward convergence but WOULD
    render as gathered, leaving a workflow looking stuck at 'n/n details'.
    No-slot workflows keep everything: extra context is harmless there."""
    if not required_slots:
        return updates
    declared = {s["name"] for s in required_slots}
    by_canon = {_canon(s["name"]): s["name"] for s in required_slots}
    out = []
    for update in updates:
        if update.name in declared:
            out.append(update)
            continue
        canon = _canon(update.name)
        exact = by_canon.get(canon)
        if exact is None:
            # unique substring match either way ('locationupdate' ⊃ 'location')
            matches = [n for c, n in by_canon.items() if c in canon or canon in c]
            exact = matches[0] if len(matches) == 1 else None
        if exact is None:
            continue  # unknown name — drop rather than store a phantom slot
        out.append(update.model_copy(update={"name": exact}))
    return out


def apply_slot_updates(
    slots: dict, updates: list[SlotUpdate], now: datetime, window_last_msg_id: int
) -> dict:
    """Last-write-wins; value=None retracts; low-confidence updates ignored."""
    out = dict(slots)
    for update in updates:
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
    """Fires only when every required slot is filled with enough confidence —
    call with EFFECTIVE (decayed) slots. A workflow with no required slots
    converges on any confident match (caller checks the verdict)."""
    if not required_slots:
        return True
    for spec in required_slots:
        filled = slots.get(spec["name"])
        if filled is None or filled.get("confidence", 0) < MIN_FIRE_CONFIDENCE:
            return False
    return True


def fingerprint(slots: dict) -> str:
    """Exact-match dedup key over the resolved slot values. Deliberately a
    fast path only ("7pm" ≠ "19:00") — semantic dedup is the classifier's
    continuation verdict against the episode's execution summary."""
    canonical = sorted(
        (name, str(info.get("value", "")).strip().lower()) for name, info in slots.items()
    )
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


def lifecycle_close_reason(
    status: str,
    opened_at: datetime,
    last_activity_at: datetime,
    unrelated_streak: int,
    has_slots: bool,
    now: datetime,
    *,
    candidate_ttl: timedelta,
    candidate_unrelated_k: int,
    invested_idle: timedelta,
    max_age: timedelta,
    dedup_window: timedelta,
) -> str | None:
    """Time/streak-based close for an open episode, or None to keep it.

    A candidate's leash is derived from SUBSTANCE: with no gathered slots it
    dies fast (short TTL, or a streak of off-topic checks); once details have
    landed it earns the long leash and survives interruptions. Idle is
    measured from attributed activity only — unrelated chatter never keeps an
    episode alive. `converged` (parked) and `fired` episodes have in-flight
    work and close only via the hard age cap; their other exits are
    event-driven (recheck, executor revert, agent completion).
    """
    if now - opened_at > max_age:
        return "expired"
    idle = now - last_activity_at
    if status == "candidate":
        if has_slots:
            if idle > invested_idle:
                return "expired"
        else:
            if unrelated_streak >= candidate_unrelated_k:
                return "expired"
            if idle > candidate_ttl:
                return "expired"
    elif status == "satisfied":
        if idle > dedup_window:
            return "done"
    return None


def render_slots(slots: dict) -> str:
    if not slots:
        return "(none)"
    return "\n".join(f"- {name}: {info['value']}" for name, info in sorted(slots.items()))
