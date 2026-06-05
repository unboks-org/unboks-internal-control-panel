"""Canonical WhatsApp/Zernio health evaluation for Nr3.

Tenant status and WhatsApp channel health are different things. This module
keeps the channel decision in one place so the sidebar, workspace card, status
API, and webhook router do not disagree about whether WhatsApp is safe to use.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import channel_connections
from app.tenants import get_tenant_client_data


PENDING_REQUEST_STATUSES = {
    "pending",
    "link_generated",
    "auth_started",
    "callback_received",
    "pending_number",
}


@dataclass(frozen=True)
class AllowlistHealth:
    ok: bool
    label: str
    summary: str
    accounts: list[str]
    raw_accounts: list[str]


@dataclass(frozen=True)
class WhatsAppHealth:
    status: str
    label: str
    connected: bool
    provider_connected: bool
    badge_class: str
    chip_class: str
    visible: bool
    phone: str
    phone_number_id: str | None
    provider_account_id: str | None
    zernio_profile_id: str | None
    connected_at: str | None
    updated_at: str | None
    last_error: str | None
    allowlist: AllowlistHealth
    repair_available: bool
    action_label: str
    summary: str


def redacted_account_ids(accounts: list[str]) -> list[str]:
    redacted: list[str] = []
    for account in accounts[:4]:
        if len(account) <= 10:
            redacted.append(account[:2] + "..." if account else "")
        else:
            redacted.append(f"{account[:6]}...{account[-4:]}")
    return redacted


def whatsapp_allowlist_health(
    tenant_id: str,
    connected_account_id: str,
) -> AllowlistHealth:
    data = get_tenant_client_data(tenant_id)
    raw = data.get("channel_account_allowlist")
    if not isinstance(raw, dict):
        return AllowlistHealth(
            ok=False,
            label="Missing strict allowlist",
            summary="No channel_account_allowlist found in client.json.",
            accounts=[],
            raw_accounts=[],
        )
    mode = str(raw.get("mode") or "").strip().lower()
    raw_accounts_value = raw.get("zernio_accounts")
    accounts = (
        [str(item).strip() for item in raw_accounts_value if str(item).strip()]
        if isinstance(raw_accounts_value, list)
        else []
    )
    wildcard = any(
        item.lower() in {"*", "all", "any", "wildcard"} for item in accounts
    )
    if mode != "strict":
        return AllowlistHealth(
            ok=False,
            label="Allowlist is not strict",
            summary="channel_account_allowlist.mode must be strict.",
            accounts=redacted_account_ids(accounts),
            raw_accounts=accounts,
        )
    if not accounts:
        return AllowlistHealth(
            ok=False,
            label="Allowlist is empty",
            summary="Strict allowlist has no Zernio account ids.",
            accounts=[],
            raw_accounts=[],
        )
    if wildcard:
        return AllowlistHealth(
            ok=False,
            label="Allowlist is permissive",
            summary="Strict allowlist cannot contain wildcard account ids.",
            accounts=redacted_account_ids(accounts),
            raw_accounts=accounts,
        )
    if connected_account_id and connected_account_id not in accounts:
        return AllowlistHealth(
            ok=False,
            label="Connected account not allowlisted",
            summary="Connected Zernio account id is not present in strict allowlist.",
            accounts=redacted_account_ids(accounts),
            raw_accounts=accounts,
        )
    return AllowlistHealth(
        ok=True,
        label="Strict allowlist",
        summary="Strict Zernio account allowlist is active.",
        accounts=redacted_account_ids(accounts),
        raw_accounts=accounts,
    )


def _allowlist_dict(allowlist: AllowlistHealth) -> dict[str, object]:
    return {
        "ok": allowlist.ok,
        "label": allowlist.label,
        "summary": allowlist.summary,
        "accounts": allowlist.accounts,
    }


def whatsapp_health_to_template(status: WhatsAppHealth) -> dict[str, object]:
    return {
        "status": status.status,
        "label": status.label,
        "badge_class": status.badge_class,
        "chip_class": status.chip_class,
        "visible": status.visible,
        "phone": status.phone,
        "allowlist": _allowlist_dict(status.allowlist),
        "connected": status.connected,
        "provider_connected": status.provider_connected,
        "repair_available": status.repair_available,
        "action_label": status.action_label,
        "summary": status.summary,
    }


def build_whatsapp_health(tenant_id: str) -> WhatsAppHealth:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    latest = channel_connections.get_latest_connection_request_for_tenant(tenant_id)
    client_data = get_tenant_client_data(tenant_id)
    allowlist = whatsapp_allowlist_health(
        tenant_id,
        connection.zernio_account_id if connection and connection.zernio_account_id else "",
    )
    phone = connection.display_phone_number if connection and connection.display_phone_number else ""
    profile_id = (
        connection.zernio_profile_id
        if connection and connection.zernio_profile_id
        else channel_connections.get_tenant_zernio_profile_id(tenant_id)
    )

    if connection and connection.status == "connected":
        if allowlist.ok:
            return WhatsAppHealth(
                status="connected_healthy",
                label="Connected / healthy",
                connected=True,
                provider_connected=True,
                badge_class="tenant-wa-connected",
                chip_class="status-ok",
                visible=True,
                phone=phone,
                phone_number_id=connection.phone_number_id,
                provider_account_id=connection.zernio_account_id,
                zernio_profile_id=connection.zernio_profile_id,
                connected_at=connection.connected_at,
                updated_at=connection.updated_at,
                last_error=connection.last_error,
                allowlist=allowlist,
                repair_available=False,
                action_label="Refresh status",
                summary="WhatsApp is connected and protected by a strict Zernio account allowlist.",
            )
        repair_available = bool(connection.zernio_account_id)
        return WhatsAppHealth(
            status="needs_repair_missing_allowlist",
            label=f"Needs repair: {allowlist.label}",
            connected=False,
            provider_connected=True,
            badge_class="tenant-wa-critical",
            chip_class="status-error",
            visible=True,
            phone=phone,
            phone_number_id=connection.phone_number_id,
            provider_account_id=connection.zernio_account_id,
            zernio_profile_id=connection.zernio_profile_id,
            connected_at=connection.connected_at,
            updated_at=connection.updated_at,
            last_error=connection.last_error,
            allowlist=allowlist,
            repair_available=repair_available,
            action_label=(
                "Repair allowlist from verified Zernio account"
                if repair_available
                else "Generate new WhatsApp connection link"
            ),
            summary=(
                "Zernio has a connected account for this tenant, but runtime routing is not allowed "
                "until the strict account allowlist matches it."
            ),
        )

    if (
        (connection and connection.status == "pending")
        or (latest and latest.status in PENDING_REQUEST_STATUSES)
        or client_data.get("whatsapp_connect_token")
    ):
        return WhatsAppHealth(
            status="connection_pending",
            label="Connection pending",
            connected=False,
            provider_connected=False,
            badge_class="tenant-wa-pending",
            chip_class="status-warn",
            visible=True,
            phone=phone,
            phone_number_id=connection.phone_number_id if connection else None,
            provider_account_id=connection.zernio_account_id if connection else None,
            zernio_profile_id=profile_id,
            connected_at=connection.connected_at if connection else None,
            updated_at=connection.updated_at if connection else (latest.updated_at if latest else None),
            last_error=None,
            allowlist=allowlist,
            repair_available=False,
            action_label="Refresh status",
            summary="A WhatsApp authorization link exists or authorization is waiting for completion.",
        )

    if connection and connection.status == "failed":
        return WhatsAppHealth(
            status="needs_reconnect_authorization_failed",
            label="Needs reconnect: authorization failed",
            connected=False,
            provider_connected=False,
            badge_class="tenant-wa-failed",
            chip_class="status-error",
            visible=True,
            phone=phone,
            phone_number_id=connection.phone_number_id,
            provider_account_id=connection.zernio_account_id,
            zernio_profile_id=connection.zernio_profile_id,
            connected_at=connection.connected_at,
            updated_at=connection.updated_at,
            last_error=connection.last_error,
            allowlist=allowlist,
            repair_available=False,
            action_label="Generate new WhatsApp connection link",
            summary=connection.last_error or "The last WhatsApp authorization attempt failed.",
        )

    return WhatsAppHealth(
        status="not_connected",
        label="Not connected",
        connected=False,
        provider_connected=False,
        badge_class="tenant-wa-muted",
        chip_class="status-unknown",
        visible=False,
        phone="",
        phone_number_id=None,
        provider_account_id=None,
        zernio_profile_id=profile_id,
        connected_at=None,
        updated_at=None,
        last_error=None,
        allowlist=allowlist,
        repair_available=False,
        action_label="Generate new WhatsApp connection link",
        summary="Generate a secure link when the client is ready to authorize WhatsApp.",
    )


def whatsapp_health_to_api(status: WhatsAppHealth, tenant_id: str) -> dict[str, object]:
    return {
        "success": True,
        "tenantId": tenant_id,
        "channel": "whatsapp",
        "provider": "zernio",
        "status": status.status,
        "label": status.label,
        "connected": status.connected,
        "providerConnected": status.provider_connected,
        "displayPhoneNumber": status.phone or None,
        "phoneNumberId": status.phone_number_id,
        "providerAccountId": status.provider_account_id,
        "zernioProfileId": status.zernio_profile_id,
        "connectedAt": status.connected_at,
        "lastUpdatedAt": status.updated_at,
        "lastError": status.last_error,
        "allowlist": _allowlist_dict(status.allowlist),
        "repairAvailable": status.repair_available,
        "actionLabel": status.action_label,
        "summary": status.summary,
    }
