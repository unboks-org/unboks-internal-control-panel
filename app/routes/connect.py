from __future__ import annotations

import asyncio
import json
import logging
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request
import httpx

from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app import audit_log
from app import channel_connections
from app.config import get_settings
from app.delete_operations import (
    DeleteOperationConflict,
    require_tenant_mutation_generation,
)
from app.emailer import (
    build_whatsapp_connection_email,
    send_email,
    smtp_is_configured,
)
from app.security import is_authenticated
from app.provisioning import (
    AutoProvisionResult,
    queue_tenant_host_action,
    tenant_creation_lock,
)
from app.tenants import (
    get_tenant,
    get_tenant_client_data,
    list_tenants,
    tenant_contact_details,
    update_tenant_channel_account_allowlist,
)
from app.whatsapp_health import build_whatsapp_health, whatsapp_health_to_api
from app.zernio import (
    ZernioAccountSummary,
    ZernioAPIError,
    ZernioNotConfigured,
    ZernioService,
    build_whatsapp_callback_url,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/api", tags=["internal-api"])
public_router = APIRouter(tags=["connect"])
templates = Jinja2Templates(directory="app/templates")

CALLBACK_RESULT_PATH = "/connect/whatsapp/result"
FAILED_STATUSES = {"failed", "failure", "error", "cancelled", "canceled", "denied"}
PENDING_NUMBER_STATUSES = {
    "pending",
    "pending-number",
    "pending_number",
    "number-selection",
    "number-selection-required",
}
SAFE_CALLBACK_KEYS = {
    "connected",
    "status",
    "connection_status",
    "accountId",
    "account_id",
    "zernioAccountId",
    "zernio_account_id",
    "profileId",
    "profile_id",
    "phoneNumberId",
    "phone_number_id",
    "selectedPhoneNumberId",
    "selected_phone_number_id",
    "displayPhoneNumber",
    "display_phone_number",
    "username",
    "wabaId",
    "waba_id",
    "platform",
    "error",
    "error_description",
    "message",
}


@dataclass(frozen=True)
class _ZernioOwnerResolution:
    status: Literal["ready", "unmapped", "retryable"]
    tenant_id: str = ""
    generation_id: str = ""
    reason: str = ""


def _zernio_owner_ready(
    tenant_id: str,
    generation_id: str,
) -> _ZernioOwnerResolution:
    return _ZernioOwnerResolution(
        status="ready",
        tenant_id=tenant_id,
        generation_id=generation_id,
    )


def _zernio_owner_unmapped(reason: str) -> _ZernioOwnerResolution:
    return _ZernioOwnerResolution(status="unmapped", reason=reason)


def _zernio_owner_retryable(reason: str) -> _ZernioOwnerResolution:
    return _ZernioOwnerResolution(status="retryable", reason=reason)


class WhatsAppPhoneSelection(BaseModel):
    phoneNumberId: str
    accountId: Optional[str] = None


def _require_operator_json(request: Request) -> None:
    settings = get_settings()
    if not is_authenticated(request, settings):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def _whatsapp_callback_url(*, correlation_token: str | None = None) -> str:
    return build_whatsapp_callback_url(
        get_settings(),
        correlation_token=correlation_token,
    )


def _result_redirect(status: str, *, tenant_id: str | None = None) -> RedirectResponse:
    url = f"{CALLBACK_RESULT_PATH}?status={status}"
    if tenant_id:
        url = f"{url}&tenantId={tenant_id}"
    return RedirectResponse(url=url, status_code=303)


def _first_query_value(request: Request, *names: str) -> str | None:
    for name in names:
        value = request.query_params.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _safe_callback_payload(request: Request) -> dict[str, str]:
    payload: dict[str, str] = {}
    for key, value in request.query_params.multi_items():
        if key in SAFE_CALLBACK_KEYS and value:
            payload[key] = value[:500]
    return payload


def _safe_error_summary(request: Request) -> str:
    error = _first_query_value(
        request,
        "error_description",
        "error",
        "message",
    )
    return (error or "WhatsApp authorization failed.")[:500]


def _normalized_callback_status(request: Request) -> str:
    raw = _first_query_value(request, "status", "connection_status")
    if not raw:
        return ""
    return raw.strip().lower().replace(" ", "-")


def _is_expired(connection_request: channel_connections.ConnectionRequest) -> bool:
    expires_at = connection_request.state_token_expires_at
    if not expires_at:
        return False
    try:
        parsed = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed < datetime.now(timezone.utc)


def _safe_phone_option(account: ZernioAccountSummary) -> dict[str, object]:
    return {
        "accountId": account.id,
        "profileId": account.profile_id,
        "displayName": account.display_name,
        "username": account.username,
        "displayPhoneNumber": account.display_phone_number,
        "phoneNumberId": account.phone_number_id,
        "wabaId": account.waba_id,
        "enabled": account.enabled,
        "isActive": account.is_active,
        "platformStatus": account.platform_status,
    }


def _account_is_connected(account: ZernioAccountSummary) -> bool:
    return (
        bool(account.id)
        and account.platform.lower() == "whatsapp"
        and account.enabled
        and account.is_active
        and (account.platform_status or "").lower() in {"", "active"}
    )


def _display_phone(account: ZernioAccountSummary) -> str | None:
    return account.display_phone_number or account.username


def _ensure_connected_account_allowlisted(
    tenant_id: str,
    *,
    zernio_account_id: str,
    note: str,
    expected_generation_id: str | None = None,
) -> AutoProvisionResult:
    """Persist the strict account mapping directly or through the host worker."""
    if update_tenant_channel_account_allowlist(
        tenant_id,
        zernio_account_id=zernio_account_id,
        note=note,
        expected_generation_id=expected_generation_id,
    ):
        return AutoProvisionResult(
            status="succeeded",
            message="Strict WhatsApp account allowlist updated.",
        )

    result = queue_tenant_host_action(
        slug=tenant_id,
        action="repair_whatsapp_allowlist",
        zernio_account_id=zernio_account_id,
        allowlist_note=note,
    )
    log = logger.info if result.status in {"succeeded", "queued"} else logger.error
    log(
        "whatsapp_connected_allowlist_host_repair tenant=%s account=%s status=%s job_id=%s",
        tenant_id,
        zernio_account_id[:24],
        result.status,
        result.job_id or "",
    )
    return result


def _serialized_connected_account_mutation(function):
    """Hold the exact tenant generation across DB, allowlist, and queue writes."""
    def wrapped(
        tenant_id: str,
        account: ZernioAccountSummary,
        *,
        expected_generation_id: str | None = None,
        **kwargs,
    ):
        from app.delete_operations import require_tenant_mutation_generation

        with tenant_creation_lock(tenant_id):
            try:
                generation_id = require_tenant_mutation_generation(
                    tenant_id,
                    expected_generation_id=expected_generation_id,
                )
            except Exception as exc:
                raise channel_connections.ProviderOwnershipConflict(
                    f"Tenant generation changed; provider mutation was blocked: {exc}"
                ) from exc
            return function(
                tenant_id,
                account,
                expected_generation_id=generation_id,
                **kwargs,
            )

    return wrapped


@_serialized_connected_account_mutation
def _upsert_connected_account(
    tenant_id: str,
    account: ZernioAccountSummary,
    *,
    request_id: str | None = None,
    callback_payload: dict[str, str] | None = None,
    require_current_request: bool = False,
    enforce_latest_request: bool = False,
    expected_latest_request_id: str | None = None,
    expected_generation_id: str | None = None,
) -> tuple[channel_connections.TenantChannelConnection, AutoProvisionResult]:
    if require_current_request or enforce_latest_request:
        current_request = (
            channel_connections.get_connection_request(request_id)
            if request_id
            else None
        )
        latest_request = channel_connections.get_latest_connection_request_for_tenant(
            tenant_id
        )
        required_latest_id = (
            request_id if require_current_request else expected_latest_request_id
        )
        if (
            (latest_request.id if latest_request else None) != required_latest_id
            or (
                require_current_request
                and (
                    current_request is None
                    or current_request.status != "callback_received"
                )
            )
        ):
            raise channel_connections.ProviderOwnershipConflict(
                "The provider authorization request was superseded by a newer link."
            )
    metadata: dict[str, object] = {
        "zernio": {
            "displayName": account.display_name,
            "username": account.username,
            "platformStatus": account.platform_status,
            "enabled": account.enabled,
            "isActive": account.is_active,
        }
    }
    if callback_payload:
        metadata["callback"] = callback_payload
    # Claim provider ownership in SQLite before changing tenant-writable
    # routing state. A cross-tenant conflict must leave the allowlist untouched.
    connection = channel_connections.upsert_tenant_channel_connection(
        tenant_id=tenant_id,
        status="pending",
        zernio_profile_id=account.profile_id,
        zernio_account_id=account.id,
        zernio_account_verified=True,
        phone_number_id=account.phone_number_id,
        display_phone_number=_display_phone(account),
        waba_id=account.waba_id,
        metadata=metadata,
        last_request_id=request_id,
        last_error="Strict tenant allowlist verification is pending.",
        expected_generation_id=expected_generation_id,
    )
    allowlist_result = _ensure_connected_account_allowlisted(
        tenant_id,
        zernio_account_id=account.id,
        note=(
            "Nr3 WhatsApp connection: strict Zernio account allowlist for "
            f"{_display_phone(account) or account.username or 'connected WhatsApp'}."
        ),
        expected_generation_id=expected_generation_id,
    )
    if allowlist_result.status == "succeeded":
        connection_status = "connected"
        last_error = None
    elif allowlist_result.status == "queued":
        connection_status = "pending"
        last_error = "Strict tenant allowlist repair is queued."
    else:
        connection_status = "failed"
        last_error = (
            "Provider authorization succeeded, but strict tenant routing "
            "could not be secured."
        )
    connection = channel_connections.upsert_tenant_channel_connection(
        tenant_id=tenant_id,
        status=connection_status,
        zernio_profile_id=account.profile_id,
        zernio_account_id=account.id,
        zernio_account_verified=True,
        phone_number_id=account.phone_number_id,
        display_phone_number=_display_phone(account),
        waba_id=account.waba_id,
        metadata=metadata,
        last_request_id=request_id,
        last_error=last_error,
        expected_generation_id=expected_generation_id,
    )
    return connection, allowlist_result


def _record_unverified_connection_attempt(
    tenant_id: str,
    *,
    status: str,
    zernio_profile_id: str | None,
    zernio_account_id: str | None = None,
    request_id: str,
    callback_payload: dict[str, str],
    last_error: str | None,
    expected_generation_id: str | None = None,
) -> channel_connections.TenantChannelConnection | None:
    """Record request state without replacing any provider-verified account."""
    with tenant_creation_lock(tenant_id):
        generation_id = require_tenant_mutation_generation(
            tenant_id,
            expected_generation_id=expected_generation_id,
        )
        existing = channel_connections.get_tenant_channel_connection(tenant_id)
        latest = channel_connections.get_latest_connection_request_for_tenant(
            tenant_id
        )
        if latest is None or latest.id != request_id:
            logger.info(
                "whatsapp_connect_superseded_request_unchanged tenant=%s request=%s",
                tenant_id,
                request_id,
            )
            return existing
        if existing is not None and existing.zernio_account_verified:
            return existing
        return channel_connections.upsert_tenant_channel_connection(
            tenant_id=tenant_id,
            status=status,
            zernio_profile_id=zernio_profile_id,
            zernio_account_id=zernio_account_id,
            zernio_account_verified=False,
            metadata={"callback": callback_payload},
            last_request_id=request_id,
            last_error=last_error,
            expected_generation_id=generation_id,
        )


def _sync_whatsapp_connection_from_zernio(
    tenant_id: str,
    *,
    expected_generation_id: str | None = None,
    expected_account_id: str | None = None,
    expected_request_id: str | None = None,
    enforce_expected_request: bool = False,
    attach_latest_request: bool = True,
) -> channel_connections.TenantChannelConnection | None:
    """Reconcile Nr3 state from Zernio when the browser callback was missed."""
    try:
        if not expected_generation_id:
            expected_generation_id = (
                channel_connections.current_tenant_generation_id(tenant_id)
            )
    except channel_connections.ProviderOwnershipConflict:
        return None
    zernio_profile_id = _tenant_zernio_profile_id(tenant_id)
    if not zernio_profile_id:
        return None
    try:
        accounts = ZernioService().list_accounts(platform="whatsapp")
    except (ZernioNotConfigured, ZernioAPIError):
        return None
    expected_account_id = str(expected_account_id or "").strip() or None
    for account in accounts:
        if (
            account.profile_id == zernio_profile_id
            and _account_is_connected(account)
            and (expected_account_id is None or account.id == expected_account_id)
        ):
            latest = channel_connections.get_latest_connection_request_for_tenant(
                tenant_id
            )
            if enforce_expected_request and (
                (latest.id if latest else None) != expected_request_id
            ):
                return None
            request_to_update = latest if attach_latest_request else None
            try:
                connection, allowlist_result = _upsert_connected_account(
                    tenant_id,
                    account,
                    request_id=request_to_update.id if request_to_update else None,
                    enforce_latest_request=True,
                    expected_latest_request_id=(latest.id if latest else None),
                    expected_generation_id=expected_generation_id,
                )
            except channel_connections.ProviderOwnershipConflict as exc:
                logger.warning(
                    "whatsapp_status_provider_owner_conflict tenant=%s account=%s error=%s",
                    tenant_id,
                    account.id[:24],
                    str(exc)[:160],
                )
                return None
            exact_failed_request_can_recover = bool(
                request_to_update
                and request_to_update.status == "failed"
                and expected_account_id
                and not request_to_update.zernio_account_verified
                and request_to_update.zernio_account_id == account.id
                and request_to_update.zernio_profile_id == zernio_profile_id
            )
            if request_to_update and (
                request_to_update.status not in {
                    "connected",
                    "failed",
                    "expired",
                    "cancelled",
                }
                or exact_failed_request_can_recover
            ):
                channel_connections.update_connection_request(
                    request_to_update.id,
                    status=(
                        "connected"
                        if allowlist_result.status == "succeeded"
                        else "callback_received"
                        if allowlist_result.status == "queued"
                        else "failed"
                    ),
                    zernio_account_id=account.id,
                    zernio_account_verified=True,
                    selected_phone_number_id=account.phone_number_id,
                    display_phone_number=_display_phone(account),
                    callback_payload={
                        "source": "zernio_status_reconcile",
                        "accountId": account.id,
                        "profileId": account.profile_id or "",
                        "displayPhoneNumber": _display_phone(account) or "",
                    },
                    error_summary=connection.last_error,
                )
            return connection
    return None


def _tenant_zernio_profile_id(tenant_id: str) -> str | None:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    if (
        connection
        and connection.zernio_account_verified
        and connection.zernio_profile_id
    ):
        return connection.zernio_profile_id
    return channel_connections.get_tenant_zernio_profile_id(tenant_id)


def _create_whatsapp_authorization(
    tenant,
    *,
    actor: str,
    expected_generation_id: str | None = None,
) -> channel_connections.CreatedConnectionRequest:
    service = ZernioService()
    if not expected_generation_id:
        expected_generation_id = channel_connections.current_tenant_generation_id(
            tenant.id
        )
    zernio_profile_id, _ = channel_connections.ensure_tenant_zernio_profile(
        tenant_id=tenant.id,
        name=tenant.name,
        status=tenant.status,
        expected_generation_id=expected_generation_id,
        create_profile=lambda: service.create_profile(
            name=tenant.name,
            description=f"Unboks tenant workspace: {tenant.id}",
        ),
        delete_profile=getattr(service, "delete_profile", None),
    )

    # Zernio's standard redirect contract appends connection details but does
    # not echo the OAuth ``state`` returned by GET /connect/{platform}. Carry
    # an Nr3-owned nonce in the redirect URL instead. Zernio preserves an
    # existing query string when it appends its result parameters.
    correlation_token = secrets.token_urlsafe(48)
    connect_url = service.get_connect_url(
        platform="whatsapp",
        profile_id=zernio_profile_id,
        redirect_url=_whatsapp_callback_url(
            correlation_token=correlation_token,
        ),
    )

    created = channel_connections.create_connection_request(
        tenant_id=tenant.id,
        auth_url=connect_url.auth_url,
        zernio_profile_id=zernio_profile_id,
        state_token=correlation_token,
        status="link_generated",
        expected_generation_id=expected_generation_id,
    )
    logger.info(
        "whatsapp_connect_link_generated tenant=%s request_id=%s actor=%s",
        tenant.id,
        created.request.id,
        actor,
    )
    audit_log.record_event(
        tenant_id=tenant.id,
        action="whatsapp.connect_link_generated",
        result="ok",
        safe_summary="WhatsApp authorization link generated.",
        metadata={"request_id": created.request.id, "actor": actor},
    )
    return created


def _public_whatsapp_token_valid(tenant_id: str, token: str) -> bool:
    if not tenant_id or not token:
        return False
    data = get_tenant_client_data(tenant_id)
    expected = data.get("whatsapp_connect_token")
    expires_at = data.get("whatsapp_connect_token_expires_at")
    if isinstance(expires_at, str) and expires_at.strip():
        try:
            parsed = datetime.fromisoformat(expires_at.strip())
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > parsed:
                return False
        except ValueError:
            return False
    return (
        isinstance(expected, str)
        and bool(expected.strip())
        and hmac.compare_digest(token.strip(), expected.strip())
    )


def _load_whatsapp_phone_options(
    tenant_id: str,
) -> tuple[str | None, list[ZernioAccountSummary]]:
    zernio_profile_id = _tenant_zernio_profile_id(tenant_id)
    if not zernio_profile_id:
        return None, []
    service = ZernioService()
    accounts = service.list_accounts(platform="whatsapp")
    return zernio_profile_id, [
        account
        for account in accounts
        if account.profile_id == zernio_profile_id
        and _account_is_connected(account)
    ]


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

    try:
        expected_generation_id = channel_connections.current_tenant_generation_id(
            tenant.id
        )
    except (
        channel_connections.ProviderOwnershipConflict,
        DeleteOperationConflict,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        created = _create_whatsapp_authorization(
            tenant,
            actor="nr3-admin",
            expected_generation_id=expected_generation_id,
        )
    except channel_connections.ProviderOwnershipConflict as exc:
        raise HTTPException(
            status_code=409,
            detail="Tenant lifecycle changed; no WhatsApp link was created.",
        ) from exc
    except ZernioNotConfigured as exc:
        audit_log.record_event(
            tenant_id=tenant.id,
            action="whatsapp.connect_link_failed",
            result="failed",
            safe_summary="Zernio API key is not configured.",
        )
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ZernioAPIError as exc:
        logger.warning(
            "whatsapp_connect_link_failed tenant=%s status=%s error=%s",
            tenant.id,
            exc.status_code,
            exc.message,
        )
        audit_log.record_event(
            tenant_id=tenant.id,
            action="whatsapp.connect_link_failed",
            result="failed",
            safe_summary=exc.message,
            metadata={"status_code": exc.status_code},
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


@public_router.get("/connect/whatsapp/customer/start")
def customer_start_whatsapp_connection(tenantId: str = "", token: str = ""):
    """Customer-facing WhatsApp authorization launcher.

    Nr2 can show this URL to a signed-in tenant without exposing any Zernio
    credential. The token is per-tenant, random, and stored only in client.json.
    """
    tenant = get_tenant(tenantId.strip())
    if tenant is None:
        return _result_redirect("failed", tenant_id=tenantId.strip() or None)
    try:
        with tenant_creation_lock(tenant.id):
            expected_generation_id = (
                channel_connections.current_tenant_generation_id(tenant.id)
            )
            if not _public_whatsapp_token_valid(tenant.id, token):
                return _result_redirect("failed", tenant_id=tenant.id)
            created = _create_whatsapp_authorization(
                tenant,
                actor="tenant-self-service",
                expected_generation_id=expected_generation_id,
            )
    except (
        channel_connections.ProviderOwnershipConflict,
        ZernioNotConfigured,
        ZernioAPIError,
    ):
        return _result_redirect("failed", tenant_id=tenant.id)
    return RedirectResponse(url=created.request.auth_url or CALLBACK_RESULT_PATH, status_code=303)


@router.get("/tenants/{tenant_id}/channels/whatsapp/status")
def whatsapp_connection_status(tenant_id: str, request: Request) -> dict:
    """Return the safe WhatsApp/Zernio connection state for a tenant."""
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    try:
        expected_generation_id = channel_connections.current_tenant_generation_id(
            tenant.id
        )
    except channel_connections.ProviderOwnershipConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    connection = channel_connections.get_tenant_channel_connection(tenant.id)
    unverified_account_can_reconcile = bool(
        connection
        and not connection.zernio_account_verified
        and connection.zernio_account_id
    )
    expected_recovery_request_id: str | None = None
    attach_recovery_request = True
    superseded_unverified_candidate = False
    if unverified_account_can_reconcile and connection:
        latest_request = (
            channel_connections.get_latest_connection_request_for_tenant(tenant.id)
        )
        expected_recovery_request_id = connection.last_request_id
        if connection.last_request_id and (
            (latest_request.id if latest_request else None)
            == connection.last_request_id
        ):
            pass
        elif (
            connection.status == "connected"
            and not connection.last_request_id
            and (
                latest_request is None
                or latest_request.status in {
                    "connected",
                    "failed",
                    "expired",
                    "cancelled",
                }
                or _is_expired(latest_request)
            )
        ):
            # Some pre-generation connections have no request correlation and
            # an old, already-expired link row. Revalidate only the exact stored
            # account/profile and leave that historical request untouched.
            expected_recovery_request_id = (
                latest_request.id if latest_request else None
            )
            attach_recovery_request = False
        else:
            # A newly generated link supersedes any exact-account recovery
            # candidate left by an older failed/legacy request.
            unverified_account_can_reconcile = False
            superseded_unverified_candidate = True
    if not superseded_unverified_candidate and (
        connection is None
        or connection.status in {"pending", "not_connected"}
        or unverified_account_can_reconcile
    ):
        connection = _sync_whatsapp_connection_from_zernio(
            tenant.id,
            expected_generation_id=expected_generation_id,
            expected_account_id=(
                connection.zernio_account_id
                if unverified_account_can_reconcile and connection
                else None
            ),
            expected_request_id=expected_recovery_request_id,
            enforce_expected_request=unverified_account_can_reconcile,
            attach_latest_request=attach_recovery_request,
        ) or connection
    return whatsapp_health_to_api(build_whatsapp_health(tenant.id), tenant.id)


@router.post("/tenants/{tenant_id}/channels/whatsapp/repair-allowlist")
def repair_whatsapp_allowlist(tenant_id: str, request: Request) -> dict:
    """Repair strict runtime allowlist from the verified Nr3 connection record."""
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")
    try:
        with tenant_creation_lock(tenant.id):
            expected_generation_id = (
                channel_connections.current_tenant_generation_id(tenant.id)
            )
            connection = channel_connections.get_tenant_channel_connection(
                tenant.id
            )
            if (
                connection is None
                or connection.status != "connected"
                or not connection.zernio_account_verified
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "No provider-verified connected Zernio account found for "
                        "this tenant. Generate a new authorization link."
                    ),
                )
            if not connection.zernio_account_id:
                raise HTTPException(
                    status_code=409,
                    detail="Connected WhatsApp state has no provider account id.",
                )
            health = build_whatsapp_health(tenant.id)
            if health.connected:
                return whatsapp_health_to_api(health, tenant.id)
            if not health.repair_available:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "WhatsApp allowlist cannot be repaired from the current state."
                    ),
                )
            allowlist_note = (
                "Nr3 WhatsApp repair: strict Zernio account allowlist restored "
                "from verified connected account "
                f"{connection.display_phone_number or connection.zernio_account_id}."
            )
            require_tenant_mutation_generation(
                tenant.id,
                expected_generation_id=expected_generation_id,
            )
            written = update_tenant_channel_account_allowlist(
                tenant.id,
                zernio_account_id=connection.zernio_account_id,
                note=allowlist_note,
                expected_generation_id=expected_generation_id,
            )
            if not written:
                result = queue_tenant_host_action(
                    slug=tenant.id,
                    action="repair_whatsapp_allowlist",
                    zernio_account_id=connection.zernio_account_id,
                    allowlist_note=allowlist_note,
                )
                if result.status != "succeeded":
                    raise HTTPException(
                        status_code=500,
                        detail=result.message
                        or "Could not write strict allowlist to tenant client.json.",
                    )
            audit_log.record_event(
                tenant_id=tenant.id,
                action="whatsapp.allowlist_repaired",
                result="ok",
                safe_summary=(
                    "Strict WhatsApp/Zernio allowlist repaired from verified connection."
                ),
                metadata={
                    "account_id": connection.zernio_account_id,
                    "phone_number_id": connection.phone_number_id,
                },
            )
            return whatsapp_health_to_api(
                build_whatsapp_health(tenant.id), tenant.id
            )
    except (
        channel_connections.ProviderOwnershipConflict,
        DeleteOperationConflict,
    ) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/tenants/{tenant_id}/channels/whatsapp/connect/send-link")
def send_whatsapp_connection_link(tenant_id: str, request: Request) -> dict:
    """Send the latest generated WhatsApp authorization link to the tenant."""
    _require_operator_json(request)
    settings = get_settings()
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    contact = tenant_contact_details(tenant.id)
    to_email = contact.get("email", "")
    if not to_email:
        raise HTTPException(
            status_code=409,
            detail="Tenant contact email is missing.",
        )

    latest = channel_connections.get_latest_connection_request_for_tenant(
        tenant.id
    )
    if latest is None or not latest.auth_url:
        raise HTTPException(
            status_code=409,
            detail="Generate an authorization link first.",
        )
    if latest.status in {"connected", "failed", "expired", "cancelled"}:
        raise HTTPException(
            status_code=409,
            detail="Generate a fresh authorization link first.",
        )
    if _is_expired(latest):
        channel_connections.update_connection_request(
            latest.id,
            status="expired",
            error_summary="WhatsApp authorization link expired.",
        )
        raise HTTPException(
            status_code=409,
            detail="Authorization link expired. Generate a fresh link first.",
        )
    if not smtp_is_configured(settings):
        raise HTTPException(status_code=503, detail="SMTP is not configured.")

    draft = build_whatsapp_connection_email(
        client_first_name=contact.get("first_name", ""),
        authorization_link=latest.auth_url,
    )
    try:
        send_email(
            to_email=to_email,
            subject=draft.subject,
            body=draft.body,
            settings=settings,
        )
    except Exception as exc:
        audit_log.record_event(
            tenant_id=tenant.id,
            action="whatsapp.connect_email_failed",
            result="failed",
            safe_summary="WhatsApp connection email failed to send.",
            metadata={"request_id": latest.id},
        )
        raise HTTPException(
            status_code=502,
            detail="WhatsApp connection email failed to send.",
        ) from exc

    audit_log.record_event(
        tenant_id=tenant.id,
        action="whatsapp.connect_email_sent",
        result="ok",
        safe_summary="WhatsApp connection email sent.",
        metadata={"request_id": latest.id},
    )
    return {
        "success": True,
        "tenantId": tenant.id,
        "sent": True,
        "email": to_email,
        "message": f"Email sent successfully to {to_email}",
    }


@router.get("/tenants/{tenant_id}/channels/whatsapp/phone-numbers")
def whatsapp_phone_numbers(tenant_id: str, request: Request) -> dict:
    """Return safe WhatsApp phone number options for this tenant."""
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    try:
        zernio_profile_id, accounts = _load_whatsapp_phone_options(tenant.id)
    except ZernioNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ZernioAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc

    connection = channel_connections.get_tenant_channel_connection(tenant.id)
    if len(accounts) == 1:
        status = "single_phone"
    elif len(accounts) > 1:
        status = "multiple_phone"
    elif zernio_profile_id:
        status = "no_phone_numbers"
    else:
        status = "not_connected"

    return {
        "success": True,
        "tenantId": tenant.id,
        "channel": "whatsapp",
        "provider": "zernio",
        "status": status,
        "zernioProfileId": zernio_profile_id,
        "selectedPhoneNumberId": (
            connection.phone_number_id if connection is not None else None
        ),
        "phoneNumbers": [_safe_phone_option(account) for account in accounts],
    }


@router.post("/tenants/{tenant_id}/channels/whatsapp/phone-numbers/select")
def select_whatsapp_phone_number(
    tenant_id: str,
    selection: WhatsAppPhoneSelection,
    request: Request,
) -> dict:
    """Persist the operator-confirmed WhatsApp phone number for a tenant."""
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    try:
        expected_generation_id = channel_connections.current_tenant_generation_id(
            tenant.id
        )
    except channel_connections.ProviderOwnershipConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    try:
        zernio_profile_id, accounts = _load_whatsapp_phone_options(tenant.id)
    except ZernioNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ZernioAPIError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    if not zernio_profile_id:
        raise HTTPException(status_code=409, detail="WhatsApp connection not started.")

    selected = next(
        (
            account
            for account in accounts
            if account.phone_number_id == selection.phoneNumberId
            and (
                selection.accountId is None
                or account.id == selection.accountId
            )
        ),
        None,
    )
    if selected is None:
        raise HTTPException(status_code=400, detail="Invalid WhatsApp phone selection.")

    existing_connection = channel_connections.get_tenant_channel_connection(
        tenant.id
    )
    last_request_id = (
        existing_connection.last_request_id if existing_connection else None
    )
    try:
        connection, allowlist_result = _upsert_connected_account(
            tenant.id,
            selected,
            request_id=last_request_id,
            callback_payload={"selected_via": "operator_phone_selection"},
            expected_generation_id=expected_generation_id,
        )
    except channel_connections.ProviderOwnershipConflict as exc:
        audit_log.record_event(
            tenant_id=tenant.id,
            action="whatsapp.phone_selection_provider_ownership_conflict",
            result="blocked",
            safe_summary=(
                "WhatsApp phone selection was blocked because provider ownership "
                "belongs to another tenant."
            ),
            metadata={"request_id": last_request_id},
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "This WhatsApp account or Zernio profile is already connected "
                "to another tenant."
            ),
        ) from exc
    if last_request_id:
        request_status = (
            "connected"
            if allowlist_result.status == "succeeded"
            else "callback_received"
            if allowlist_result.status == "queued"
            else "failed"
        )
        channel_connections.update_connection_request(
            last_request_id,
            status=request_status,
            zernio_account_id=selected.id,
            zernio_account_verified=True,
            selected_phone_number_id=selected.phone_number_id,
            display_phone_number=selected.display_phone_number,
            callback_payload={"selected_via": "operator_phone_selection"},
            error_summary=connection.last_error,
        )
    if allowlist_result.status not in {"succeeded", "queued"}:
        raise HTTPException(
            status_code=500,
            detail=(
                "WhatsApp was selected at the provider, but the strict tenant "
                "allowlist could not be persisted or queued for repair."
            ),
        )
    audit_log.record_event(
        tenant_id=tenant.id,
        action="whatsapp.phone_selected",
        result="ok",
        safe_summary="WhatsApp phone number selected.",
        metadata={
            "request_id": last_request_id,
            "account_id": selected.id,
            "phone_number_id": selected.phone_number_id,
        },
    )

    return {
        "success": True,
        "tenantId": tenant.id,
        "channel": connection.channel,
        "provider": connection.provider,
        "status": connection.status,
        "connected": allowlist_result.status == "succeeded",
        "allowlistRepairQueued": allowlist_result.status == "queued",
        "displayPhoneNumber": connection.display_phone_number,
        "phoneNumberId": connection.phone_number_id,
        "providerAccountId": connection.zernio_account_id,
        "zernioProfileId": connection.zernio_profile_id,
        "connectedAt": connection.connected_at,
        "lastUpdatedAt": connection.updated_at,
        "lastError": connection.last_error,
    }


@router.get("/connect/whatsapp/callback")
def whatsapp_connection_callback(request: Request):
    """Receive the public Zernio redirect and update Nr3 connection state.

    This endpoint is intentionally unauthenticated because the browser returns
    here from Meta/Zernio. Nr3's random redirect nonce is the trust anchor.
    """
    # ``nr3_token`` is the only value generated by Nr3 for the standard
    # redirect. Keep the older provider fields as a compatibility path for
    # authorization links issued before this fix, but never prefer them over
    # the Nr3-owned correlation token.
    state_token = _first_query_value(
        request,
        "nr3_token",
        "state",
        "connect_token",
    )
    if not state_token:
        logger.warning("whatsapp_connect_callback_missing_state")
        return _result_redirect("failed")

    connection_request = channel_connections.find_connection_request_by_state_token(
        state_token
    )
    if connection_request is None:
        logger.warning("whatsapp_connect_callback_invalid_state")
        return _result_redirect("failed")

    tenant_id = connection_request.tenant_id
    callback_payload = _safe_callback_payload(request)
    if connection_request.status in {
        "connected",
        "failed",
        "expired",
        "cancelled",
        "callback_received",
    }:
        replay_status = (
            "success"
            if connection_request.status == "connected"
            else "pending-activation"
            if connection_request.status == "callback_received"
            else "failed"
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_replayed",
            result="ignored",
            safe_summary="Duplicate WhatsApp callback was ignored.",
            metadata={"request_id": connection_request.id},
        )
        return _result_redirect(replay_status, tenant_id=tenant_id)
    if _is_expired(connection_request):
        channel_connections.update_connection_request(
            connection_request.id,
            status="expired",
            callback_payload=callback_payload,
            error_summary="WhatsApp authorization link expired.",
        )
        _record_unverified_connection_attempt(
            tenant_id,
            status="failed",
            zernio_profile_id=connection_request.zernio_profile_id,
            request_id=connection_request.id,
            callback_payload=callback_payload,
            expected_generation_id=connection_request.tenant_generation_id,
            last_error="WhatsApp authorization link expired.",
        )
        logger.info(
            "whatsapp_connect_callback_expired tenant=%s request_id=%s",
            tenant_id,
            connection_request.id,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_expired",
            result="failed",
            safe_summary="WhatsApp authorization link expired.",
            metadata={"request_id": connection_request.id},
        )
        return _result_redirect("failed", tenant_id=tenant_id)

    if not channel_connections.claim_connection_request_callback(
        connection_request.id
    ):
        current = channel_connections.get_connection_request(connection_request.id)
        replay_status = (
            "success"
            if current and current.status == "connected"
            else "pending-activation"
            if current and current.status == "callback_received"
            else "failed"
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_replayed",
            result="ignored",
            safe_summary="Concurrent WhatsApp callback was ignored.",
            metadata={"request_id": connection_request.id},
        )
        return _result_redirect(replay_status, tenant_id=tenant_id)

    status = _normalized_callback_status(request)
    if status in FAILED_STATUSES or _first_query_value(
        request,
        "error",
        "error_description",
    ):
        error_summary = _safe_error_summary(request)
        channel_connections.update_connection_request(
            connection_request.id,
            status="failed",
            callback_payload=callback_payload,
            error_summary=error_summary,
        )
        _record_unverified_connection_attempt(
            tenant_id,
            status="failed",
            zernio_profile_id=connection_request.zernio_profile_id,
            request_id=connection_request.id,
            callback_payload=callback_payload,
            expected_generation_id=connection_request.tenant_generation_id,
            last_error=error_summary,
        )
        logger.info(
            "whatsapp_connect_callback_failed tenant=%s request_id=%s",
            tenant_id,
            connection_request.id,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_failed",
            result="failed",
            safe_summary=error_summary,
            metadata={"request_id": connection_request.id},
        )
        return _result_redirect("failed", tenant_id=tenant_id)

    zernio_account_id = _first_query_value(
        request,
        "accountId",
        "account_id",
        "zernioAccountId",
        "zernio_account_id",
    )
    phone_number_id = _first_query_value(
        request,
        "phoneNumberId",
        "phone_number_id",
        "selectedPhoneNumberId",
        "selected_phone_number_id",
    )
    display_phone_number = _first_query_value(
        request,
        "displayPhoneNumber",
        "display_phone_number",
        "username",
    )
    waba_id = _first_query_value(request, "wabaId", "waba_id")

    if (
        status in PENDING_NUMBER_STATUSES
        or not zernio_account_id
    ):
        channel_connections.update_connection_request(
            connection_request.id,
            status="pending_number",
            zernio_account_id=zernio_account_id,
            zernio_account_verified=False,
            callback_payload=callback_payload,
            error_summary=None,
        )
        _record_unverified_connection_attempt(
            tenant_id,
            status="pending",
            zernio_profile_id=connection_request.zernio_profile_id,
            zernio_account_id=zernio_account_id,
            request_id=connection_request.id,
            callback_payload=callback_payload,
            expected_generation_id=connection_request.tenant_generation_id,
            last_error=None,
        )
        logger.info(
            "whatsapp_connect_callback_pending_number tenant=%s request_id=%s",
            tenant_id,
            connection_request.id,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_pending_number",
            result="pending",
            safe_summary="WhatsApp authorization received; phone number pending.",
            metadata={
                "request_id": connection_request.id,
                "account_id": zernio_account_id,
            },
        )
        return _result_redirect("pending-number", tenant_id=tenant_id)

    try:
        zernio_account = ZernioService().get_account(zernio_account_id)
    except (AttributeError, ZernioNotConfigured, ZernioAPIError) as exc:
        zernio_account = None
        logger.warning(
            "whatsapp_connect_callback_account_lookup_failed tenant=%s account=%s error=%s",
            tenant_id,
            zernio_account_id[:20],
            str(exc)[:200],
        )
    verified_account = bool(
        zernio_account
        and _account_is_connected(zernio_account)
        and zernio_account.profile_id == connection_request.zernio_profile_id
    )
    if not verified_account:
        error_summary = (
            "WhatsApp authorization could not be matched to an active account "
            "owned by this tenant's Zernio profile."
        )
        channel_connections.update_connection_request(
            connection_request.id,
            status="failed",
            zernio_account_id=zernio_account_id,
            zernio_account_verified=False,
            callback_payload=callback_payload,
            error_summary=error_summary,
        )
        _record_unverified_connection_attempt(
            tenant_id,
            status="failed",
            zernio_profile_id=connection_request.zernio_profile_id,
            zernio_account_id=zernio_account_id,
            request_id=connection_request.id,
            callback_payload=callback_payload,
            expected_generation_id=connection_request.tenant_generation_id,
            last_error=error_summary,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_account_verification_failed",
            result="failed",
            safe_summary=error_summary,
            metadata={
                "request_id": connection_request.id,
                "account_id": zernio_account_id,
            },
        )
        return _result_redirect("failed", tenant_id=tenant_id)

    # Provider data, not callback query parameters, is authoritative once the
    # callback claims connection success.
    assert zernio_account is not None
    phone_number_id = zernio_account.phone_number_id
    display_phone_number = _display_phone(zernio_account)
    waba_id = zernio_account.waba_id
    try:
        _, allowlist_result = _upsert_connected_account(
            tenant_id,
            zernio_account,
            request_id=connection_request.id,
            callback_payload=callback_payload,
            require_current_request=True,
            expected_generation_id=connection_request.tenant_generation_id,
        )
    except channel_connections.ProviderOwnershipConflict as exc:
        current_request = channel_connections.get_connection_request(
            connection_request.id
        )
        latest_request = (
            channel_connections.get_latest_connection_request_for_tenant(tenant_id)
        )
        if (
            current_request is not None
            and latest_request is not None
            and (
                current_request.status == "cancelled"
                or current_request.id != latest_request.id
            )
        ):
            audit_log.record_event(
                tenant_id=tenant_id,
                action="whatsapp.callback_superseded",
                result="ignored",
                safe_summary=(
                    "An older WhatsApp callback was ignored after a newer "
                    "authorization link was generated."
                ),
                metadata={"request_id": connection_request.id},
            )
            return _result_redirect("failed", tenant_id=tenant_id)
        error_summary = (
            "Provider authorization matched an account or profile already "
            "owned by another tenant; routing remained disabled."
        )
        try:
            channel_connections.update_connection_request(
                connection_request.id,
                status="failed",
                callback_payload=callback_payload,
                error_summary=error_summary,
            )
            _record_unverified_connection_attempt(
                tenant_id,
                status="failed",
                zernio_profile_id=connection_request.zernio_profile_id,
                request_id=connection_request.id,
                callback_payload=callback_payload,
                expected_generation_id=connection_request.tenant_generation_id,
                last_error=error_summary,
            )
        except (channel_connections.ProviderOwnershipConflict, ValueError) as stale_exc:
            # A generation rotation deliberately makes the old request
            # immutable. Keep this conflict path read-only instead of turning
            # a safely rejected callback into a 500 response.
            logger.info(
                "whatsapp_callback_stale_request_unchanged tenant=%s request=%s error=%s",
                tenant_id,
                connection_request.id,
                str(stale_exc)[:160],
            )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_provider_ownership_conflict",
            result="blocked",
            safe_summary=error_summary,
            metadata={"request_id": connection_request.id},
        )
        logger.warning(
            "whatsapp_callback_provider_ownership_conflict tenant=%s request=%s error=%s",
            tenant_id,
            connection_request.id,
            str(exc)[:160],
        )
        return _result_redirect("failed", tenant_id=tenant_id)
    if allowlist_result.status not in {"succeeded", "queued"}:
        error_summary = (
            "Provider authorization succeeded, but the strict tenant allowlist "
            "could not be persisted or queued for repair."
        )
        channel_connections.update_connection_request(
            connection_request.id,
            status="failed",
            zernio_account_id=zernio_account_id,
            zernio_account_verified=True,
            selected_phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            error_summary=error_summary,
        )
        logger.error(
            "whatsapp_connect_callback_allowlist_failed tenant=%s request_id=%s status=%s",
            tenant_id,
            connection_request.id,
            allowlist_result.status,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_allowlist_failed",
            result="failed",
            safe_summary=error_summary,
            metadata={
                "request_id": connection_request.id,
                "account_id": zernio_account_id,
            },
        )
        return _result_redirect("failed", tenant_id=tenant_id)
    channel_connections.update_connection_request(
        connection_request.id,
        status=(
            "connected"
            if allowlist_result.status == "succeeded"
            else "callback_received"
        ),
        zernio_account_id=zernio_account_id,
        zernio_account_verified=True,
        selected_phone_number_id=phone_number_id,
        display_phone_number=display_phone_number,
        callback_payload=callback_payload,
        error_summary=(
            None
            if allowlist_result.status == "succeeded"
            else "Strict tenant allowlist repair is queued."
        ),
    )
    if allowlist_result.status == "queued":
        logger.info(
            "whatsapp_connect_callback_activation_pending tenant=%s request_id=%s",
            tenant_id,
            connection_request.id,
        )
        audit_log.record_event(
            tenant_id=tenant_id,
            action="whatsapp.callback_activation_pending",
            result="pending",
            safe_summary=(
                "WhatsApp authorization completed; strict tenant routing is "
                "queued for activation."
            ),
            metadata={
                "request_id": connection_request.id,
                "account_id": zernio_account_id,
                "phone_number_id": phone_number_id,
            },
        )
        return _result_redirect("pending-activation", tenant_id=tenant_id)

    logger.info(
        "whatsapp_connect_callback_connected tenant=%s request_id=%s",
        tenant_id,
        connection_request.id,
    )
    audit_log.record_event(
        tenant_id=tenant_id,
        action="whatsapp.callback_connected",
        result="ok",
        safe_summary="WhatsApp connection completed.",
        metadata={
            "request_id": connection_request.id,
            "account_id": zernio_account_id,
            "phone_number_id": phone_number_id,
        },
    )
    return _result_redirect("success", tenant_id=tenant_id)


def _zernio_payload_account_id(payload: dict) -> str:
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
    account_id = (
        data.get("accountId")
        or data.get("account_id")
        or payload.get("accountId")
        or payload.get("account_id")
    )
    if account_id:
        return str(account_id).strip()
    account = payload.get("account")
    if isinstance(account, dict):
        return str(account.get("id") or account.get("_id") or "").strip()
    return ""


def _signature_header(request: Request) -> str:
    return (
        request.headers.get("X-Zernio-Signature")
        or request.headers.get("X-Hub-Signature-256")
        or request.headers.get("X-Signature")
        or ""
    ).strip()


def _verify_zernio_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    if not body or not signature or not secret:
        return False
    algorithm = "sha256"
    received = signature
    if "=" in signature:
        algorithm, received = signature.split("=", 1)
    if algorithm.lower() != "sha256":
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received.strip(), expected)


def _tenant_id_for_zernio_account(account_id: str) -> _ZernioOwnerResolution:
    if not account_id:
        return _zernio_owner_unmapped("missing_account")
    connection = channel_connections.get_tenant_channel_connection_by_account_id(
        account_id
    )
    if connection:
        try:
            with tenant_creation_lock(connection.tenant_id):
                generation_id = require_tenant_mutation_generation(
                    connection.tenant_id
                )
                # Re-read both provider ownership and the strict runtime
                # allowlist under the same lifecycle lease that captures the
                # generation. A stale pre-delete mapping cannot authorize B.
                current = (
                    channel_connections.get_tenant_channel_connection_by_account_id(
                        account_id
                    )
                )
                allowlist = (
                    get_tenant_client_data(connection.tenant_id).get(
                        "channel_account_allowlist"
                    )
                    or {}
                )
                allowed = (
                    allowlist.get("zernio_accounts")
                    if isinstance(allowlist, dict)
                    and allowlist.get("mode") == "strict"
                    else []
                )
                if (
                    current is None
                    or current.id != connection.id
                    or current.tenant_id != connection.tenant_id
                ):
                    return _zernio_owner_unmapped("stale_or_conflicting_owner")
                if account_id in {str(item).strip() for item in allowed or []}:
                    return _zernio_owner_ready(connection.tenant_id, generation_id)
        except (DeleteOperationConflict, channel_connections.ProviderOwnershipConflict):
            return _zernio_owner_unmapped("stale_or_conflicting_owner")
        logger.warning(
            "zernio_webhook_router_connected_account_not_allowlisted tenant=%s account=%s",
            connection.tenant_id,
            account_id[:24],
        )
        return _zernio_owner_retryable("strict_allowlist_not_ready")
    # Never route solely from legacy client.json. Older callback handling could
    # persist an unverified query-string account id there. The provider-backed
    # reconciliation below must re-establish account/profile ownership first.
    return _sync_tenant_for_zernio_account(account_id)


def _sync_tenant_for_zernio_account(account_id: str) -> _ZernioOwnerResolution:
    """Self-heal webhook routing when Zernio completed but callback state was missed."""
    if not account_id:
        return _zernio_owner_unmapped("missing_account")
    owner_candidates: list[tuple[object, str, str]] = []
    for candidate in list_tenants():
        try:
            generation_id = channel_connections.current_tenant_generation_id(
                candidate.id
            )
        except channel_connections.ProviderOwnershipConflict:
            continue
        profile_id = _tenant_zernio_profile_id(candidate.id)
        if profile_id:
            owner_candidates.append((candidate, profile_id, generation_id))
    try:
        accounts = ZernioService().list_accounts(platform="whatsapp")
    except (ZernioNotConfigured, ZernioAPIError) as exc:
        logger.info(
            "zernio_webhook_router_reconcile_unavailable account=%s error=%s",
            account_id[:24],
            str(exc)[:120],
        )
        return _zernio_owner_retryable("provider_lookup_unavailable")

    matched = next(
        (
            account
            for account in accounts
            if account.id == account_id and _account_is_connected(account)
        ),
        None,
    )
    if matched is None or not matched.profile_id:
        return _zernio_owner_unmapped("provider_account_not_connected")

    owners = [item for item in owner_candidates if item[1] == matched.profile_id]
    if len(owners) != 1:
        logger.warning(
            "zernio_webhook_router_profile_owner_ambiguous profile=%s matches=%d",
            matched.profile_id[:24],
            len(owners),
        )
        return _zernio_owner_unmapped("ambiguous_profile_owner")
    tenant, _, expected_generation_id = owners[0]
    try:
        _, allowlist_result = _upsert_connected_account(
            tenant.id,
            matched,
            callback_payload={
                "source": "zernio_webhook_router_reconcile",
                "accountId": matched.id,
                "profileId": matched.profile_id,
                "displayPhoneNumber": _display_phone(matched) or "",
            },
            expected_generation_id=expected_generation_id,
        )
    except channel_connections.ProviderOwnershipConflict:
        logger.warning(
            "zernio_webhook_router_provider_owner_conflict tenant=%s account=%s",
            tenant.id,
            account_id[:24],
        )
        return _zernio_owner_unmapped("provider_owner_conflict")
    if allowlist_result.status != "succeeded":
        logger.info(
            "zernio_webhook_router_reconcile_allowlist_pending tenant=%s account=%s status=%s",
            tenant.id,
            account_id[:24],
            allowlist_result.status,
        )
        return _zernio_owner_retryable("strict_allowlist_repair_pending")
    logger.info(
        "zernio_webhook_router_reconciled tenant=%s account=%s",
        tenant.id,
        account_id[:24],
    )
    return _zernio_owner_ready(tenant.id, expected_generation_id)


async def _forward_zernio_webhook_to_tenant(
    *,
    tenant_id: str,
    body: bytes,
    signature: str,
    content_type: str,
) -> tuple[int, str]:
    url = f"http://wtyj-{tenant_id}:8001/webhooks/zernio"
    headers = {"Content-Type": content_type or "application/json"}
    if signature:
        headers["X-Zernio-Signature"] = signature
    async with httpx.AsyncClient(timeout=12) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code, response.text[:500]


def _forward_generation_bound_zernio_webhook(
    *,
    tenant_id: str,
    expected_generation_id: str,
    account_id: str,
    body: bytes,
    signature: str,
    content_type: str,
) -> tuple[int, str]:
    """Forward while the exact tenant generation owns the reusable hostname.

    This function runs in a worker thread. Holding a synchronous filesystem
    lock across an ``await`` on the main event loop can deadlock another async
    request; the private event loop here keeps the server loop free while the
    cross-process lifecycle lease serializes delete/recreate and forwarding.
    """
    with tenant_creation_lock(tenant_id):
        require_tenant_mutation_generation(
            tenant_id,
            expected_generation_id=expected_generation_id,
        )
        current = channel_connections.get_tenant_channel_connection_by_account_id(
            account_id
        )
        allowlist = (
            get_tenant_client_data(tenant_id).get("channel_account_allowlist")
            or {}
        )
        allowed = (
            allowlist.get("zernio_accounts")
            if isinstance(allowlist, dict) and allowlist.get("mode") == "strict"
            else []
        )
        if (
            current is None
            or current.tenant_id != tenant_id
            or account_id not in {str(item).strip() for item in allowed or []}
        ):
            raise channel_connections.ProviderOwnershipConflict(
                "Webhook routing ownership changed before forwarding."
            )
        return asyncio.run(
            _forward_zernio_webhook_to_tenant(
                tenant_id=tenant_id,
                body=body,
                signature=signature,
                content_type=content_type,
            )
        )


@router.post("/zernio/webhook-router")
async def zernio_webhook_router(request: Request) -> PlainTextResponse:
    """Route the single Zernio webhook stream to the owning tenant container."""
    body = await request.body()
    settings = get_settings()
    if not settings.zernio_webhook_secret:
        logger.error("zernio_webhook_router_secret_missing")
        return PlainTextResponse("Webhook secret not configured", status_code=503)

    signature = _signature_header(request)
    if not _verify_zernio_webhook_signature(
        body, signature, settings.zernio_webhook_secret
    ):
        logger.warning("zernio_webhook_router_bad_signature")
        return PlainTextResponse("Invalid signature", status_code=401)

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("zernio_webhook_router_bad_json")
        return PlainTextResponse("OK", status_code=202)
    if not isinstance(payload, dict):
        logger.warning("zernio_webhook_router_bad_payload")
        return PlainTextResponse("OK", status_code=202)

    account_id = _zernio_payload_account_id(payload)
    # Ownership resolution may take the synchronous lifecycle flock (and, on
    # self-heal, perform a provider read), so keep it off the async event loop.
    resolution = await run_in_threadpool(_tenant_id_for_zernio_account, account_id)
    if resolution.status == "retryable":
        logger.warning(
            "zernio_webhook_router_resolution_retryable account=%s event=%s reason=%s",
            account_id[:24],
            str(payload.get("event") or "")[:80],
            resolution.reason,
        )
        return PlainTextResponse(
            "Routing temporarily unavailable",
            status_code=503,
            headers={"Retry-After": "5"},
        )
    if resolution.status != "ready":
        logger.warning(
            "zernio_webhook_router_unmapped_account account=%s event=%s reason=%s",
            account_id[:24],
            str(payload.get("event") or "")[:80],
            resolution.reason,
        )
        return PlainTextResponse("OK", status_code=202)
    tenant_id = resolution.tenant_id
    expected_generation_id = resolution.generation_id

    try:
        status_code, response_text = await run_in_threadpool(
            _forward_generation_bound_zernio_webhook,
            tenant_id=tenant_id,
            expected_generation_id=expected_generation_id,
            account_id=account_id,
            body=body,
            signature=signature,
            content_type=request.headers.get("Content-Type", "application/json"),
        )
    except (DeleteOperationConflict, channel_connections.ProviderOwnershipConflict) as exc:
        logger.warning(
            "zernio_webhook_router_stale_generation tenant=%s account=%s error=%s",
            tenant_id,
            account_id[:24],
            str(exc)[:200],
        )
        return PlainTextResponse("OK", status_code=202)
    except Exception as exc:
        logger.warning(
            "zernio_webhook_router_forward_failed tenant=%s account=%s error=%s",
            tenant_id,
            account_id[:24],
            str(exc)[:200],
        )
        return PlainTextResponse("Forward failed", status_code=502)

    if status_code >= 400:
        logger.warning(
            "zernio_webhook_router_tenant_rejected tenant=%s account=%s status=%s body=%s",
            tenant_id,
            account_id[:24],
            status_code,
            response_text[:200],
        )
        return PlainTextResponse("Tenant rejected webhook", status_code=status_code)
    logger.info(
        "zernio_webhook_router_forwarded tenant=%s account=%s event=%s",
        tenant_id,
        account_id[:24],
        str(payload.get("event") or "")[:80],
    )
    return PlainTextResponse("OK", status_code=200)


@public_router.get("/connect/whatsapp/result", response_class=HTMLResponse)
def whatsapp_connection_result(request: Request, status: str = "failed"):
    safe_status = (
        status
        if status in {"success", "pending-number", "pending-activation", "failed"}
        else "failed"
    )
    content: dict[str, dict[str, str]] = {
        "success": {
            "eyebrow": "WhatsApp connection",
            "title": "Connection received",
            "message": (
                "Your WhatsApp authorization was received. "
                "You can close this window."
            ),
            "chip": "Success",
            "chip_class": "status-ok",
        },
        "pending-number": {
            "eyebrow": "WhatsApp connection",
            "title": "Phone number needs review",
            "message": (
                "Authorization was received. The Unboks team will confirm "
                "the phone number before activating WhatsApp."
            ),
            "chip": "Pending",
            "chip_class": "status-warn",
        },
        "pending-activation": {
            "eyebrow": "WhatsApp connection",
            "title": "Activation in progress",
            "message": (
                "Your WhatsApp authorization was received. Unboks is securing "
                "the tenant-specific routing before messages are activated."
            ),
            "chip": "Pending",
            "chip_class": "status-warn",
        },
        "failed": {
            "eyebrow": "WhatsApp connection",
            "title": "Connection not completed",
            "message": (
                "We could not complete the WhatsApp connection. "
                "Please contact the Unboks team for a new secure link."
            ),
            "chip": "Failed",
            "chip_class": "status-error",
        },
    }
    return templates.TemplateResponse(
        request,
        "whatsapp_connect_result.html",
        {"result": content[safe_status]},
    )
