from pydantic import BaseModel, Field


class SlotUpdate(BaseModel):
    name: str = Field(description="Slot name from the required slots list")
    value: str | None = Field(
        description="New value, or null to RETRACT a previously filled slot "
        "(e.g. the group walked back a decision)"
    )
    confidence: float = Field(ge=0, le=1)


class IntentVerdict(BaseModel):
    """Structured output of the cheap intent classifier."""

    match: bool = Field(
        description="True if this conversation window advances the described intent"
    )
    confidence: float = Field(ge=0, le=1)
    slot_updates: list[SlotUpdate] = Field(
        default_factory=list,
        description="Slot values this window establishes, changes, or retracts",
    )


class GeneratedExamples(BaseModel):
    """Synthetic utterances produced at workflow save time."""

    positives: list[str] = Field(
        description="15-20 short chat messages a group member might realistically "
        "send while expressing this intent; vary phrasing and language"
    )
    negatives: list[str] = Field(
        description="6-10 near-miss messages that look related but do NOT express "
        "the intent (hard negatives)"
    )
