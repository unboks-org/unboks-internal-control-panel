from __future__ import annotations

import json
import logging
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
import httpx

from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import audit_log
from app import channel_connections
from app.config import get_settings
from app.emailer import (
    build_whatsapp_connection_email,
    send_email,
    smtp_is_configured,
)
from app.security import is_authenticated
from app.tenants import (
    get_tenant,
    get_tenant_client_data,
    list_tenants,
    tenant_contact_details,
)
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
    "state",
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


class WhatsAppPhoneSelection(BaseModel):
    phoneNumberId: str
    accountId: Optional[str] = None


def _require_operator_json(request: Request) -> None:
    settings = get_settings()
    if not is_authenticated(request, settings):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def _whatsapp_callback_url() -> str:
    return build_whatsapp_callback_url(get_settings())


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


def _upsert_connected_account(
    tenant_id: str,
    account: ZernioAccountSummary,
    *,
    request_id: str | None = None,
    callback_payload: dict[str, str] | None = None,
) -> channel_connections.TenantChannelConnection:
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
    return channel_connections.upsert_tenant_channel_connection(
        tenant_id=tenant_id,
        status="connected",
        zernio_profile_id=account.profile_id,
        zernio_account_id=account.id,
        phone_number_id=account.phone_number_id,
        display_phone_number=_display_phone(account),
        waba_id=account.waba_id,
        metadata=metadata,
        last_request_id=request_id,
        last_error=None,
    )


def _sync_whatsapp_connection_from_zernio(
    tenant_id: str,
) -> channel_connections.TenantChannelConnection | None:
    """Reconcile Nr3 state from Zernio when the browser callback was missed."""
    zernio_profile_id = _tenant_zernio_profile_id(tenant_id)
    if not zernio_profile_id:
        return None
    try:
        accounts = ZernioService().list_accounts(platform="whatsapp")
    except (ZernioNotConfigured, ZernioAPIError):
        return None
    for account in accounts:
        if account.profile_id == zernio_profile_id and _account_is_connected(account):
            latest = channel_connections.get_latest_connection_request_for_tenant(
                tenant_id
            )
            if latest and latest.status not in {
                "connected",
                "failed",
                "expired",
                "cancelled",
            }:
                channel_connections.update_connection_request(
                    latest.id,
                    status="connected",
                    zernio_account_id=account.id,
                    selected_phone_number_id=account.phone_number_id,
                    display_phone_number=_display_phone(account),
                    callback_payload={
                        "source": "zernio_status_reconcile",
                        "accountId": account.id,
                        "profileId": account.profile_id or "",
                        "displayPhoneNumber": _display_phone(account) or "",
                    },
                    error_summary=None,
                )
            return _upsert_connected_account(
                tenant_id,
                account,
                request_id=latest.id if latest else None,
            )
    return None


def _tenant_zernio_profile_id(tenant_id: str) -> str | None:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    if connection and connection.zernio_profile_id:
        return connection.zernio_profile_id
    return channel_connections.get_tenant_zernio_profile_id(tenant_id)


def _create_whatsapp_authorization(tenant, *, actor: str) -> channel_connections.CreatedConnectionRequest:
    service = ZernioService()
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
        if account.platform.lower() == "whatsapp"
        and account.profile_id == zernio_profile_id
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
        created = _create_whatsapp_authorization(tenant, actor="nr3-admin")
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
    if tenant is None or not _public_whatsapp_token_valid(tenant.id, token):
        return _result_redirect("failed", tenant_id=tenantId.strip() or None)
    try:
        created = _create_whatsapp_authorization(tenant, actor="tenant-self-service")
    except (ZernioNotConfigured, ZernioAPIError):
        return _result_redirect("failed", tenant_id=tenant.id)
    return RedirectResponse(url=created.request.auth_url or CALLBACK_RESULT_PATH, status_code=303)


@router.get("/tenants/{tenant_id}/channels/whatsapp/status")
def whatsapp_connection_status(tenant_id: str, request: Request) -> dict:
    """Return the safe WhatsApp/Zernio connection state for a tenant."""
    _require_operator_json(request)
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Tenant not found.")

    connection = channel_connections.get_tenant_channel_connection(tenant.id)
    if connection is None or connection.status in {"pending", "not_connected"}:
        connection = _sync_whatsapp_connection_from_zernio(tenant.id) or connection
    if connection is None:
        zernio_profile_id = channel_connections.get_tenant_zernio_profile_id(
            tenant.id
        )
        return {
            "success": True,
            "tenantId": tenant.id,
            "channel": "whatsapp",
            "provider": "zernio",
            "status": "not_connected",
            "connected": False,
            "displayPhoneNumber": None,
            "phoneNumberId": None,
            "providerAccountId": None,
            "zernioProfileId": zernio_profile_id,
            "connectedAt": None,
            "lastUpdatedAt": None,
            "lastError": None,
        }

    return {
        "success": True,
        "tenantId": tenant.id,
        "channel": connection.channel,
        "provider": connection.provider,
        "status": connection.status,
        "connected": connection.status == "connected",
        "displayPhoneNumber": connection.display_phone_number,
        "phoneNumberId": connection.phone_number_id,
        "providerAccountId": connection.zernio_account_id,
        "zernioProfileId": connection.zernio_profile_id,
        "connectedAt": connection.connected_at,
        "lastUpdatedAt": connection.updated_at,
        "lastError": connection.last_error,
    }


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
    if last_request_id:
        channel_connections.update_connection_request(
            last_request_id,
            status="connected",
            zernio_account_id=selected.id,
            selected_phone_number_id=selected.phone_number_id,
            display_phone_number=selected.display_phone_number,
            callback_payload={"selected_via": "operator_phone_selection"},
            error_summary=None,
        )

    connection = channel_connections.upsert_tenant_channel_connection(
        tenant_id=tenant.id,
        status="connected",
        zernio_profile_id=zernio_profile_id,
        zernio_account_id=selected.id,
        phone_number_id=selected.phone_number_id,
        display_phone_number=selected.display_phone_number,
        waba_id=selected.waba_id,
        metadata={"selectedPhone": _safe_phone_option(selected)},
        last_request_id=last_request_id,
        last_error=None,
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
        "connected": True,
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
    here from Meta/Zernio. The random callback state is the trust anchor.
    """
    state_token = _first_query_value(request, "state", "connect_token")
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
    if _is_expired(connection_request):
        channel_connections.update_connection_request(
            connection_request.id,
            status="expired",
            callback_payload=callback_payload,
            error_summary="WhatsApp authorization link expired.",
        )
        channel_connections.upsert_tenant_channel_connection(
            tenant_id=tenant_id,
            status="failed",
            zernio_profile_id=connection_request.zernio_profile_id,
            last_request_id=connection_request.id,
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
        channel_connections.upsert_tenant_channel_connection(
            tenant_id=tenant_id,
            status="failed",
            zernio_profile_id=connection_request.zernio_profile_id,
            last_request_id=connection_request.id,
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

    zernio_account: ZernioAccountSummary | None = None
    if zernio_account_id:
        try:
            zernio_account = ZernioService().get_account(zernio_account_id)
        except (AttributeError, ZernioNotConfigured, ZernioAPIError) as exc:
            logger.warning(
                "whatsapp_connect_callback_account_lookup_failed tenant=%s account=%s error=%s",
                tenant_id,
                zernio_account_id[:20],
                str(exc)[:200],
            )
    if zernio_account is not None:
        phone_number_id = phone_number_id or zernio_account.phone_number_id
        display_phone_number = display_phone_number or _display_phone(zernio_account)
        waba_id = waba_id or zernio_account.waba_id

    if (
        status in PENDING_NUMBER_STATUSES
        or not zernio_account_id
    ):
        channel_connections.update_connection_request(
            connection_request.id,
            status="pending_number",
            zernio_account_id=zernio_account_id,
            selected_phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            callback_payload=callback_payload,
            error_summary=None,
        )
        channel_connections.upsert_tenant_channel_connection(
            tenant_id=tenant_id,
            status="pending",
            zernio_profile_id=connection_request.zernio_profile_id,
            zernio_account_id=zernio_account_id,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            waba_id=waba_id,
            metadata={"callback": callback_payload},
            last_request_id=connection_request.id,
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

    channel_connections.update_connection_request(
        connection_request.id,
        status="connected",
        zernio_account_id=zernio_account_id,
        selected_phone_number_id=phone_number_id,
        display_phone_number=display_phone_number,
        callback_payload=callback_payload,
        error_summary=None,
    )
    if zernio_account is not None:
        _upsert_connected_account(
            tenant_id,
            zernio_account,
            request_id=connection_request.id,
            callback_payload=callback_payload,
        )
    else:
        channel_connections.upsert_tenant_channel_connection(
            tenant_id=tenant_id,
            status="connected",
            zernio_profile_id=connection_request.zernio_profile_id,
            zernio_account_id=zernio_account_id,
            phone_number_id=phone_number_id,
            display_phone_number=display_phone_number,
            waba_id=waba_id,
            metadata={"callback": callback_payload},
            last_request_id=connection_request.id,
            last_error=None,
        )
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


def _tenant_id_for_zernio_account(account_id: str) -> str | None:
    connection = channel_connections.get_tenant_channel_connection_by_account_id(
        account_id
    )
    if connection:
        return connection.tenant_id
    for tenant in list_tenants():
        allowlist = (
            get_tenant_client_data(tenant.id).get("channel_account_allowlist") or {}
        )
        if not isinstance(allowlist, dict):
            continue
        allowed = allowlist.get("zernio_accounts") or []
        if account_id in {str(item) for item in allowed}:
            return tenant.id
    return _sync_tenant_for_zernio_account(account_id)


def _sync_tenant_for_zernio_account(account_id: str) -> str | None:
    """Self-heal webhook routing when Zernio completed but callback state was missed."""
    if not account_id:
        return None
    try:
        accounts = ZernioService().list_accounts(platform="whatsapp")
    except (ZernioNotConfigured, ZernioAPIError) as exc:
        logger.info(
            "zernio_webhook_router_reconcile_unavailable account=%s error=%s",
            account_id[:24],
            str(exc)[:120],
        )
        return None

    matched = next(
        (
            account
            for account in accounts
            if account.id == account_id and _account_is_connected(account)
        ),
        None,
    )
    if matched is None or not matched.profile_id:
        return None

    for tenant in list_tenants():
        if _tenant_zernio_profile_id(tenant.id) != matched.profile_id:
            continue
        _upsert_connected_account(
            tenant.id,
            matched,
            callback_payload={
                "source": "zernio_webhook_router_reconcile",
                "accountId": matched.id,
                "profileId": matched.profile_id,
                "displayPhoneNumber": _display_phone(matched) or "",
            },
        )
        logger.info(
            "zernio_webhook_router_reconciled tenant=%s account=%s",
            tenant.id,
            account_id[:24],
        )
        return tenant.id
    return None


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
    tenant_id = _tenant_id_for_zernio_account(account_id)
    if not tenant_id:
        logger.warning(
            "zernio_webhook_router_unmapped_account account=%s event=%s",
            account_id[:24],
            str(payload.get("event") or "")[:80],
        )
        return PlainTextResponse("OK", status_code=202)

    try:
        status_code, response_text = await _forward_zernio_webhook_to_tenant(
            tenant_id=tenant_id,
            body=body,
            signature=signature,
            content_type=request.headers.get("Content-Type", "application/json"),
        )
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
        status if status in {"success", "pending-number", "failed"} else "failed"
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
