"""Prompt-rendering tests (intent/prompts.py).

The classifier must see a sub-fire-bar slot's confidence and the bar it
needs — rendered as plainly gathered, the model never re-extracts it, the
confidence never refreshes, and a workflow deadlocks at "n/n details"
without firing (prod, Jul 11: date=tomorrow stored at 0.6 against a 0.7
fire bar). The comparison uses DECAYED confidence — what the fire check
actually sees — or the same deadlock returns via decay.
"""

from datetime import datetime, timedelta, timezone

from app.intent.prompts import render_episodes
from app.models import IntentEpisode

NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)
GRACE = timedelta(hours=6)
PER_HOUR = 0.85


def _episode(**kw) -> IntentEpisode:
    ep = IntentEpisode(
        workflow_id=1, chat_id=1, thread_key=0, status="candidate", summary="hike tomorrow"
    )
    for key, value in kw.items():
        setattr(ep, key, value)
    return ep


def _slot(value, confidence, ts=NOW):
    return {"value": value, "confidence": confidence, "message_id": 1, "ts": ts.isoformat()}


def test_render_episodes_shows_confidence_on_every_gathering_slot():
    # One evidence rule for the classifier needs every slot's number visible:
    # sub-bar slots carry the bar they still need, confirmed slots just their
    # confidence.
    ep = _episode(slots={"time": _slot("11am", 0.85), "date": _slot("tomorrow", 0.6)})
    out = render_episodes([ep], 0.7, NOW, GRACE, PER_HOUR)
    assert "date=tomorrow (confidence 0.60, needs 0.70)" in out
    assert "time=11am (confidence 0.85)" in out
    assert "time=11am (confidence 0.85, needs" not in out


def test_render_episodes_uses_decayed_confidence():
    # Written 9h ago (3h past grace): 0.9 × 0.85³ ≈ 0.55 — the fire check no
    # longer counts it, so the prompt must say so even though the STORED
    # confidence still clears the bar.
    ep = _episode(slots={"date": _slot("Tue", 0.9, ts=NOW - timedelta(hours=9))})
    out = render_episodes([ep], 0.7, NOW, GRACE, PER_HOUR)
    assert "date=Tue (confidence 0.55, needs 0.70)" in out


def test_render_episodes_all_confirmed_has_no_needs_marker():
    ep = _episode(slots={"date": _slot("Tue", 0.9)})
    out = render_episodes([ep], 0.7, NOW, GRACE, PER_HOUR)
    assert "date=Tue (confidence 0.90)" in out
    assert "needs" not in out


def test_render_episodes_no_marker_on_handled_topics():
    # A fired/satisfied topic discards slot updates — asking the classifier to
    # re-confirm a faded detail there contradicts the "already ran" line
    # rendered right below it.
    for status in ("fired", "satisfied"):
        ep = _episode(status=status, slots={"date": _slot("tomorrow", 0.6)})
        out = render_episodes([ep], 0.7, NOW, GRACE, PER_HOUR)
        assert "date=tomorrow" in out
        assert "(confidence" not in out
