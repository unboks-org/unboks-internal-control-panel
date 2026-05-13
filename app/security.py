import hmac
import time
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.config import Settings


SESSION_COOKIE = "nr3_admin_session"


def _serializer(settings: Settings) -> URLSafeSerializer:
    return URLSafeSerializer(settings.session_secret, salt="nr3-admin-session")


def verify_admin_password(candidate: str, settings: Settings) -> bool:
    if not settings.admin_password:
        return False
    return hmac.compare_digest(candidate, settings.admin_password)


def create_session_value(settings: Settings) -> str:
    payload = {"role": "admin", "iat": int(time.time())}
    return _serializer(settings).dumps(payload)


def is_authenticated(request: Request, settings: Settings) -> bool:
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return False
    try:
        payload = _serializer(settings).loads(raw)
    except BadSignature:
        return False
    if payload.get("role") != "admin":
        return False
    issued_at = int(payload.get("iat", 0))
    return int(time.time()) - issued_at <= settings.session_max_age_seconds


def require_admin(request: Request, settings: Settings) -> Optional[RedirectResponse]:
    if is_authenticated(request, settings):
        return None
    return RedirectResponse(url="/login", status_code=303)


def set_session_cookie(response: Response, value: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=settings.env == "production",
        samesite="lax",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)
