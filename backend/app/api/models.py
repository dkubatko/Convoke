"""Model library + role assignments.

The library holds many OpenAI-compatible endpoints with probed capability
flags; each execution role (agent / intent / vision / transcription / video)
is assigned one. Replaces the old role-keyed /api/providers contract."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.models import probe_capabilities, probe_reasoning
from app.core.crypto import decrypt, encrypt
from app.core.db import get_session
from app.core.security import require_operator
from app.models import ConnectedModel, MessageAttachment, ModelRoleAssignment
from app.models.agents import MODEL_ROLES, ROLE_ATTACHMENT_KINDS, ROLE_REQUIRED_CAPABILITY

router = APIRouter(dependencies=[Depends(require_operator)])


class ModelIn(BaseModel):
    name: str
    base_url: str
    model_name: str
    # None = keep existing key; "" = clear (endpoint needs no key)
    api_key: str | None = None
    capabilities: dict[str, bool] = {}


class ModelOut(BaseModel):
    id: int
    name: str
    base_url: str
    model_name: str
    has_api_key: bool
    capabilities: dict[str, bool]
    last_tested_at: datetime | None
    last_test_detail: str | None
    assigned_roles: list[str]
    updated_at: datetime


class CapabilityProbe(BaseModel):
    ok: bool
    detail: str


class ModelTestIn(BaseModel):
    base_url: str
    model_name: str
    # None = fall back to the key already saved for `model_id` (if any);
    # "" = explicitly no key.
    api_key: str | None = None
    model_id: int | None = None


class ModelTestOut(BaseModel):
    chat: CapabilityProbe
    vision: CapabilityProbe
    transcription: CapabilityProbe


class RoleAssignmentIn(BaseModel):
    model_id: int
    # None = Default: the reasoning parameter is omitted from this role's
    # calls. Any non-empty string (low/medium/high or provider-specific) is
    # validated with a live micro-call before the assignment saves.
    reasoning_effort: str | None = Field(default=None, max_length=32)


class RoleAssignmentOut(BaseModel):
    role: str
    model_id: int | None
    model_name: str | None  # display name of the assigned library entry
    required_capability: str
    # False only when a model is assigned and lacks the required capability.
    capability_ok: bool
    reasoning_effort: str | None


async def _assigned_roles(session: AsyncSession) -> dict[int, list[str]]:
    rows = (await session.execute(select(ModelRoleAssignment))).scalars()
    out: dict[int, list[str]] = {}
    for a in rows:
        out.setdefault(a.model_id, []).append(a.role)
    return out


def _out(m: ConnectedModel, roles: list[str]) -> ModelOut:
    return ModelOut(
        id=m.id,
        name=m.name,
        base_url=m.base_url,
        model_name=m.model_name,
        has_api_key=m.api_key_encrypted is not None,
        capabilities={k: bool(v) for k, v in (m.capabilities or {}).items()},
        last_tested_at=m.last_tested_at,
        last_test_detail=m.last_test_detail,
        assigned_roles=sorted(roles),
        updated_at=m.updated_at,
    )


@router.get("/models", response_model=list[ModelOut])
async def list_models(session: AsyncSession = Depends(get_session)) -> list[ModelOut]:
    assigned = await _assigned_roles(session)
    rows = (await session.execute(select(ConnectedModel).order_by(ConnectedModel.id))).scalars()
    return [_out(m, assigned.get(m.id, [])) for m in rows]


@router.post("/models/test", response_model=ModelTestOut)
async def test_model(
    body: ModelTestIn, session: AsyncSession = Depends(get_session)
) -> ModelTestOut:
    """Probe chat, vision, and transcription concurrently so a typo'd URL or
    bad key is caught — and modality flags detected — before saving."""
    api_key = body.api_key
    stored = await session.get(ConnectedModel, body.model_id) if body.model_id is not None else None
    if api_key is None and stored is not None and stored.api_key_encrypted:
        api_key = decrypt(stored.api_key_encrypted)
    probes = await probe_capabilities(body.base_url.rstrip("/"), body.model_name, api_key)
    # Re-probing a saved model with its saved config counts as "tested".
    if (
        stored is not None
        and stored.base_url == body.base_url.rstrip("/")
        and stored.model_name == body.model_name
    ):
        passed = [k for k, (ok, _) in probes.items() if ok]
        stored.last_tested_at = datetime.now(timezone.utc)
        stored.last_test_detail = (
            f"responds: {', '.join(passed)}" if passed else probes["chat"][1]
        )
        await session.commit()
    return ModelTestOut(
        **{k: CapabilityProbe(ok=ok, detail=detail) for k, (ok, detail) in probes.items()}
    )


@router.post("/models", response_model=ModelOut, status_code=status.HTTP_201_CREATED)
async def create_model(
    body: ModelIn, session: AsyncSession = Depends(get_session)
) -> ModelOut:
    existing = (
        await session.execute(select(ConnectedModel).where(ConnectedModel.name == body.name))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"A model named '{body.name}' already exists")
    m = ConnectedModel(
        name=body.name,
        base_url=body.base_url.rstrip("/"),
        model_name=body.model_name,
        api_key_encrypted=encrypt(body.api_key) if body.api_key else None,
        capabilities=body.capabilities,
        last_tested_at=datetime.now(timezone.utc),
    )
    session.add(m)
    await session.commit()
    await session.refresh(m)  # load server-side defaults (updated_at)
    return _out(m, [])


@router.put("/models/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: int, body: ModelIn, session: AsyncSession = Depends(get_session)
) -> ModelOut:
    m = await session.get(ConnectedModel, model_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    m.name = body.name
    m.base_url = body.base_url.rstrip("/")
    m.model_name = body.model_name
    m.capabilities = body.capabilities
    if body.api_key is not None:
        m.api_key_encrypted = encrypt(body.api_key) if body.api_key else None
    await session.commit()
    await session.refresh(m)
    return _out(m, (await _assigned_roles(session)).get(m.id, []))


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(model_id: int, session: AsyncSession = Depends(get_session)) -> None:
    m = await session.get(ConnectedModel, model_id)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Model not found")
    roles = (await _assigned_roles(session)).get(model_id, [])
    if roles:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Model is assigned to role(s): {', '.join(sorted(roles))}. Unassign first.",
        )
    await session.delete(m)
    await session.commit()


@router.get("/model-roles", response_model=list[RoleAssignmentOut])
async def list_role_assignments(
    session: AsyncSession = Depends(get_session),
) -> list[RoleAssignmentOut]:
    assignments = {
        a.role: a for a in (await session.execute(select(ModelRoleAssignment))).scalars()
    }
    models = {
        m.id: m for m in (await session.execute(select(ConnectedModel))).scalars()
    }
    out = []
    for role in MODEL_ROLES:
        a = assignments.get(role)
        model = models.get(a.model_id) if a else None
        required = ROLE_REQUIRED_CAPABILITY[role]
        out.append(
            RoleAssignmentOut(
                role=role,
                model_id=model.id if model else None,
                model_name=model.name if model else None,
                required_capability=required,
                capability_ok=model is None or bool((model.capabilities or {}).get(required)),
                reasoning_effort=a.reasoning_effort if a else None,
            )
        )
    return out


@router.put("/model-roles/{role}", response_model=RoleAssignmentOut)
async def assign_role(
    role: str, body: RoleAssignmentIn, session: AsyncSession = Depends(get_session)
) -> RoleAssignmentOut:
    if role not in MODEL_ROLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown role '{role}'")
    model = await session.get(ConnectedModel, body.model_id)
    if model is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown model id {body.model_id}")
    effort = (body.reasoning_effort or "").strip() or None
    if effort is not None:
        # There is no discovery API for supported levels anywhere in the
        # OpenAI-compatible ecosystem — the only truth is a live probe. A
        # rejected level never saves, so a broken assignment can't exist.
        ok, detail = await probe_reasoning(model, effort)
        if not ok:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)
    assignment = await session.get(ModelRoleAssignment, role)
    if assignment is None:
        session.add(ModelRoleAssignment(role=role, model_id=model.id, reasoning_effort=effort))
    else:
        assignment.model_id = model.id
        assignment.reasoning_effort = effort

    # Media that was skipped for lack of a model gets another chance now.
    kinds = ROLE_ATTACHMENT_KINDS.get(role)
    if kinds:
        await session.execute(
            update(MessageAttachment)
            .where(MessageAttachment.status == "skipped", MessageAttachment.kind.in_(kinds))
            .values(status="pending", attempts=0, error=None)
        )
    await session.commit()
    required = ROLE_REQUIRED_CAPABILITY[role]
    return RoleAssignmentOut(
        role=role,
        model_id=model.id,
        model_name=model.name,
        required_capability=required,
        capability_ok=bool((model.capabilities or {}).get(required)),
        reasoning_effort=effort,
    )


@router.delete("/model-roles/{role}", status_code=status.HTTP_204_NO_CONTENT)
async def unassign_role(role: str, session: AsyncSession = Depends(get_session)) -> None:
    assignment = await session.get(ModelRoleAssignment, role)
    if assignment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Role is not assigned")
    await session.delete(assignment)
    await session.commit()
