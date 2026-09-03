import hmac
import logging
import os
from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, StrictBool

from app.config import get_settings
from app.icp_overrides import (
    add_sot_entry,
    effective_state_envelope,
    set_ai_tone,
    set_feature_toggle,
)
from app.tenants import tenant_slug_exists_for_creation, validate_slug


router = APIRouter(prefix="/internal", tags=["internal"])
logger = logging.getLogger(__name__)


class AgentStyleWrite(BaseModel):
    tone: str = ""
    notes: str = ""


class SotEntryWrite(BaseModel):
    id: str = ""
    title: str
    content: str
    category: str = "general"


class FeatureToggleWrite(BaseModel):
    value: StrictBool


WRITABLE_TENANT_FEATURE_TOGGLES = frozenset({"ai_auto_reply"})


def _require_internal_bridge(
    tenant_id: str,
    authorization: str,
    x_tenant_identity: Optional[str],
) -> None:
    settings = get_settings()
    identity = (x_tenant_identity or "").strip()
    if not identity:
        logger.warning("bridge_auth.reject tenant=%s reason=missing_identity", tenant_id)
        raise HTTPException(status_code=403, detail="Tenant identity is required")
    if identity != tenant_id:
        logger.warning(
            "bridge_auth.reject tenant=%s identity=%s reason=identity_mismatch",
            tenant_id,
            identity,
        )
        raise HTTPException(status_code=403, detail="Tenant identity mismatch")
    expected = _tenant_bridge_token(tenant_id, settings)
    legacy_allowed = False
    if expected is None and settings.allow_legacy_shared_bridge_token:
        expected = settings.internal_api_token
        legacy_allowed = True
    if not expected:
        logger.warning("bridge_auth.reject tenant=%s reason=tenant_token_missing", tenant_id)
        raise HTTPException(
            status_code=503,
            detail="Tenant bridge token is not configured",
        )
    if not authorization.startswith("Bearer "):
        logger.warning("bridge_auth.reject tenant=%s reason=missing_bearer", tenant_id)
        raise HTTPException(status_code=401, detail="Missing bridge token")
    candidate = authorization[7:].strip()
    if not hmac.compare_digest(candidate, expected):
        logger.warning("bridge_auth.reject tenant=%s reason=invalid_token", tenant_id)
        raise HTTPException(status_code=401, detail="Invalid bridge token")
    if legacy_allowed:
        logger.warning("bridge_auth.legacy_shared_token tenant=%s", tenant_id)


def _tenant_bridge_token(tenant_id: str, settings) -> Optional[str]:
    base_dir = settings.tenant_bridge_token_dir
    if not base_dir:
        return None
    safe_name = tenant_id.strip()
    if not safe_name or "/" in safe_name or "\\" in safe_name or safe_name.startswith("."):
        return None
    path = os.path.join(base_dir, safe_name)
    try:
        token = open(path, encoding="utf-8").read().strip()
    except OSError:
        return None
    return token if len(token) >= 32 else None


@contextmanager
def _authorized_bridge_lease(
    tenant_id: str,
    authorization: str,
    x_tenant_identity: Optional[str],
    *,
    read_only: bool = False,
):
    """Keep bridge authentication and tenant access on one generation.

    Without this lease, a request authenticated with generation A's token can
    pause after authentication and resume after the same slug is recreated as
    generation B. Holding the lifecycle lock through the read or mutation
    makes token rotation and generation ownership one atomic authorization
    decision.
    """
    try:
        safe_tenant_id = validate_slug(tenant_id)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Tenant not found") from exc
    if safe_tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant identity mismatch")

    from app.delete_operations import require_tenant_mutation_generation
    from app.provisioning import tenant_creation_lock, tenant_read_lock

    lease = tenant_read_lock if read_only else tenant_creation_lock
    with lease(safe_tenant_id):
        _require_internal_bridge(
            safe_tenant_id,
            authorization,
            x_tenant_identity,
        )
        if not tenant_slug_exists_for_creation(safe_tenant_id):
            raise HTTPException(status_code=404, detail="Tenant not found")
        try:
            generation_id = require_tenant_mutation_generation(safe_tenant_id)
        except Exception as exc:
            raise HTTPException(
                status_code=409,
                detail="Tenant generation changed; retry with the current bridge token.",
            ) from exc
        yield generation_id


@router.get("/tenants/{tenant_id}/overrides")
def read_tenant_overrides(
    tenant_id: str,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    with _authorized_bridge_lease(
        tenant_id, authorization, x_tenant_identity, read_only=True
    ):
        return effective_state_envelope(tenant_id)


@router.put("/tenants/{tenant_id}/feature-toggles/{feature_key}")
def write_tenant_feature_toggle(
    tenant_id: str,
    feature_key: str,
    payload: FeatureToggleWrite,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    """Let Nr2 persist only the tenant-scoped auto-reply control."""
    with _authorized_bridge_lease(
        tenant_id, authorization, x_tenant_identity
    ) as generation_id:
        if feature_key not in WRITABLE_TENANT_FEATURE_TOGGLES:
            raise HTTPException(
                status_code=404, detail="Feature toggle is not writable"
            )
        set_feature_toggle(
            tenant_id,
            feature_key,
            payload.value,
            updated_by="nr2-dashboard",
            expected_generation_id=generation_id,
        )
        toggle = effective_state_envelope(tenant_id)["feature_toggles"][feature_key]
        return {
            "ok": True,
            "tenant_id": tenant_id,
            "feature_key": feature_key,
            "feature_toggle": toggle,
        }


@router.put("/tenants/{tenant_id}/agent-style")
def write_tenant_agent_style(
    tenant_id: str,
    payload: AgentStyleWrite,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    with _authorized_bridge_lease(
        tenant_id, authorization, x_tenant_identity
    ) as generation_id:
        set_ai_tone(
            tenant_id,
            payload.tone,
            notes=payload.notes,
            updated_by="nr2-dashboard",
            expected_generation_id=generation_id,
        )
        return {"ok": True, "tenant_id": tenant_id}


@router.post("/tenants/{tenant_id}/sot")
def write_tenant_sot_entry(
    tenant_id: str,
    payload: SotEntryWrite,
    authorization: str = Header(default=""),
    x_tenant_identity: Optional[str] = Header(default=None),
) -> dict:
    with _authorized_bridge_lease(
        tenant_id, authorization, x_tenant_identity
    ) as generation_id:
        try:
            entry = add_sot_entry(
                tenant_id,
                entry_id=payload.id,
                title=payload.title,
                content=payload.content,
                category=payload.category,
                updated_by="nr2-dashboard",
                expected_generation_id=generation_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "tenant_id": tenant_id, "entry": entry}
