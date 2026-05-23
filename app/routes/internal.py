import hmac
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app.config import get_settings
from app.icp_overrides import add_sot_entry, effective_state_envelope, set_ai_tone


router = APIRouter(prefix="/internal", tags=["internal"])


class AgentStyleWrite(BaseModel):
    tone: str = ""
    notes: str = ""


class SotEntryWrite(BaseModel):
    id: str = ""
    title: str
    content: str
    category: str = "general"


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


@router.put("/tenants/{tenant_id}/agent-style")
def write_tenant_agent_style(
    tenant_id: str,
    payload: AgentStyleWrite,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    _require_internal_bridge(tenant_id, authorization, x_tenant_identity)
    set_ai_tone(
        tenant_id,
        payload.tone,
        notes=payload.notes,
        updated_by="nr2-dashboard",
    )
    return {"ok": True, "tenant_id": tenant_id}


@router.post("/tenants/{tenant_id}/sot")
def write_tenant_sot_entry(
    tenant_id: str,
    payload: SotEntryWrite,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    _require_internal_bridge(tenant_id, authorization, x_tenant_identity)
    try:
        entry = add_sot_entry(
            tenant_id,
            entry_id=payload.id,
            title=payload.title,
            content=payload.content,
            category=payload.category,
            updated_by="nr2-dashboard",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "tenant_id": tenant_id, "entry": entry}
