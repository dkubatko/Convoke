"""Embedding model selection + swap progress.

POST hands the worker's re-embed job a target; the current config is only
overwritten once the model proves loadable. GET drives the Models-page card."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_operator
from app.memory.embeddings import EMBEDDING_REGISTRY
from app.memory.runtime import spec_for
from app.models import EmbeddingState

router = APIRouter(dependencies=[Depends(require_operator)])


class RegistryEntryOut(BaseModel):
    id: str
    label: str
    dim: int | None


class EmbeddingStateOut(BaseModel):
    model_id: str
    dim: int
    status: str
    phase: str | None
    total: int
    done: int
    error: str | None
    target_model_id: str | None
    started_at: datetime | None
    finished_at: datetime | None


class EmbeddingsOut(BaseModel):
    current: EmbeddingStateOut
    registry: list[RegistryEntryOut]


class SwitchIn(BaseModel):
    model_id: str
    # For custom HuggingFace ids only; registry models know their dimension.
    dim: int | None = None


async def _state_or_503(session: AsyncSession) -> EmbeddingState:
    state = await session.get(EmbeddingState, 1)
    if state is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Embedding state not initialized (migrations pending?)"
        )
    return state


def _out(state: EmbeddingState) -> EmbeddingsOut:
    return EmbeddingsOut(
        current=EmbeddingStateOut(
            model_id=state.model_id,
            dim=state.dim,
            status=state.status,
            phase=state.phase,
            total=state.total,
            done=state.done,
            error=state.error,
            target_model_id=(state.target or {}).get("model_id"),
            started_at=state.started_at,
            finished_at=state.finished_at,
        ),
        registry=[
            RegistryEntryOut(id=s.id, label=s.label, dim=s.dim)
            for s in EMBEDDING_REGISTRY.values()
        ],
    )


@router.get("/embeddings", response_model=EmbeddingsOut)
async def get_embeddings(session: AsyncSession = Depends(get_session)) -> EmbeddingsOut:
    return _out(await _state_or_503(session))


@router.post("/embeddings/model", response_model=EmbeddingsOut, status_code=status.HTTP_202_ACCEPTED)
async def switch_model(
    body: SwitchIn, session: AsyncSession = Depends(get_session)
) -> EmbeddingsOut:
    """Queue a model swap. The worker's re-embed job probes the model, resizes
    the vector columns, re-embeds everything, and recalibrates workflow
    thresholds. Search and the intent prefilter degrade gracefully meanwhile."""
    state = await _state_or_503(session)
    if state.status == "reembedding":
        raise HTTPException(status.HTTP_409_CONFLICT, "A re-embed is already running")
    spec = spec_for(body.model_id, body.dim)
    state.target = {
        "model_id": spec.id,
        "dim": spec.dim,
        "doc_prefix": spec.doc_prefix,
        "query_prefix": spec.query_prefix,
    }
    state.status = "reembedding"
    state.phase = "queued"
    state.total = 0
    state.done = 0
    state.error = None
    state.started_at = datetime.now(timezone.utc)
    state.finished_at = None
    await session.commit()
    return _out(state)
