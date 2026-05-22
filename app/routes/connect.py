import logging

from fastapi import APIRouter, HTTPException, Request

from app import channel_connections
from app.config import get_settings
from app.security import is_authenticated
from app.tenants import get_tenant
from app.zernio import ZernioAPIError, ZernioNotConfigured, ZernioService


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/api", tags=["internal-api"])


def _require_operator_json(request: Request) -> None:
    settings = get_settings()
    if not is_authenticated(request, settings):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def _whatsapp_callback_url() -> str:
    settings = get_settings()
    return f"{settings.unboks_admin_api_url}/connect/whatsapp/callback"


@router.post("/tenants/{tenant_id}/channels/whatsapp/connect/start")
def start_whatsapp_connection(tenant_id: str, request: Request) -> dict:
    """Generate a client-facing Zernio/Meta authorization URL.

    The operator is authenticated by the existing Nr3 admin session cookie.
    The Zernio API key is used only server-side.
    """
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    service = ZernioService()
    try:
        zernio_profile_id = channel_connections.get_tenant_zernio_profile_id(
            tenant.id
        )
        if not zernio_profile_id:
            profile = service.create_profile(
                name=tenant.name,
                description=f"Unboks tenant workspace: {tenant.id}",
            )
            zernio_profile_id = profile.id
            channel_connections.set_tenant_zernio_profile_id(
                tenant_id=tenant.id,
                name=tenant.name,
                zernio_profile_id=zernio_profile_id,
                status=tenant.status,
            )

        connect_url = service.get_connect_url(
            platform="whatsapp",
            profile_id=zernio_profile_id,
            redirect_url=_whatsapp_callback_url(),
        )
        if not connect_url.state:
            raise ZernioAPIError(502, "Zernio did not return a callback state.")

        created = channel_connections.create_connection_request(
            tenant_id=tenant.id,
            auth_url=connect_url.auth_url,
            zernio_profile_id=zernio_profile_id,
            state_token=connect_url.state,
            status="link_generated",
        )
        logger.info(
            "whatsapp_connect_link_generated tenant=%s request_id=%s",
            tenant.id,
            created.request.id,
        )
    except ZernioNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ZernioAPIError as exc:
        logger.warning(
            "whatsapp_connect_link_failed tenant=%s status=%s error=%s",
            tenant.id,
            exc.status_code,
            exc.message,
        )
        raise HTTPException(status_code=502, detail=exc.message) from exc

    return {
        "success": True,
        "tenantId": tenant.id,
        "authUrl": created.request.auth_url,
        "status": created.request.status,
        "expiresAt": created.request.state_token_expires_at,
        "requestId": created.request.id,
    }
