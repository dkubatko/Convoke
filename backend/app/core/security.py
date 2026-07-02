import hmac

from fastapi import Cookie, Depends, HTTPException, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import Settings, get_settings

SESSION_COOKIE = "convoke_session"


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt="operator-session")


def verify_password(candidate: str, settings: Settings) -> bool:
    return hmac.compare_digest(candidate.encode(), settings.operator_password.encode())


def issue_session(response: Response, settings: Settings) -> None:
    token = _serializer(settings).dumps({"role": "operator"})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)


def require_operator(
    session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    settings: Settings = Depends(get_settings),
) -> None:
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        _serializer(settings).loads(session, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
