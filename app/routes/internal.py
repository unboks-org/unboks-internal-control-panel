import hmac
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from app.config import get_settings
from app.icp_overrides import effective_state_envelope


router = APIRouter(prefix="/internal", tags=["internal"])


def _require_internal_bridge(
    tenant_id: str,
    authorization: str,
    x_tenant_identity: Optional[str],
) -> None:
    settings = get_settings()
    expected = settings.internal_api_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="NR3 internal bridge token is not configured",
        )
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bridge token")
    candidate = authorization[7:].strip()
    if not hmac.compare_digest(candidate, expected):
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    if x_tenant_identity and x_tenant_identity.strip() != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant identity mismatch")


@router.get("/tenants/{tenant_id}/overrides")
def read_tenant_overrides(
    tenant_id: str,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    _require_internal_bridge(tenant_id, authorization, x_tenant_identity)
    return effective_state_envelope(tenant_id)
