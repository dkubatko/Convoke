"""Embedding model selection + swap progress, per embedder role.

Two roles: 'intent' (the prefilter gate over workflow examples) and 'memory'
(chat-history retrieval over chunks + notes), each with its own registry,
state row, and re-embed lifecycle. POST hands the worker's re-embed job a
target; the current config is only overwritten once the model proves
loadable. Re-POSTing the current model is the supported way to REBUILD a
role's index (memory re-cuts chunks against the chunk-size setting). GET
drives the Models-page cards."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.security import require_operator
from app.memory.embeddings import ROLES, registry_for
from app.memory.runtime import embedding_state_for, spec_for
from app.models import EmbeddingState

router = APIRouter(dependencies=[Depends(require_operator)])


class RegistryEntryOut(BaseModel):
    id: str
    label: str
    dim: int | None


class EmbeddingStateOut(BaseModel):
    model_id: str
    dim: int
    max_tokens: int
    status: str
    phase: str | None
    total: int
    done: int
    error: str | None
    target_model_id: str | None
    started_at: datetime | None
    finished_at: datetime | None


class EmbeddingsOut(BaseModel):
    role: str
    current: EmbeddingStateOut
    registry: list[RegistryEntryOut]


class SwitchIn(BaseModel):
    model_id: str
    # For custom HuggingFace ids only; registry models know their dimension.
    dim: int | None = None


def _role_or_404(role: str) -> str:
    if role not in ROLES:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown embedder role {role!r}")
    return role


async def _state_or_503(session: AsyncSession, role: str) -> EmbeddingState:
    state = await embedding_state_for(session, role)
    if state is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Embedding state not initialized (migrations pending?)"
        )
    return state


def _out(state: EmbeddingState) -> EmbeddingsOut:
    return EmbeddingsOut(
        role=state.role,
        current=EmbeddingStateOut(
            model_id=state.model_id,
            dim=state.dim,
            max_tokens=state.max_tokens,
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
            for s in registry_for(state.role).values()
        ],
    )


@router.get("/embeddings/{role}", response_model=EmbeddingsOut)
async def get_embeddings(
    role: str, session: AsyncSession = Depends(get_session)
) -> EmbeddingsOut:
    return _out(await _state_or_503(session, _role_or_404(role)))


@router.post(
    "/embeddings/{role}/model",
    response_model=EmbeddingsOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def switch_model(
    role: str, body: SwitchIn, session: AsyncSession = Depends(get_session)
) -> EmbeddingsOut:
    """Queue a model swap (or, with the current model, a rebuild). The
    worker's re-embed job probes the model, resizes the role's vector
    columns, rebuilds its vectors (memory: re-chunks history first), and for
    intent recalibrates workflow thresholds. Search and the prefilter degrade
    gracefully meanwhile."""
    state = await _state_or_503(session, _role_or_404(role))
    if state.status == "reembedding":
        raise HTTPException(status.HTTP_409_CONFLICT, "A re-embed is already running")
    spec = spec_for(role, body.model_id, body.dim)
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
