from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import audit_log
from app import channel_connections
from app.config import get_settings
from app.delete_operations import (
    DeleteOperationConflict,
    DeleteOperationError,
    load_delete_operation,
    read_tenant_generation,
    require_tenant_mutation_generation,
    retry_delete_phase,
    start_delete_operation,
    update_delete_operation,
)
from app.provisioning import (
    host_action_job_is_active,
    queue_tenant_host_action,
    read_host_action_result,
    tenant_creation_lock,
    tenant_provision_claim,
)
from app.security import is_authenticated
from app.tenants import (
    RESERVED_SLUGS,
    forget_tenant_state_strict,
    get_tenant,
)
from app.zernio import ZernioAPIError, ZernioNotConfigured, ZernioService


router = APIRouter(prefix="/internal/api", tags=["internal-api"])

DELETE_CONFIRMATION = "DELETE FOREVER"
_DELETE_ATTEMPTS: dict[str, list[float]] = {}


def _runtime_slug_is_lexically_absent(root_value: str, slug: str) -> bool:
    """Prove the mounted client root exists and has no entry for ``slug``."""
    if not root_value:
        return False
    root = Path(root_value)
    return root.is_dir() and not os.path.lexists(root / slug)


class TenantDeleteRequest(BaseModel):
    typedSlug: str
    finalConfirmation: str
    tenantGenerationId: str


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


def _collect_provider_ids_from_client_json(data: dict[str, Any]) -> dict[str, list[str]]:
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
    # Profile ids are intentionally not trusted from tenant-writable runtime
    # JSON. Nr3's provider records are the deletion authority for profiles.
    return {"account_ids": account_ids, "profile_ids": profile_ids}


def _collect_tenant_zernio_cleanup_ids(
    tenant_id: str,
    client_data: dict[str, Any],
) -> dict[str, list[str]]:
    db_ids = channel_connections.list_tenant_zernio_ids(tenant_id)
    client_ids = _collect_provider_ids_from_client_json(client_data)
    account_ids: list[str] = []
    profile_ids: list[str] = []
    for value in db_ids.get("account_ids", []) + client_ids.get("account_ids", []):
        _add_unique(account_ids, value)
    for value in db_ids.get("profile_ids", []) + client_ids.get("profile_ids", []):
        _add_unique(profile_ids, value)
    return {"account_ids": account_ids, "profile_ids": profile_ids}


def _cleanup_zernio_before_tenant_delete(
    tenant_id: str,
    *,
    account_ids: list[str],
    profile_ids: list[str],
) -> list[str]:
    account_ids = list(dict.fromkeys(account_ids))
    profile_ids = list(dict.fromkeys(profile_ids))
    if not account_ids and not profile_ids:
        return ["zernio cleanup skipped: no zernio account/profile ids found"]

    service = ZernioService()
    details: list[str] = []
    try:
        # A provider callback can create an account immediately before the
        # durable delete claim blocks its local attachment. Discover every
        # current WhatsApp account under this tenant's verified profile so the
        # external profile cannot retain an orphaned account.
        if profile_ids:
            discovered_accounts = service.list_accounts(platform="whatsapp")
            for account in discovered_accounts:
                if account.profile_id in profile_ids:
                    _add_unique(account_ids, account.id)
        for account_id in account_ids:
            if channel_connections.provider_id_owned_by_other_tenant(
                tenant_id=tenant_id,
                zernio_account_id=account_id,
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A recorded Zernio account is currently owned by another "
                        "tenant. Provider cleanup and deletion were blocked."
                    ),
                )
        for profile_id in profile_ids:
            if channel_connections.provider_id_owned_by_other_tenant(
                tenant_id=tenant_id,
                zernio_profile_id=profile_id,
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "A recorded Zernio profile is currently owned by another "
                        "tenant. Provider cleanup and deletion were blocked."
                    ),
                )
        verified_account_ids: list[str] = []
        for account_id in account_ids:
            try:
                account = service.get_account(account_id)
            except ZernioAPIError as exc:
                if exc.status_code == 404:
                    details.append(f"zernio account already absent: {account_id}")
                    continue
                raise
            if not account.profile_id or account.profile_id not in profile_ids:
                audit_log.record_event(
                    tenant_id=tenant_id,
                    action="tenant.zernio_cleanup_ownership_rejected",
                    result="blocked",
                    safe_summary=(
                        "Tenant delete blocked because a provider account did "
                        "not belong to the tenant's verified Zernio profile."
                    ),
                    metadata={"account_id": account_id},
                )
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Zernio account ownership could not be verified for this "
                        "tenant. No provider account was deleted."
                    ),
                )
            verified_account_ids.append(account_id)

        for account_id in verified_account_ids:
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
    channel_connections.clear_tenant_orphan_profile_ids(tenant_id, profile_ids)
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

    if body.typedSlug != tenant_id:
        raise HTTPException(
            status_code=400,
            detail="Typed tenant slug does not match exactly.",
        )
    if body.finalConfirmation != DELETE_CONFIRMATION:
        raise HTTPException(
            status_code=400,
            detail="Final confirmation text is invalid.",
        )
    if tenant_id in RESERVED_SLUGS:
        raise HTTPException(
            status_code=403,
            detail="The master Unboks tenant cannot be deleted.",
        )
    tenant = get_tenant(tenant_id)
    if tenant is None:
        try:
            with tenant_creation_lock(tenant_id):
                completed = load_delete_operation(tenant_id)
                if (
                    completed is not None
                    and completed["tenant_generation_id"]
                    != body.tenantGenerationId.strip()
                ):
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Tenant generation changed; this delete confirmation "
                            "is stale and no mutation was attempted."
                        ),
                    )
                root_value = os.getenv("NR3_TENANTS_CLIENT_DIR", "").strip()
                if completed is not None and completed.get("phase") != "deleted":
                    job_id = (
                        completed.get("delete_job_id")
                        if str(completed.get("phase") or "").startswith("delete")
                        else completed.get("prepare_job_id")
                    )
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Tenant deletion is quarantined and retryable: "
                            f"operation={completed['operation_id']} job={job_id or 'pending'} "
                            f"phase={completed['phase']}. Retry the exact confirmed delete."
                        ),
                    )
                if completed is not None and completed.get("phase") == "deleted":
                    if (
                        not _runtime_slug_is_lexically_absent(root_value, tenant_id)
                        or tenant_provision_claim(tenant_id) is not None
                    ):
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "A completed delete tombstone exists, but runtime "
                                "absence cannot be proved. The slug remains quarantined."
                            ),
                        )
                    return {
                        "success": True,
                        "status": "deleted",
                        "tenantId": tenant_id,
                        "message": "This tenant generation was already deleted.",
                        "details": list(completed["provider_details"])
                        + list(completed["delete_details"]),
                        "jobId": completed["delete_job_id"],
                        "operationId": completed["operation_id"],
                    }
        except HTTPException:
            raise
        except (DeleteOperationError, ValueError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Tenant lifecycle state is unavailable; slug is quarantined: {exc}",
            ) from exc
        raise HTTPException(status_code=404, detail="Tenant not found.")

    if not _host_worker_enabled():
        raise HTTPException(
            status_code=503,
            detail="Tenant delete worker is disabled. No tenant was deleted.",
        )

    dashboard_url = f"https://dashboard.unboks.org/{tenant.id}"
    try:
        with tenant_creation_lock(tenant.id):
            operation = load_delete_operation(tenant.id)
            if operation is None:
                confirmed_generation_id = require_tenant_mutation_generation(
                    tenant.id,
                    expected_generation_id=body.tenantGenerationId,
                )
                client_data, generation_fingerprint = read_tenant_generation(
                    tenant.id
                )
                cleanup_ids = _collect_tenant_zernio_cleanup_ids(
                    tenant.id, client_data
                )
                # Persist the complete intent while provider mutations hold the
                # same lifecycle lock, before any host/provider side effect.
                operation = start_delete_operation(
                    slug=tenant.id,
                    tenant_generation_id=confirmed_generation_id,
                    generation_fingerprint=generation_fingerprint,
                    account_ids=cleanup_ids["account_ids"],
                    profile_ids=cleanup_ids["profile_ids"],
                )
            elif (
                operation["tenant_generation_id"]
                != body.tenantGenerationId.strip()
            ):
                raise DeleteOperationConflict(
                    "Tenant generation changed; this delete confirmation is stale "
                    "and no mutation was attempted."
                )
            elif operation["phase"] == "deleted":
                raise DeleteOperationConflict(
                    "A generation already proven deleted is visible in the runtime "
                    "again. It was quarantined for manual recovery."
                )

        # Before provider cleanup starts, the mounted runtime must still be the
        # exact generation for which the operator confirmed deletion.
        if operation["phase"] in {
            "preparing",
            "backup_queued",
            "backup_failed",
            "prepared",
            "provider_cleanup",
            "provider_cleanup_failed",
        }:
            _, current_fingerprint = read_tenant_generation(tenant.id)
            if current_fingerprint != operation["generation_fingerprint"]:
                raise DeleteOperationConflict(
                    "Tenant generation changed after delete confirmation. "
                    "Provider and runtime deletion were blocked."
                )
    except DeleteOperationConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeleteOperationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    operation_id = str(operation["operation_id"])
    generation_fingerprint = str(operation["generation_fingerprint"])

    if operation["phase"] == "backup_failed":
        operation = retry_delete_phase(
            slug=tenant.id,
            operation_id=operation_id,
            action="prepare_delete_tenant",
        )
    if operation["phase"] == "preparing":
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"preparing"},
            phase="backup_queued",
        )

    if operation["phase"] == "backup_queued":
        prepare_job_id = str(operation["prepare_job_id"])
        preparation = read_host_action_result(
            job_id=prepare_job_id,
            slug=tenant.id,
            action="prepare_delete_tenant",
            delete_operation_id=operation_id,
        )
        if preparation is None:
            preparation = queue_tenant_host_action(
                slug=tenant.id,
                action="prepare_delete_tenant",
                dashboard_url=dashboard_url,
                typed_slug=body.typedSlug,
                final_confirmation=body.finalConfirmation,
                requested_job_id=prepare_job_id,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
            )
        if preparation.status == "disabled":
            raise HTTPException(
                status_code=503,
                detail="Tenant delete worker is disabled. No tenant was deleted.",
            )
        if preparation.status == "failed":
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"backup_queued"},
                phase="backup_failed",
                last_error=preparation.message,
            )
            audit_log.record_event(
                tenant_id=tenant.id,
                action="tenant.delete_backup_failed",
                result="failed",
                safe_summary=preparation.message,
                metadata={
                    "job_id": prepare_job_id,
                    "delete_operation_id": operation_id,
                },
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Tenant backup could not be verified, so Zernio and the "
                    f"runtime were left unchanged: {preparation.message}"
                ),
            )
        prepare_active = host_action_job_is_active(
            job_id=prepare_job_id,
            slug=tenant.id,
            action="prepare_delete_tenant",
        )
        if preparation.status != "succeeded" or prepare_active:
            return {
                "success": False,
                "status": "backup_pending",
                "tenantId": tenant.id,
                "message": (
                    "Tenant backup is still running. Zernio and the runtime "
                    "remain unchanged; retry this confirmed delete shortly."
                ),
                "details": list(preparation.details),
                "jobId": prepare_job_id,
                "operationId": operation_id,
            }
        if (
            preparation.operation_id != operation_id
            or preparation.generation_fingerprint != generation_fingerprint
            or not preparation.backup_path
            or not preparation.backup_digest
        ):
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"backup_queued"},
                phase="backup_failed",
                last_error="Prepared backup result lacked exact operation proof.",
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Prepared backup result lacked exact operation/generation "
                    "proof. Provider and runtime deletion were blocked."
                ),
            )
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"backup_queued"},
            phase="prepared",
            prepare_backup_path=preparation.backup_path,
            prepare_backup_digest=preparation.backup_digest,
            prepare_details=list(preparation.details),
            last_error="",
        )

    if operation["phase"] == "provider_cleanup_failed":
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"provider_cleanup_failed"},
            phase="provider_cleanup",
            last_error="",
        )
    elif operation["phase"] == "prepared":
        # This durable phase is the recovery instruction if the process stops
        # during an external provider call.
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"prepared"},
            phase="provider_cleanup",
        )

    if operation["phase"] == "provider_cleanup":
        try:
            provider_details = _cleanup_zernio_before_tenant_delete(
                tenant.id,
                account_ids=list(operation["account_ids"]),
                profile_ids=list(operation["profile_ids"]),
            )
        except HTTPException as exc:
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"provider_cleanup"},
                phase="provider_cleanup_failed",
                last_error=str(exc.detail),
            )
            raise
        except Exception as exc:
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"provider_cleanup"},
                phase="provider_cleanup_failed",
                last_error="Unexpected provider cleanup failure.",
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Unexpected provider cleanup failure; the runtime was not "
                    "deleted. Retry after checking the provider connection."
                ),
            ) from exc
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"provider_cleanup"},
            phase="provider_cleaned",
            provider_details=provider_details,
            last_error="",
        )

    if operation["phase"] == "delete_failed":
        operation = retry_delete_phase(
            slug=tenant.id,
            operation_id=operation_id,
            action="delete_tenant",
        )
    elif operation["phase"] == "provider_cleaned":
        # Persist final host intent before publishing the worker job. A crash
        # now is recovered with the same deterministic job id on the next call.
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"provider_cleaned"},
            phase="delete_dispatching",
        )
    if operation["phase"] == "delete_dispatching":
        operation = update_delete_operation(
            slug=tenant.id,
            operation_id=operation_id,
            expected_phases={"delete_dispatching"},
            phase="delete_queued",
        )

    if operation["phase"] == "delete_queued":
        delete_job_id = str(operation["delete_job_id"])
        result = read_host_action_result(
            job_id=delete_job_id,
            slug=tenant.id,
            action="delete_tenant",
            delete_operation_id=operation_id,
        )
        if result is None:
            result = queue_tenant_host_action(
                slug=tenant.id,
                action="delete_tenant",
                dashboard_url=dashboard_url,
                typed_slug=body.typedSlug,
                final_confirmation=body.finalConfirmation,
                requested_job_id=delete_job_id,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                prepared_backup_path=str(operation["prepare_backup_path"]),
                prepared_backup_digest=str(operation["prepare_backup_digest"]),
            )
        if result.status == "disabled":
            raise HTTPException(
                status_code=503,
                detail="Tenant delete worker is disabled. No tenant was deleted.",
            )
        if result.status == "failed":
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"delete_queued"},
                phase="delete_failed",
                last_error=result.message,
            )
            audit_log.record_event(
                tenant_id=tenant.id,
                action="tenant.delete_failed",
                result="failed",
                safe_summary=result.message,
                metadata={
                    "job_id": delete_job_id,
                    "delete_operation_id": operation_id,
                    "provider_cleanup_completed": True,
                },
            )
            raise HTTPException(status_code=502, detail=result.message)
        delete_active = host_action_job_is_active(
            job_id=delete_job_id,
            slug=tenant.id,
            action="delete_tenant",
        )
        if result.status != "succeeded" or delete_active:
            return {
                "success": False,
                "status": "delete_pending",
                "tenantId": tenant.id,
                "message": (
                    "Provider cleanup is complete and the durable host deletion "
                    "is still running. Retry this request to refresh its result."
                ),
                "details": list(operation["prepare_details"])
                + list(operation["provider_details"])
                + list(result.details),
                "jobId": delete_job_id,
                "operationId": operation_id,
            }
        result_proven = (
            result.safe_to_release is True
            and result.operation_id == operation_id
            and result.generation_fingerprint == generation_fingerprint
            and result.prepared_backup_path == operation["prepare_backup_path"]
            and result.prepared_backup_digest == operation["prepare_backup_digest"]
            and bool(result.backup_path)
            and bool(result.backup_digest)
        )
        if not result_proven:
            update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"delete_queued"},
                phase="delete_failed",
                last_error="Final worker result lacked exact safe-release proof.",
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Final worker result lacked exact safe-release and backup "
                    "proof. Local tenant state was retained for recovery."
                ),
            )
        client_root = os.getenv("NR3_TENANTS_CLIENT_DIR", "").strip()
        if not _runtime_slug_is_lexically_absent(client_root, tenant.id):
            return {
                "success": False,
                "status": "delete_verifying",
                "tenantId": tenant.id,
                "message": (
                    "The worker reported success, but runtime absence is not yet "
                    "visible to Nr3. Local state remains quarantined."
                ),
                "details": list(result.details),
                "jobId": delete_job_id,
                "operationId": operation_id,
            }
        with tenant_creation_lock(tenant.id):
            current = load_delete_operation(tenant.id)
            if (
                current is None
                or current.get("operation_id") != operation_id
                or current.get("phase") != "delete_queued"
                or tenant_provision_claim(tenant.id) is not None
                or not _runtime_slug_is_lexically_absent(client_root, tenant.id)
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Tenant lifecycle ownership changed during final "
                        "reconciliation. Local state remains quarantined."
                    ),
                )
            # Keep the delete claim active until every local store is proven
            # clear. A partial cleanup is retried idempotently from this phase.
            try:
                forget_tenant_state_strict(tenant.id)
            except Exception as exc:
                audit_log.record_event(
                    tenant_id=tenant.id,
                    action="tenant.delete_local_reconcile_failed",
                    result="failed",
                    safe_summary=(
                        "Host deletion succeeded but strict local-state cleanup "
                        "did not complete; lifecycle remains quarantined."
                    ),
                    metadata={
                        "job_id": delete_job_id,
                        "delete_operation_id": operation_id,
                    },
                )
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "Host deletion succeeded, but local control state could "
                        "not be fully reconciled. The tenant remains quarantined "
                        "and this confirmed request can be retried safely."
                    ),
                ) from exc
            if not _runtime_slug_is_lexically_absent(client_root, tenant.id):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Runtime mount visibility changed during local cleanup. "
                        "The tenant remains quarantined."
                    ),
                )
            operation = update_delete_operation(
                slug=tenant.id,
                operation_id=operation_id,
                expected_phases={"delete_queued"},
                phase="deleted",
                delete_details=list(result.details),
                delete_backup_path=result.backup_path,
                delete_backup_digest=result.backup_digest,
                last_error="",
            )
        audit_log.record_event(
            tenant_id=tenant.id,
            action="tenant.deleted",
            result="ok",
            safe_summary="Tenant permanently deleted after recoverable backup.",
            metadata={
                "job_id": delete_job_id,
                "delete_operation_id": operation_id,
            },
        )
        return {
            "success": True,
            "status": "deleted",
            "tenantId": tenant.id,
            "message": result.message,
            "details": list(operation["prepare_details"])
            + list(operation["provider_details"])
            + list(operation["delete_details"]),
            "jobId": delete_job_id,
            "operationId": operation_id,
        }

    raise HTTPException(
        status_code=409,
        detail=f"Delete operation is paused in unexpected phase {operation['phase']}.",
    )
