import hmac
import time
from urllib.parse import urlparse
from typing import Optional

from itsdangerous import BadSignature, URLSafeSerializer
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from app.config import Settings


SESSION_COOKIE = "nr3_admin_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}


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


def _is_admin_state_change(request: Request) -> bool:
    if request.method.upper() in SAFE_METHODS:
        return False
    path = request.url.path
    if path == "/login":
        return False
    if path == "/logout" or path.startswith("/admin/"):
        return True
    if path.startswith("/internal/api/tenants/"):
        return True
    return False


def _allowed_csrf_hosts(request: Request, settings: Settings) -> set[str]:
    hosts = {request.url.hostname or ""}
    configured = urlparse(settings.base_url)
    if configured.hostname:
        hosts.add(configured.hostname)
    forwarded_host = request.headers.get("x-forwarded-host")
    if forwarded_host:
        hosts.add(forwarded_host.split(",", 1)[0].strip().split(":", 1)[0])
    host = request.headers.get("host")
    if host:
        hosts.add(host.split(":", 1)[0])
    return {host.lower() for host in hosts if host}


def _request_source_host(request: Request) -> str:
    origin = request.headers.get("origin")
    if origin:
        return (urlparse(origin).hostname or "").lower()
    referer = request.headers.get("referer")
    if referer:
        return (urlparse(referer).hostname or "").lower()
    return ""


def csrf_protect_admin_request(
    request: Request,
    settings: Settings,
) -> Optional[PlainTextResponse]:
    """Reject cross-site admin mutations in production.

    Nr3 uses cookie sessions and normal HTML forms. A production-only
    Origin/Referer gate protects state-changing admin routes without changing
    the existing form and file-upload paths.
    """
    if settings.env != "production" or not _is_admin_state_change(request):
        return None
    source_host = _request_source_host(request)
    if source_host and source_host in _allowed_csrf_hosts(request, settings):
        return None
    return PlainTextResponse("CSRF validation failed.", status_code=403)
