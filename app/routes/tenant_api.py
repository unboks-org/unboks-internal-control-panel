from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import audit_log
from app.config import get_settings
from app.provisioning import queue_tenant_host_action
from app.security import is_authenticated
from app.tenants import RESERVED_SLUGS, forget_tenant_state, get_tenant


router = APIRouter(prefix="/internal/api", tags=["internal-api"])

DELETE_CONFIRMATION = "DELETE FOREVER"
_DELETE_ATTEMPTS: dict[str, list[float]] = {}


class TenantDeleteRequest(BaseModel):
    typedSlug: str
    finalConfirmation: str


def _require_super_admin(request: Request) -> None:
    settings = get_settings()
    if not is_authenticated(request, settings):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def _check_delete_rate_limit(key: str) -> None:
    now = time.monotonic()
    window_start = now - 60
    attempts = [
        stamp
        for stamp in _DELETE_ATTEMPTS.get(key, [])
        if stamp >= window_start
    ]
    if len(attempts) >= 5:
        _DELETE_ATTEMPTS[key] = attempts
        raise HTTPException(
            status_code=429,
            detail="Too many delete attempts. Wait one minute and try again.",
        )
    attempts.append(now)
    _DELETE_ATTEMPTS[key] = attempts


@router.delete("/tenants/{tenant_id}")
def delete_tenant_forever(
    tenant_id: str,
    body: TenantDeleteRequest,
    request: Request,
) -> dict:
    """Queue a privileged host-side permanent tenant delete.

    The FastAPI container does not run Docker/nginx deletes directly. It
    validates the two confirmations, then asks the root host worker to back up
    and delete the tenant.
    """
    _require_super_admin(request)
    _check_delete_rate_limit(f"{request.client.host if request.client else 'unknown'}:{tenant_id}")

    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    if tenant.id in RESERVED_SLUGS:
        raise HTTPException(
            status_code=403,
            detail="The master Unboks tenant cannot be deleted.",
        )
    if body.typedSlug != tenant.id:
        raise HTTPException(
            status_code=400,
            detail="Typed tenant slug does not match exactly.",
        )
    if body.finalConfirmation != DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail="Final confirmation text is invalid.",
        )

    result = queue_tenant_host_action(
        slug=tenant.id,
        action="delete_tenant",
        dashboard_url=f"https://dashboard.unboks.org/{tenant.id}",
        typed_slug=body.typedSlug,
        final_confirmation=body.finalConfirmation,
    )
    if result.status == "disabled":
        raise HTTPException(
            status_code=503,
            detail="Tenant delete worker is disabled. No tenant was deleted.",
        )
    if result.status == "failed":
        audit_log.record_event(
            tenant_id=tenant.id,
            action="tenant.delete_failed",
            result="failed",
            safe_summary=result.message,
            metadata={"job_id": result.job_id},
        )
        raise HTTPException(status_code=502, detail=result.message)
    if result.status == "succeeded":
        forget_tenant_state(tenant.id)
        audit_log.record_event(
            tenant_id=tenant.id,
            action="tenant.deleted",
            result="ok",
            safe_summary="Tenant permanently deleted after backup.",
            metadata={"job_id": result.job_id},
        )
        return {
            "success": True,
            "status": "deleted",
            "tenantId": tenant.id,
            "message": result.message,
            "details": list(result.details),
            "jobId": result.job_id,
        }

    return {
        "success": False,
        "status": result.status,
        "tenantId": tenant.id,
        "message": result.message,
        "details": list(result.details),
        "jobId": result.job_id,
    }
