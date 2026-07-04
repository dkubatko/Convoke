from typing import Literal

from pydantic import BaseModel, Field


class SlotUpdate(BaseModel):
    name: str = Field(description="Slot name from the required slots list")
    value: str | None = Field(
        description="New value, or null to RETRACT a previously filled slot "
        "(e.g. the group walked back a decision)"
    )
    confidence: float = Field(default=0.7, ge=0, le=1)


# Fields are defaulted wherever possible so a small model that omits one still
# yields a usable verdict instead of failing validation (pydantic-ai re-prompts
# on validation errors, but each retry costs a call).


class DetectVerdict(BaseModel):
    """Classifier output when NO episode of this intent is being tracked."""

    relation: Literal["unrelated", "ambiguous", "active"] = Field(
        description="unrelated: nothing to do with the intent. "
        "ambiguous: plausibly about it but not clear enough yet. "
        "active: the group is clearly engaging in this intent."
    )
    confidence: float = Field(default=0.7, ge=0, le=1)
    topic_summary: str = Field(
        default="",
        description="1-2 sentences naming this SPECIFIC occurrence (who/what/when), "
        "not the intent in general",
    )
    slot_updates: list[SlotUpdate] = Field(
        default_factory=list,
        description="Slot values these messages establish, change, or retract",
    )


class AttributionVerdict(BaseModel):
    """Classifier output when 1+ episodes of this intent are being tracked."""

    relation: Literal["unrelated", "continues_episode", "new_instance"] = Field(
        description="continues_episode: the new messages continue, confirm, thank for, "
        "or refine a listed topic (including one already handled). "
        "new_instance: a distinctly separate occurrence of the intent "
        "(different event/subject/timeframe). "
        "unrelated: neither."
    )
    episode_ref: int | None = Field(
        default=None,
        description="For continues_episode: the number of the topic being continued",
    )
    confidence: float = Field(default=0.7, ge=0, le=1)
    topic_summary: str = Field(
        default="",
        description="Updated 1-2 sentence summary of the topic these messages belong to",
    )
    slot_updates: list[SlotUpdate] = Field(
        default_factory=list,
        description="Slot values these messages establish, change, or retract",
    )
    topic_concluded: bool = Field(
        default=False,
        description="True if the group explicitly dropped or finished the topic "
        "('nah, forget it', 'already handled it ourselves')",
    )


class RecheckVerdict(BaseModel):
    """One question asked before firing a topic that waited out a rate limit:
    given what happened since, is acting still wanted?"""

    still_wanted: bool = Field(
        description="True unless the later messages show the group resolved, "
        "cancelled, or abandoned the plan"
    )
    confidence: float = Field(default=0.7, ge=0, le=1)
    reason: str = Field(default="", description="One short sentence of justification")


class GeneratedExamples(BaseModel):
    """Synthetic utterances produced at workflow save time."""

    positives: list[str] = Field(
        description="Short chat messages a group member might realistically send "
        "while expressing this intent; vary phrasing and language"
    )
    negatives: list[str] = Field(
        description="Near-miss messages that look related but do NOT express the "
        "intent (hard negatives)"
    )
