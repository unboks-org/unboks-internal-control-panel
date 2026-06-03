from __future__ import annotations

import os
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import audit_log
from app import channel_connections
from app.config import get_settings
from app.provisioning import queue_tenant_host_action
from app.security import is_authenticated
from app.tenants import (
    RESERVED_SLUGS,
    forget_tenant_state,
    get_tenant,
    get_tenant_client_data,
)
from app.zernio import ZernioAPIError, ZernioNotConfigured, ZernioService


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


def _host_worker_enabled() -> bool:
    return os.getenv("NR3_AUTO_PROVISION", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _add_unique(target: list[str], value: Any) -> None:
    if value is None:
        return
    text = str(value).strip()
    if text and text not in target:
        target.append(text)


def _collect_provider_ids_from_client_json(tenant_id: str) -> dict[str, list[str]]:
    data = get_tenant_client_data(tenant_id)
    account_ids: list[str] = []
    profile_ids: list[str] = []
    if not data:
        return {"account_ids": account_ids, "profile_ids": profile_ids}

    allowlist = data.get("channel_account_allowlist")
    if isinstance(allowlist, dict):
        raw_accounts = allowlist.get("zernio_accounts")
        if isinstance(raw_accounts, list):
            for item in raw_accounts:
                _add_unique(account_ids, item)

    for key in (
        "zernio_account_id",
        "whatsapp_provider_account_id",
        "provider_account_id",
    ):
        _add_unique(account_ids, data.get(key))
    for key in ("zernio_profile_id", "profile_id"):
        _add_unique(profile_ids, data.get(key))
    return {"account_ids": account_ids, "profile_ids": profile_ids}


def _collect_tenant_zernio_cleanup_ids(tenant_id: str) -> dict[str, list[str]]:
    db_ids = channel_connections.list_tenant_zernio_ids(tenant_id)
    client_ids = _collect_provider_ids_from_client_json(tenant_id)
    account_ids: list[str] = []
    profile_ids: list[str] = []
    for value in db_ids.get("account_ids", []) + client_ids.get("account_ids", []):
        _add_unique(account_ids, value)
    for value in db_ids.get("profile_ids", []) + client_ids.get("profile_ids", []):
        _add_unique(profile_ids, value)
    return {"account_ids": account_ids, "profile_ids": profile_ids}


def _cleanup_zernio_before_tenant_delete(tenant_id: str) -> list[str]:
    ids = _collect_tenant_zernio_cleanup_ids(tenant_id)
    account_ids = ids["account_ids"]
    profile_ids = ids["profile_ids"]
    if not account_ids and not profile_ids:
        return ["zernio cleanup skipped: no zernio account/profile ids found"]

    service = ZernioService()
    details: list[str] = []
    try:
        for account_id in account_ids:
            try:
                service.delete_account(account_id)
                details.append(f"zernio account disconnected: {account_id}")
            except ZernioAPIError as exc:
                if exc.status_code == 404:
                    details.append(f"zernio account already absent: {account_id}")
                    continue
                raise
        for profile_id in profile_ids:
            try:
                service.delete_profile(profile_id)
                details.append(f"zernio profile deleted: {profile_id}")
            except ZernioAPIError as exc:
                if exc.status_code == 404:
                    details.append(f"zernio profile already absent: {profile_id}")
                    continue
                raise
    except ZernioNotConfigured as exc:
        audit_log.record_event(
            tenant_id=tenant_id,
            action="tenant.zernio_cleanup_failed",
            result="failed",
            safe_summary="Tenant delete blocked because Zernio is not configured.",
            metadata={
                "zernio_account_count": len(account_ids),
                "zernio_profile_count": len(profile_ids),
            },
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "Tenant has Zernio provider state, but Zernio is not configured. "
                "Delete was blocked so the external account is not orphaned."
            ),
        ) from exc
    except ZernioAPIError as exc:
        audit_log.record_event(
            tenant_id=tenant_id,
            action="tenant.zernio_cleanup_failed",
            result="failed",
            safe_summary="Tenant delete blocked because Zernio cleanup failed.",
            metadata={
                "zernio_account_count": len(account_ids),
                "zernio_profile_count": len(profile_ids),
                "status_code": exc.status_code,
            },
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Zernio cleanup failed, so tenant deletion was blocked. "
                "Retry after fixing the provider connection."
            ),
        ) from exc

    audit_log.record_event(
        tenant_id=tenant_id,
        action="tenant.zernio_cleanup_completed",
        result="ok",
        safe_summary="Zernio provider accounts/profiles cleaned before tenant delete.",
        metadata={
            "zernio_account_count": len(account_ids),
            "zernio_profile_count": len(profile_ids),
        },
    )
    return details


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

    if not _host_worker_enabled():
        raise HTTPException(
            status_code=503,
            detail="Tenant delete worker is disabled. No tenant was deleted.",
        )

    zernio_cleanup_details = _cleanup_zernio_before_tenant_delete(tenant.id)

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
            "details": zernio_cleanup_details + list(result.details),
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
