"""Unit tests for the pure episode state machine (intent/state.py)."""

from datetime import datetime, timedelta, timezone

from app.intent.schemas import SlotUpdate
from app.intent.state import (
    apply_slot_updates,
    decay_factor,
    effective_slots,
    fingerprint,
    is_converged,
    lifecycle_close_reason,
    normalize_slot_updates,
)

NOW = datetime(2026, 7, 2, 12, 0, tzinfo=timezone.utc)
REQUIRED = [{"name": "date", "description": "agreed date"}, {"name": "title", "description": ""}]
GRACE = timedelta(hours=6)
PER_HOUR = 0.85


def _slot(value, confidence, ts=NOW):
    return {"value": value, "confidence": confidence, "message_id": 1, "ts": ts.isoformat()}


# ---------- slot bookkeeping ----------

def test_apply_fills_and_overwrites():
    slots = apply_slot_updates({}, [SlotUpdate(name="date", value="Tue 7pm", confidence=0.8)], NOW, 10)
    assert slots["date"]["value"] == "Tue 7pm"
    slots = apply_slot_updates(slots, [SlotUpdate(name="date", value="Wed 8pm", confidence=0.9)], NOW, 12)
    assert slots["date"]["value"] == "Wed 8pm"  # last-write-wins


def test_retraction_clears_slot():
    slots = {"date": _slot("Tue", 0.9)}
    slots = apply_slot_updates(slots, [SlotUpdate(name="date", value=None, confidence=0.9)], NOW, 2)
    assert "date" not in slots


def test_low_confidence_update_ignored():
    assert apply_slot_updates({}, [SlotUpdate(name="date", value="maybe Tue?", confidence=0.3)], NOW, 1) == {}


# ---------- slot-name normalization (small models invent names) ----------

LOCATION = [{"name": "location", "description": "where"}]


def _names(updates):
    return [u.name for u in updates]


def test_normalize_keeps_exact_and_remaps_variants():
    updates = [
        SlotUpdate(name="location", value="A", confidence=0.9),
        SlotUpdate(name="Location", value="B", confidence=0.9),
        SlotUpdate(name="location_update", value="Palm Springs", confidence=0.7),  # the live bug
    ]
    assert _names(normalize_slot_updates(updates, LOCATION)) == ["location"] * 3


def test_normalize_drops_unknown_and_ambiguous():
    two = [{"name": "date"}, {"name": "end_date"}]  # 'date' ⊂ 'end_date' → ambiguous
    updates = [
        SlotUpdate(name="weather", value="sunny", confidence=0.9),  # unknown
        SlotUpdate(name="dat", value="Tue", confidence=0.9),  # substring of both
    ]
    assert normalize_slot_updates(updates, two) == []


def test_normalize_word_boundary_extension_only():
    """The verified poisoning case: an invented 'timezone' must NOT land on a
    declared 'time' slot — only `_`-delimited extensions remap."""
    time_slot = [{"name": "time", "description": "when"}]
    updates = [
        SlotUpdate(name="timezone", value="PST", confidence=0.9),  # bare substring → drop
        SlotUpdate(name="meeting_time", value="7pm", confidence=0.9),  # suffix word → remap
        SlotUpdate(name="time_range", value="7-9", confidence=0.9),  # prefix word → remap
    ]
    out = normalize_slot_updates(updates, time_slot)
    assert [(u.name, u.value) for u in out] == [("time", "7pm"), ("time", "7-9")]


def test_normalize_passthrough_for_no_slot_workflows():
    updates = [SlotUpdate(name="anything", value="x", confidence=0.9)]
    assert normalize_slot_updates(updates, []) == updates


# ---------- graduated decay ----------

def test_no_decay_within_grace():
    assert decay_factor(NOW - timedelta(hours=5), NOW, GRACE, PER_HOUR) == 1.0


def test_decay_is_gradual_after_grace():
    # written 8h ago = 2h past grace: 0.85^2 ≈ 0.72 — faded but present
    slots = effective_slots({"date": _slot("Tue", 0.9, ts=NOW - timedelta(hours=8))}, NOW, GRACE, PER_HOUR)
    assert 0.6 < slots["date"]["confidence"] < 0.9
    # 10h past grace: 0.85^10 ≈ 0.197 → below floor → dropped
    slots = effective_slots({"date": _slot("Tue", 0.9, ts=NOW - timedelta(hours=16))}, NOW, GRACE, PER_HOUR)
    assert slots == {}


def test_slots_age_independently():
    stored = {
        "date": _slot("Tue", 0.9, ts=NOW - timedelta(hours=9)),  # 0.9 × 0.85³ ≈ 0.55 → dropped
        "title": _slot("dinner", 0.9, ts=NOW - timedelta(minutes=5)),  # fresh
    }
    eff = effective_slots(stored, NOW, GRACE, PER_HOUR)
    assert "date" not in eff and eff["title"]["confidence"] == 0.9
    assert not is_converged(REQUIRED, eff)
    assert stored["date"]["confidence"] == 0.9  # lazy: stored never mutated


def test_reassertion_restores_full_strength():
    stored = {"date": _slot("Tue", 0.9, ts=NOW - timedelta(hours=9))}
    stored = apply_slot_updates(stored, [SlotUpdate(name="date", value="Tue", confidence=0.9)], NOW, 5)
    eff = effective_slots(stored, NOW, GRACE, PER_HOUR)
    assert eff["date"]["confidence"] == 0.9


def test_effective_slots_tolerates_naive_and_garbage_ts():
    """Imported/hand-edited rows: a naive ts is treated as UTC, an unparseable
    one degrades to fresh — neither crashes convergence (was a TypeError loop)."""
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    slots = {
        "a": {"value": "x", "confidence": 0.9, "ts": naive},
        "b": {"value": "y", "confidence": 0.9, "ts": "not-a-date"},
    }
    eff = effective_slots(slots, NOW, GRACE, PER_HOUR)
    assert eff["a"]["confidence"] == 0.9  # within grace once coerced to UTC
    assert eff["b"]["confidence"] == 0.9


# ---------- convergence ----------

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


def test_no_slot_workflow_converges_trivially():
    assert is_converged([], {})


# ---------- fingerprint ----------

def test_fingerprint_ignores_case_whitespace_and_order():
    a = {"date": _slot("Tue 7PM ", 0.9), "title": _slot("Dinner", 0.8)}
    b = {"title": _slot("dinner", 0.7), "date": _slot("tue 7pm", 0.95)}
    assert fingerprint(a) == fingerprint(b)


def test_fingerprint_differs_on_values():
    a = {"date": _slot("Tue 7pm", 0.9)}
    b = {"date": _slot("Wed 8pm", 0.9)}
    assert fingerprint(a) != fingerprint(b)


# ---------- lifecycle ----------

LIFECYCLE = dict(
    candidate_ttl=timedelta(minutes=20),
    candidate_unrelated_k=3,
    invested_idle=timedelta(hours=12),
    max_age=timedelta(days=7),
    dedup_window=timedelta(hours=24),
)


def _reason(status, *, opened_h=1.0, idle_m=5.0, streak=0, has_slots=False, now=NOW):
    return lifecycle_close_reason(
        status,
        now - timedelta(hours=opened_h),
        now - timedelta(minutes=idle_m),
        streak,
        has_slots,
        now,
        **LIFECYCLE,
    )


def test_vague_candidate_expires_fast():
    assert _reason("candidate", idle_m=5) is None
    assert _reason("candidate", idle_m=25) == "expired"


def test_vague_candidate_closes_on_unrelated_streak():
    assert _reason("candidate", streak=2) is None
    assert _reason("candidate", streak=3) == "expired"


def test_invested_candidate_earns_the_long_leash():
    """Gathered substance — not a status label — buys survival: the same
    candidate that dies at 25 min empty survives for hours with a slot."""
    assert _reason("candidate", idle_m=25, has_slots=True) is None
    assert _reason("candidate", idle_m=11 * 60, has_slots=True) is None
    assert _reason("candidate", idle_m=13 * 60, has_slots=True) == "expired"


def test_invested_candidate_ignores_unrelated_streak():
    assert _reason("candidate", streak=10, has_slots=True) is None


def test_satisfied_closes_after_dedup_window():
    assert _reason("satisfied", idle_m=23 * 60) is None
    assert _reason("satisfied", idle_m=25 * 60) == "done"


def test_parked_and_fired_close_only_via_hard_cap():
    for status in ("converged", "fired"):
        assert _reason(status, idle_m=48 * 60) is None
        assert _reason(status, opened_h=24 * 8, idle_m=5) == "expired"


def test_hard_age_cap_applies_to_all():
    for status in ("candidate", "converged", "fired", "satisfied"):
        assert _reason(status, opened_h=24 * 8, idle_m=1, has_slots=True) == "expired"
