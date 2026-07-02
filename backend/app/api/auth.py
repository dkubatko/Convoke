from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.security import clear_session, issue_session, require_operator, verify_password

router = APIRouter()


class LoginRequest(BaseModel):
    password: str


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict:
    if not verify_password(body.password, settings):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")
    issue_session(response, settings)
    return {"ok": True}


@router.post("/logout")
async def logout(response: Response) -> dict:
    clear_session(response)
    return {"ok": True}


@router.get("/me", dependencies=[Depends(require_operator)])
async def me() -> dict:
    return {"role": "operator"}
