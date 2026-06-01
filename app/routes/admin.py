from fastapi import APIRouter, Form, Request, File, UploadFile
from starlette.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates
from typing import Optional

from app.config import get_settings
from app.emailer import (
    EmailSendResult,
    build_onboarding_link,
    prepare_or_send_onboarding_email,
)
from app.onboarding import (
    INTAKE_QUESTIONS,
    LeadInput,
    LeadNotFoundError,
    LeadValidationError,
    build_setup_summary,
    clean_optional,
    create_lead,
    create_or_refresh_token,
    get_lead,
    list_intake_answers,
    list_intake_answer_counts,
    list_leads,
    set_review_decision,
)
from app.public_signup_requests import (
    get_signup_request,
    is_archived_signup,
    list_signup_requests,
    mark_provisioned,
    update_signup_request,
)
from app.signup_service import create_public_signup_tenant
from app import audit_log
from app import todos as todo_store
from app.security import (
    clear_session_cookie,
    create_session_value,
    require_admin,
    set_session_cookie,
    verify_admin_password,
)
from app.provisioning import auto_provision_tenant, queue_tenant_host_action
from app.nr2_sync import (
    fetch_auto_block_settings,
    fetch_nr2_knowledge,
    update_auto_block_settings,
)
from app.port_registry import PortRegistryError, reserve_tenant_port
from app.prompt_conflicts import (
    build_prompt_conflict_report,
    dangerous_candidate_conflicts,
    mark_reviewed,
)
from app.tenants import (
    ESCALATION_MODES,
    NOTE_PRIORITIES,
    Tenant,
    TenantCreateError,
    derive_slug_from_name,
    get_tenant,
    get_tenant_client_data,
    list_tenants,
    register_tenant,
    sorted_notes,
    tenant_account_details,
    update_tenant_account_details,
    update_tenant_status,
    validate_slug,
    RESERVED_SLUGS,
)

import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus


logger = logging.getLogger(__name__)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


REVIEW_AWAITING_STATUSES = {"form_submitted"}
REVIEW_DECIDED_STATUSES = {"review_needs_changes", "review_approved", "tenant_ready"}

AGENT_FEATURE_ACTIONS: dict[str, str] = {
    "learning-from-operator-answers": "learning_from_operator",
}


def _shell_context(active: str, active_tenant: Optional[Tenant] = None) -> dict:
    """Context every admin template needs so the sidebar renders."""
    tenants = list_tenants()
    return {
        "active": active,
        "tenants": tenants,
        "active_tenant": active_tenant,
        "tenant_whatsapp_statuses": _tenant_whatsapp_statuses(tenants),
    }


def _tenant_whatsapp_statuses(tenants: tuple[Tenant, ...]) -> dict[str, dict]:
    from app import channel_connections

    statuses: dict[str, dict] = {}
    pending_request_statuses = {
        "pending",
        "link_generated",
        "auth_started",
        "callback_received",
        "pending_number",
    }
    for tenant in tenants:
        connection = channel_connections.get_tenant_channel_connection(tenant.id)
        latest = channel_connections.get_latest_connection_request_for_tenant(
            tenant.id
        )
        if connection and connection.status == "connected":
            allowlist = _whatsapp_allowlist_status(
                tenant.id,
                connection.zernio_account_id or "",
            )
            if not allowlist["ok"]:
                statuses[tenant.id] = {
                    "status": "connected_unprotected",
                    "label": f"Critical: {allowlist['label']}",
                    "badge_class": "tenant-wa-critical",
                    "chip_class": "status-error",
                    "visible": True,
                    "phone": connection.display_phone_number or "",
                    "allowlist": allowlist,
                }
                continue
            statuses[tenant.id] = {
                "status": "connected",
                "label": "Connected",
                "badge_class": "tenant-wa-connected",
                "chip_class": "status-ok",
                "visible": True,
                "phone": connection.display_phone_number or "",
                "allowlist": allowlist,
            }
        elif (
            (connection and connection.status == "pending")
            or (latest and latest.status in pending_request_statuses)
        ):
            statuses[tenant.id] = {
                "status": "pending",
                "label": "Awaiting activation",
                "badge_class": "tenant-wa-pending",
                "chip_class": "status-warn",
                "visible": True,
                "phone": (
                    connection.display_phone_number
                    if connection and connection.display_phone_number
                    else ""
                ),
                "allowlist": _whatsapp_allowlist_status(tenant.id, ""),
            }
        elif get_tenant_client_data(tenant.id).get("whatsapp_connect_token"):
            statuses[tenant.id] = {
                "status": "awaiting_activation",
                "label": "Awaiting activation",
                "badge_class": "tenant-wa-pending",
                "chip_class": "status-warn",
                "visible": True,
                "phone": "",
                "allowlist": _whatsapp_allowlist_status(tenant.id, ""),
            }
        elif connection and connection.status == "failed":
            statuses[tenant.id] = {
                "status": "failed",
                "label": "Failed",
                "badge_class": "tenant-wa-failed",
                "chip_class": "status-error",
                "visible": True,
                "phone": connection.display_phone_number or "",
                "allowlist": _whatsapp_allowlist_status(tenant.id, ""),
            }
        else:
            statuses[tenant.id] = {
                "status": "not_connected",
                "label": "Not connected",
                "badge_class": "tenant-wa-muted",
                "chip_class": "status-unknown",
                "visible": False,
                "phone": "",
                "allowlist": _whatsapp_allowlist_status(tenant.id, ""),
            }
    return statuses


def _whatsapp_allowlist_status(
    tenant_id: str,
    connected_account_id: str,
) -> dict[str, object]:
    data = get_tenant_client_data(tenant_id)
    raw = data.get("channel_account_allowlist")
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "label": "Missing strict allowlist",
            "summary": "No channel_account_allowlist found in client.json.",
            "accounts": [],
        }
    mode = str(raw.get("mode") or "").strip().lower()
    raw_accounts = raw.get("zernio_accounts")
    accounts = (
        [str(item).strip() for item in raw_accounts if str(item).strip()]
        if isinstance(raw_accounts, list)
        else []
    )
    wildcard = any(
        item.lower() in {"*", "all", "any", "wildcard"} for item in accounts
    )
    if mode != "strict":
        return {
            "ok": False,
            "label": "Allowlist is not strict",
            "summary": "channel_account_allowlist.mode must be strict.",
            "accounts": _redacted_account_ids(accounts),
        }
    if not accounts:
        return {
            "ok": False,
            "label": "Allowlist is empty",
            "summary": "Strict allowlist has no Zernio account ids.",
            "accounts": [],
        }
    if wildcard:
        return {
            "ok": False,
            "label": "Allowlist is permissive",
            "summary": "Strict allowlist cannot contain wildcard account ids.",
            "accounts": _redacted_account_ids(accounts),
        }
    if connected_account_id and connected_account_id not in accounts:
        return {
            "ok": False,
            "label": "Connected account not allowlisted",
            "summary": "Connected Zernio account id is not present in strict allowlist.",
            "accounts": _redacted_account_ids(accounts),
        }
    return {
        "ok": True,
        "label": "Strict allowlist",
        "summary": "Strict Zernio account allowlist is active.",
        "accounts": _redacted_account_ids(accounts),
    }


def _redacted_account_ids(accounts: list[str]) -> list[str]:
    redacted: list[str] = []
    for account in accounts[:4]:
        if len(account) <= 10:
            redacted.append(account[:2] + "..." if account else "")
        else:
            redacted.append(f"{account[:6]}...{account[-4:]}")
    return redacted


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(default="")) -> Response:
    settings = get_settings()
    if not settings.admin_password:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Admin password is not configured.",
            },
            status_code=500,
        )
    if not verify_admin_password(password, settings):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid password."},
            status_code=401,
        )

    response = RedirectResponse(url="/admin/tenants", status_code=303)
    set_session_cookie(response, create_session_value(settings), settings)
    return response


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


# ---------------------------------------------------------------------------
# Admin shell pages
# ---------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin_root(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/admin/tenants", response_class=HTMLResponse)
def admin_tenants_index(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenants = list_tenants()
    if tenants:
        return RedirectResponse(url=f"/admin/tenants/{tenants[0].id}", status_code=303)
    return RedirectResponse(url="/admin/settings", status_code=303)



@router.post("/admin/tenants/{tenant_id}/channels/{channel}/toggle")
def admin_toggle_channel(
    request: Request, tenant_id: str, channel: str,
) -> Response:
    """Flip one channel's on/off state for one tenant.

    The state is written to Nr3 channel storage and mirrored into the ICP
    override envelope consumed by Nr2.
    """
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    from app import channel_state as _channel_state
    _channel_state.toggle_channel(tenant_id, channel)
    return RedirectResponse(
        url=f"/admin/tenants/{tenant_id}#channels-section",
        status_code=303,
    )


def _workspace_redirect(
    tenant_id: str,
    anchor: str = "tenant-header-anchor",
    *,
    message: str = "",
    level: str = "ok",
) -> RedirectResponse:
    suffix = f"#{anchor}" if anchor else ""
    query = ""
    if message:
        query = (
            f"?action_message={quote_plus(message)}"
            f"&action_level={quote_plus(level)}"
        )
    return RedirectResponse(
        url=f"/admin/tenants/{tenant_id}{query}{suffix}",
        status_code=303,
    )


@router.post("/admin/tenants/{tenant_id}/agent/{feature}/toggle")
def admin_toggle_agent_feature(
    request: Request,
    tenant_id: str,
    feature: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    feature_key = AGENT_FEATURE_ACTIONS.get(feature)
    if not feature_key:
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message="Unknown AI Agent control.",
            level="warn",
        )
    from app import icp_overrides
    toggles = icp_overrides.feature_toggles_for_tenant(tenant_id)
    current = toggles.get(feature_key, {}).get("value")
    if current is None:
        defaults = {
            "agent_replies_enabled": tenant.agent.replies_enabled,
            "ai_auto_reply": tenant.agent.auto_reply_enabled,
            "learning_from_operator": tenant.agent.learning_enabled,
        }
        current = defaults.get(feature_key, False)
    next_value = not bool(current)
    icp_overrides.set_feature_toggle(tenant_id, feature_key, next_value)
    label = feature.replace("-", " ")
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message=f"{label} set to {'ON' if next_value else 'OFF'}.",
    )


@router.post("/admin/tenants/{tenant_id}/agent/tone")
def admin_save_agent_tone(
    request: Request,
    tenant_id: str,
    tone: str = Form(default=""),
    tone_notes: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    candidate = "\n".join(part for part in (tone, tone_notes) if part.strip())
    conflicts = dangerous_candidate_conflicts(
        tenant_id,
        name="Pending Nr3 tone override",
        text=candidate,
        priority="tone_style",
    )
    if conflicts:
        return _workspace_redirect(
            tenant_id,
            "prompt-conflicts-section",
            message=f"Tone override not saved: {conflicts[0].title}.",
            level="warn",
        )
    from app import icp_overrides
    icp_overrides.set_ai_tone(
        tenant_id,
        tone,
        notes=tone_notes,
    )
    clean_tone = (tone or "").strip()
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message="Tone override saved." if clean_tone else "Tone override cleared.",
    )


@router.post("/admin/tenants/{tenant_id}/agent/escalation-rules")
def admin_save_agent_escalation_rules(
    request: Request,
    tenant_id: str,
    soft_escalation_when: str = Form(default=""),
    hard_escalation_when: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import icp_overrides
    icp_overrides.set_escalation_rules(
        tenant_id,
        soft_when=soft_escalation_when,
        hard_when=hard_escalation_when,
    )
    has_rules = bool(
        (soft_escalation_when or "").strip()
        or (hard_escalation_when or "").strip()
    )
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message=(
            "Escalation rules override saved."
            if has_rules
            else "Escalation rules override cleared."
        ),
    )


@router.post("/admin/tenants/{tenant_id}/agent/name")
def admin_save_agent_name_override(
    request: Request,
    tenant_id: str,
    agent_name: str = Form(default=""),
    clear_override: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import icp_overrides
    if clear_override:
        icp_overrides.set_agent_name_override(tenant_id, "")
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message="AI Agent name override cleared.",
        )
    from app.agent_identity import validate_agent_name
    try:
        clean_name = validate_agent_name(agent_name)
    except ValueError as exc:
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message=str(exc),
            level="warn",
        )
    icp_overrides.set_agent_name_override(tenant_id, clean_name)
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message="AI Agent name override saved.",
    )


@router.post("/admin/tenants/{tenant_id}/agent/response-timing")
def admin_save_response_timing_override(
    request: Request,
    tenant_id: str,
    mode: str = Form(default="preset"),
    preset: str = Form(default="balanced"),
    delay_seconds: str = Form(default="12"),
    max_wait_seconds: str = Form(default="25"),
    custom_delay_seconds: str = Form(default="12"),
    random_min_seconds: str = Form(default="5"),
    random_max_seconds: str = Form(default="25"),
    batching_enabled: str = Form(default=""),
    clear_override: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import icp_overrides
    if clear_override:
        icp_overrides.set_response_timing_override(tenant_id, clear=True)
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message="Response timing override cleared.",
        )
    try:
        delay = float(delay_seconds)
        max_wait = float(max_wait_seconds)
        custom_delay = float(custom_delay_seconds)
        random_min = float(random_min_seconds)
        random_max = float(random_max_seconds)
    except ValueError:
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message="Response timing values must be numbers.",
            level="warn",
        )
    icp_overrides.set_response_timing_override(
        tenant_id,
        enabled=(batching_enabled == "on"),
        mode=mode,
        preset=preset,
        delay_seconds=delay,
        max_wait_seconds=max_wait,
        custom_delay_seconds=custom_delay,
        random_min_seconds=random_min,
        random_max_seconds=random_max,
    )
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message="Response timing override saved.",
    )


@router.post("/admin/tenants/{tenant_id}/auto-block")
def admin_save_auto_block_settings(
    request: Request,
    tenant_id: str,
    enabled: Optional[str] = Form(default=None),
    hate_speech: Optional[str] = Form(default=None),
    severe_insult: Optional[str] = Form(default=None),
    threat: Optional[str] = Form(default=None),
    sexual_harassment: Optional[str] = Form(default=None),
    fraud_scam: Optional[str] = Form(default=None),
    severe_abuse: Optional[str] = Form(default=None),
    repeated_profanity_enabled: Optional[str] = Form(default=None),
    repeated_profanity_threshold: int = Form(default=3),
    warn_before_block: Optional[str] = Form(default=None),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    payload = {
        "enabled": enabled == "on",
        "zero_tolerance": {
            "hate_speech": hate_speech == "on",
            "severe_insult": severe_insult == "on",
            "threat": threat == "on",
            "sexual_harassment": sexual_harassment == "on",
            "fraud_scam": fraud_scam == "on",
            "severe_abuse": severe_abuse == "on",
        },
        "repeated_profanity": {
            "enabled": repeated_profanity_enabled == "on",
            "threshold": repeated_profanity_threshold,
            "warn_before_block": warn_before_block == "on",
        },
        "final_block_notice_enabled": False,
    }
    sync = update_auto_block_settings(tenant_id, payload)
    from app import audit_log
    audit_log.record_event(
        actor="nr3-admin",
        tenant_id=tenant_id,
        action="auto_block_settings_updated",
        result="success" if sync.ok else "failed",
        safe_summary="Auto-block settings updated." if sync.ok else sync.error,
        metadata={"source_url": sync.source_url},
    )
    return _workspace_redirect(
        tenant_id,
        "auto-block-section",
        message="Auto-block settings saved." if sync.ok else f"Auto-block settings not saved: {sync.error}",
        level="ok" if sync.ok else "warn",
    )


@router.post("/admin/tenants/{tenant_id}/sot")
def admin_add_sot_entry(
    request: Request,
    tenant_id: str,
    title: str = Form(default=""),
    category: str = Form(default="general"),
    content: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    conflicts = dangerous_candidate_conflicts(
        tenant_id,
        name="Pending Nr3 Source of Truth entry",
        text=content,
        priority="sot_company_facts",
    )
    if conflicts:
        return _workspace_redirect(
            tenant_id,
            "prompt-conflicts-section",
            message=f"Source of Truth not saved: {conflicts[0].title}.",
            level="warn",
        )
    from app import icp_overrides
    try:
        icp_overrides.add_sot_entry(
            tenant_id,
            title=title,
            category=category,
            content=content,
        )
    except ValueError as exc:
        return _workspace_redirect(
            tenant_id,
            "agent-section",
            message=str(exc),
            level="warn",
        )
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message="Source of Truth entry added.",
    )


@router.post("/admin/tenants/{tenant_id}/sot/{entry_id}/delete")
def admin_delete_sot_entry(
    request: Request,
    tenant_id: str,
    entry_id: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import icp_overrides
    deleted = icp_overrides.delete_sot_entry(tenant_id, entry_id)
    return _workspace_redirect(
        tenant_id,
        "agent-section",
        message="Source of Truth entry deleted." if deleted else "Source of Truth entry was not found.",
        level="ok" if deleted else "warn",
    )


@router.post("/admin/tenants/{tenant_id}/notes")
def admin_add_tenant_note(
    request: Request,
    tenant_id: str,
    body: str = Form(default=""),
    priority: str = Form(default="normal"),
    follow_up_date: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import tenant_notes
    try:
        tenant_notes.add_note(
            tenant_id,
            body,
            priority=priority,
            follow_up_date=follow_up_date,
        )
    except ValueError as exc:
        return _workspace_redirect(
            tenant_id,
            "notes-section",
            message=str(exc),
            level="warn",
        )
    return _workspace_redirect(
        tenant_id,
        "notes-section",
        message="Tenant note added.",
    )


@router.post("/admin/tenants/{tenant_id}/notes/{note_id}/pin")
def admin_toggle_tenant_note_pin(
    request: Request,
    tenant_id: str,
    note_id: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    from app import tenant_notes
    changed = tenant_notes.toggle_pin(tenant_id, note_id)
    return _workspace_redirect(
        tenant_id,
        "notes-section",
        message="Note pin updated." if changed else "Note was not found.",
        level="ok" if changed else "warn",
    )


@router.post("/admin/tenants/{tenant_id}/notes/{note_id}/follow-up-done")
def admin_mark_tenant_note_follow_up_done(
    request: Request,
    tenant_id: str,
    note_id: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    from app import tenant_notes
    changed = tenant_notes.mark_follow_up_done(tenant_id, note_id)
    return _workspace_redirect(
        tenant_id,
        "notes-section",
        message="Follow-up marked done." if changed else "Note was not found.",
        level="ok" if changed else "warn",
    )


@router.post("/admin/tenants/{tenant_id}/details")
def admin_update_tenant_details(
    request: Request,
    tenant_id: str,
    name: str = Form(default=""),
    contact_person: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
    website: str = Form(default=""),
    address: str = Form(default=""),
    logo_url: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    clean_name = (name or "").strip()
    if not clean_name:
        return _workspace_redirect(
            tenant_id,
            "tenant-details-section",
            message="Business name is required.",
            level="warn",
        )
    clean_email = (email or "").strip()
    if clean_email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", clean_email):
        return _workspace_redirect(
            tenant_id,
            "tenant-details-section",
            message="Enter a valid contact email or leave it empty.",
            level="warn",
        )
    try:
        update_tenant_account_details(
            tenant_id,
            name=clean_name,
            contact_person=contact_person,
            email=clean_email,
            phone=phone,
            website=website,
            address=address,
            logo_url=logo_url,
        )
    except TenantCreateError as exc:
        return _workspace_redirect(
            tenant_id,
            "tenant-details-section",
            message=str(exc),
            level="warn",
        )
    except OSError as exc:
        logger.warning("tenant_details.save_failed slug=%s err=%r", tenant_id, exc)
        return _workspace_redirect(
            tenant_id,
            "tenant-details-section",
            message="Tenant details could not be saved.",
            level="warn",
        )
    return _workspace_redirect(
        tenant_id,
        "tenant-details-section",
        message="Tenant details saved.",
        level="ok",
    )


@router.post("/admin/tenants/{tenant_id}/suspend")
def admin_suspend_tenant(
    request: Request,
    tenant_id: str,
    confirmation: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    if tenant_id in RESERVED_SLUGS:
        return _workspace_redirect(
            tenant_id,
            "danger-section",
            message="The Unboks master tenant cannot be made inactive from Nr 3.",
            level="warn",
        )
    expected = f"suspend {tenant_id}"
    if (confirmation or "").strip() != expected:
        return _workspace_redirect(
            tenant_id,
            "danger-section",
            message=f"Type exactly '{expected}' to suspend this tenant.",
            level="warn",
        )
    from app import channel_state, icp_overrides
    channel_state.set_all_channels(tenant_id, False)
    for feature_key in (
        "agent_replies_enabled",
        "ai_auto_reply",
        "learning_from_operator",
        "tenant_suspended",
    ):
        icp_overrides.set_feature_toggle(
            tenant_id,
            feature_key,
            feature_key == "tenant_suspended",
        )
    result = queue_tenant_host_action(
        slug=tenant_id,
        action="suspend_tenant",
        dashboard_url=f"https://dashboard.unboks.org/login?workspace={tenant_id}",
    )
    try:
        update_tenant_status(tenant_id, "inactive")
    except Exception as exc:
        logger.warning("tenant_suspend.status_update_failed slug=%s err=%r", tenant_id, exc)
    if result.status == "succeeded":
        message = "Tenant is inactive: channels and AI disabled, container stopped."
        level = "ok"
    elif result.status in {"queued", "disabled"}:
        message = (
            "Tenant bridge overrides were set inactive. Host container stop is "
            f"{result.status}: {result.message}"
        )
        level = "warn"
    else:
        message = (
            "Tenant bridge overrides were set inactive, but host container stop "
            f"failed: {result.message}"
        )
        level = "warn"
    return _workspace_redirect(
        tenant_id,
        "danger-section",
        message=message,
        level=level,
    )


@router.post("/admin/tenants/{tenant_id}/unpause")
def admin_unpause_tenant(
    request: Request,
    tenant_id: str,
    confirmation: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    if tenant_id in RESERVED_SLUGS:
        return _workspace_redirect(
            tenant_id,
            "danger-section",
            message="The Unboks master tenant cannot be changed from Nr 3.",
            level="warn",
        )
    expected = f"unpause {tenant_id}"
    if (confirmation or "").strip() != expected:
        return _workspace_redirect(
            tenant_id,
            "danger-section",
            message=f"Type exactly '{expected}' to unpause this tenant.",
            level="warn",
        )
    from app import channel_state, icp_overrides
    channel_state.set_all_channels(tenant_id, True)
    for feature_key in (
        "agent_replies_enabled",
        "ai_auto_reply",
        "learning_from_operator",
        "tenant_suspended",
    ):
        icp_overrides.set_feature_toggle(
            tenant_id,
            feature_key,
            feature_key != "tenant_suspended",
        )
    result = queue_tenant_host_action(
        slug=tenant_id,
        action="unpause_tenant",
        dashboard_url=f"https://dashboard.unboks.org/login?workspace={tenant_id}",
    )
    try:
        update_tenant_status(tenant_id, "active")
    except Exception as exc:
        logger.warning("tenant_unpause.status_update_failed slug=%s err=%r", tenant_id, exc)
    if result.status == "succeeded":
        message = "Tenant is active again: channels and AI restored, container started."
        level = "ok"
    elif result.status in {"queued", "disabled"}:
        message = (
            "Tenant bridge overrides were set active. Host container start is "
            f"{result.status}: {result.message}"
        )
        level = "warn"
    else:
        message = (
            "Tenant bridge overrides were set active, but host container start "
            f"failed: {result.message}"
        )
        level = "warn"
    return _workspace_redirect(
        tenant_id,
        "danger-section",
        message=message,
        level=level,
    )


@router.post("/admin/tenants/{tenant_id}/password-reset/send")
def admin_send_tenant_password_reset(
    request: Request,
    tenant_id: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)

    from app.password_recovery import request_reset
    from app.tenants import tenant_contact_details

    contact = tenant_contact_details(tenant_id)
    email = contact.get("email", "")
    if not email:
        return _workspace_redirect(
            tenant_id,
            "danger-section",
            message="No tenant contact email is configured.",
            level="warn",
        )
    request_reset(
        tenant_id=tenant_id,
        email=email,
        ip_address=request.client.host if request.client else "internal_admin",
        settings=settings,
        actor="internal_admin",
    )
    return _workspace_redirect(
        tenant_id,
        "danger-section",
        message=f"Password reset email requested for {email}.",
        level="ok",
    )


@router.post("/admin/tenants/import", response_class=HTMLResponse)
def admin_tenant_import_existing(
    request: Request,
    slug: str = Form(default=""),
    name: str = Form(default=""),
    status: str = Form(default="active"),
) -> Response:
    """Register an existing tenant in the ICP sidebar.

    This is for tenants already provisioned on the VPS when Nr3 cannot
    directly read that VPS filesystem. It does not create credentials,
    touch runtime files, or deploy anything.
    """
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect

    candidate_slug = (slug or "").strip()
    try:
        safe_slug = validate_slug(candidate_slug)
    except TenantCreateError as exc:
        return _create_error_response(
            request,
            str(exc),
            form_echo={
                "existing_slug": slug,
                "existing_name": name,
                "existing_status": status,
            },
        )

    display_name = (name or "").strip() or safe_slug
    normalized_status = (status or "active").strip().lower()
    if normalized_status not in ("active", "inactive"):
        normalized_status = "inactive"
    register_tenant({
        "slug": safe_slug,
        "name": display_name,
        "status": normalized_status,
    })
    logger.info("tenant_import.registry_written slug=%s", safe_slug)
    return RedirectResponse(url=f"/admin/tenants/{safe_slug}", status_code=303)



@router.get("/admin/tenants/new", response_class=HTMLResponse)
def admin_tenant_create_form(request: Request) -> Response:
    """Add-New-Tenant wizard. One page, one submit. Posts to
    /admin/tenants/create which creates the folder, writes client.json,
    saves uploaded files, and (optionally) sends the welcome email."""
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_tenant_create.html",
        {
            **_shell_context("tenant_create"),
            "error": request.query_params.get("error", ""),
            "form": {},
        },
    )


@router.post("/admin/tenants/create", response_class=HTMLResponse)
async def admin_tenant_create_submit(
    request: Request,
    name: str = Form(default=""),
    slug: str = Form(default=""),
    contact_person: str = Form(default=""),
    contact_email: str = Form(default=""),
    phone: str = Form(default=""),
    status: str = Form(default="active"),
    tone: str = Form(default=""),
    notes: str = Form(default=""),
    send_welcome: str = Form(default=""),
    files: list[UploadFile] = File(default=[]),
) -> Response:
    """J3-BE-50 -- Manual Mode tenant creation.

    Validates name + slug, generates a server-side initial token,
    builds a flat client.json the operator can copy or download,
    and (optionally) sends the welcome email. Does NOT write to
    local disk; does NOT call any provisioning service. The
    operator manually places the JSON at
    <NR3_TENANTS_CLIENT_DIR>/<slug>/config/client.json on the VPS.

    Renders the success page with a 200 + the JSON block + the
    Copy and Download controls. On validation failure re-renders
    the wizard form with the inline error and pre-filled values.
    Form `files` are accepted (so the existing form HTML keeps
    submitting cleanly) but ignored -- Manual Mode does not store
    uploads.
    """
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect

    logger.info(
        "tenant_create.received slug_raw=%r name_raw=%r files=%d send_welcome=%s",
        slug, name, len(files or []), bool(send_welcome.strip()))

    name = (name or "").strip()
    if not name:
        logger.warning("tenant_create.invalid reason=name_missing")
        return _create_error_response(
            request, "Business / tenant name is required.",
            form_echo=locals())

    candidate_slug = (slug or "").strip() or derive_slug_from_name(name)
    try:
        safe_slug = validate_slug(candidate_slug)
    except TenantCreateError as exc:
        logger.warning(
            "tenant_create.invalid reason=bad_slug candidate=%r err=%s",
            candidate_slug, exc)
        return _create_error_response(request, str(exc), form_echo=locals())

    # Initial sign-in token (operator dashboard login). URL-safe so
    # it pastes cleanly into an email body without escaping.
    initial_token = secrets.token_urlsafe(12)
    # Per-tenant access_key the wtyj-agent backend uses to identify
    # the dashboard's API calls. Generated separately from the
    # dashboard password so they can rotate independently.
    access_key = secrets.token_urlsafe(24)
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    whatsapp_connect_token = secrets.token_urlsafe(32)
    whatsapp_connect_token_expires_at = (
        datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=30)
    ).isoformat()
    dashboard_url = f"https://dashboard.unboks.org/login?workspace={safe_slug}"

    try:
        host_port = reserve_tenant_port(safe_slug)
    except PortRegistryError as exc:
        logger.warning(
            "tenant_create.invalid reason=port_allocation_failed slug=%s err=%s",
            safe_slug, exc)
        return _create_error_response(request, str(exc), form_echo=locals())

    # Manual-Mode client.json payload. Flat shape per J3-BE-50, plus
    # the new access_key field.
    client_data: dict = {
        "slug": safe_slug,
        "name": name,
        "password": initial_token,
        "access_key": access_key,
        "whatsapp_connect_token": whatsapp_connect_token,
        "whatsapp_connect_token_expires_at": whatsapp_connect_token_expires_at,
        "status": "active" if status.strip().lower() == "active" else "inactive",
        "created_at": created_at,
        "host_port": host_port,
    }
    if contact_person.strip():
        client_data["contact_person"] = contact_person.strip()
    if contact_email.strip():
        client_data["email"] = contact_email.strip()
    if phone.strip():
        client_data["whatsapp"] = phone.strip()
    if tone.strip():
        client_data["agent_tone"] = tone.strip()
    if notes.strip():
        client_data["notes"] = notes.strip()

    logger.info(
        "tenant_create.client_json_built slug=%s fields=%d",
        safe_slug, len(client_data))

    # Persist the flat client.json under NR3_TENANTS_CLIENT_DIR so the
    # sidebar's list_tenants() picks the new tenant up on the next
    # render. The downloaded JSON the operator places on the VPS is
    # IDENTICAL to the file written here -- same shape, same bytes.
    # Refuse to overwrite an existing slug: a duplicate submit would
    # otherwise silently regenerate the password and destroy the
    # paper trail the operator already copied.
    # Resolve the tenants root from the env var, falling back to the
    # same default list_tenants() reads from. mkdir -p the directory
    # if it doesn't exist yet -- silently-skipping when the dir is
    # missing was the J3 sidebar-list bug ("only 1 tenant" on a fresh
    # Replit deploy where /opt/wtyj/clients can't be created).
    from app.tenants import _DEFAULT_TENANTS_CLIENT_DIR
    root = (os.environ.get("NR3_TENANTS_CLIENT_DIR")
            or _DEFAULT_TENANTS_CLIENT_DIR).strip()
    skip_prewrite_for_worker = (
        os.environ.get("NR3_AUTO_PROVISION", "").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "tenant_create.disk_skipped slug=%s reason=root_mkdir_failed err=%r",
            safe_slug, exc)
        root = ""

    if root and not skip_prewrite_for_worker:
        tenant_dir = os.path.join(root, safe_slug)
        config_path = os.path.join(tenant_dir, "config", "client.json")
        if os.path.exists(tenant_dir):
            logger.warning(
                "tenant_create.duplicate_slug slug=%s path=%s",
                safe_slug, tenant_dir)
            return _create_error_response(
                request,
                f"A tenant folder for slug {safe_slug!r} already exists. "
                f"Delete or rename it first if you really want to recreate.",
                form_echo=locals())
        try:
            os.makedirs(os.path.join(tenant_dir, "config"))
            os.makedirs(os.path.join(tenant_dir, "data"))
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(client_data, f, indent=2, ensure_ascii=False)
            logger.info(
                "tenant_create.disk_written slug=%s path=%s", safe_slug, config_path)
        except OSError as exc:
            logger.warning(
                "tenant_create.disk_failed slug=%s err=%r", safe_slug, exc)
            # Render the success page anyway -- the operator still gets
            # the JSON to copy/download, they can place it manually.

    try:
        from app.tenants import register_tenant
        register_tenant(client_data)
        logger.info("tenant_create.registry_written slug=%s", safe_slug)
    except OSError as exc:
        logger.warning(
            "tenant_create.registry_failed slug=%s err=%r",
            safe_slug,
            exc,
        )

    # Welcome-email step. send_welcome is the checkbox value; we
    # also need a contact_email to send anywhere.
    welcome_status = "unchecked"
    welcome_error = ""
    wants_welcome = bool(send_welcome.strip())
    contact_email_clean = contact_email.strip()
    if wants_welcome and not contact_email_clean:
        welcome_status = "skipped_no_email"
        logger.warning(
            "tenant_create.welcome_skipped slug=%s reason=no_contact_email",
            safe_slug)
    elif wants_welcome:
        from app.emailer import (build_tenant_welcome_email, send_email,
                                  smtp_is_configured)
        if not smtp_is_configured(settings):
            welcome_status = "no_smtp"
            logger.warning(
                "tenant_create.welcome_skipped slug=%s reason=smtp_not_configured",
                safe_slug)
        else:
            draft = build_tenant_welcome_email(
                tenant_name=name,
                dashboard_url=dashboard_url,
                username=safe_slug,
                initial_token=initial_token,
            )
            try:
                send_email(
                    contact_email_clean,
                    draft.subject,
                    draft.body,
                    settings,
                )
                welcome_status = "sent"
                logger.info(
                    "tenant_create.welcome_sent slug=%s to=%s",
                    safe_slug, contact_email_clean)
            except Exception as exc:
                welcome_status = "failed"
                welcome_error = str(exc)
                logger.warning(
                    "tenant_create.welcome_failed slug=%s exc=%r",
                    safe_slug, exc)

    logger.info(
        "tenant_create.success slug=%s welcome=%s",
        safe_slug, welcome_status)

    # ===== Provisioning artifacts the operator pastes on the VPS =====
    client_json_text = json.dumps(client_data, indent=2, ensure_ascii=False)

    platform_env_text = (
        f"# platform.env for tenant {safe_slug}\n"
        f"# Generated by Nr3 at {created_at}\n"
        f"DASHBOARD_PASSWORD={initial_token}\n"
        f"TENANT_ID={safe_slug}\n"
        f"TENANT_SLUG={safe_slug}\n"
        f"NR3_INTERNAL_OVERRIDES_URL=http://wtyj-admin:8010\n"
        f"NR3_INTERNAL_API_TOKEN=SET_BY_FULL_VPS_SETUP_SCRIPT_TENANT_SCOPED\n"
        f"ICP_OVERRIDES_TTL_SECONDS=5\n"
        f"ANTHROPIC_API_KEY=SET_BY_FULL_VPS_SETUP_SCRIPT\n"
        f"LATE_API_KEY=SET_BY_FULL_VPS_SETUP_SCRIPT\n"
        f"ZERNIO_WEBHOOK_SECRET=SET_BY_FULL_VPS_SETUP_SCRIPT\n"
    )

    docker_compose_text = (
        f"# docker-compose.yml for tenant {safe_slug}\n"
        f"services:\n"
        f"  agent:\n"
        f"    image: wtyj-agent\n"
        f"    container_name: wtyj-{safe_slug}\n"
        f"    restart: unless-stopped\n"
        f"    ports:\n"
        f'      - "127.0.0.1:{host_port}:8001"\n'
        f"    env_file:\n"
        f"      - ./config/platform.env\n"
        f"    environment:\n"
        f"      - GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/app/config/calendar-key.json\n"
        f"    volumes:\n"
        f"      - ./config:/app/config:rw\n"
        f"      - ./data:/app/data\n"
        f"      - ./logs:/app/logs\n"
        f"    networks:\n"
        f"      - default\n"
        f"      - unboks-control\n"
        f"networks:\n"
        f"  unboks-control:\n"
        f"    external: true\n"
    )

    nginx_snippet_text = (
        f"# nginx route for tenant {safe_slug}\n"
        f"# add inside the existing api.unboks.org server block\n"
        f"location ^~ /api/{safe_slug}/ {{\n"
        f"    proxy_set_header X-Tenant-Slug {safe_slug};\n"
        f"\n"
        f"    if ($request_method = OPTIONS) {{\n"
        f"        add_header Access-Control-Allow-Origin \"https://dashboard.unboks.org\" always;\n"
        f"        add_header Access-Control-Allow-Methods \"GET, POST, PUT, PATCH, DELETE, OPTIONS\" always;\n"
        f"        add_header Access-Control-Allow-Headers \"Authorization, Content-Type, Accept, Origin, X-Tenant-Slug, Cache-Control, Pragma\" always;\n"
        f"        add_header Access-Control-Allow-Credentials \"true\" always;\n"
        f"        add_header Access-Control-Max-Age 86400 always;\n"
        f"        return 204;\n"
        f"    }}\n"
        f"\n"
        f"    proxy_pass http://127.0.0.1:{host_port}/;\n"
        f'    proxy_set_header Host $host;\n'
        f"    proxy_set_header X-Real-IP $remote_addr;\n"
        f"    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        f"    proxy_set_header X-Forwarded-Proto $scheme;\n"
        f"}}\n"
    )

    managed_nginx_block_text = (
        f"    # BEGIN UNBOKS TENANT {safe_slug}\n"
        + "\n".join(
            ("    " + line if line else "")
            for line in nginx_snippet_text.rstrip().splitlines()
        )
        + f"\n    # END UNBOKS TENANT {safe_slug}\n"
    )

    deploy_script_text = (
        f"# Provision tenant {safe_slug} on the VPS.\n"
        f"# Run as root after SSHing in.\n"
        f"set -e\n"
        f"\n"
        f"mkdir -p /root/clients/{safe_slug}/config /root/clients/{safe_slug}/data /root/clients/{safe_slug}/logs\n"
        f"cd /root/clients/{safe_slug}\n"
        f"\n"
        f"# 1. Paste client.json into:    /root/clients/{safe_slug}/config/client.json\n"
        f"# 2. Paste platform.env into:   /root/clients/{safe_slug}/config/platform.env\n"
        f"# 3. Paste docker-compose.yml:  /root/clients/{safe_slug}/docker-compose.yml\n"
        f"\n"
        f"docker compose up -d\n"
        f"\n"
        f"# 4. Paste nginx snippet into the api.unboks.org server block, then:\n"
        f"nginx -t && systemctl reload nginx\n"
        f"\n"
        f"# 5. Smoke verify\n"
        f"curl -s http://127.0.0.1:{host_port}/health\n"
    )

    full_vps_setup_script_text = (
        f"# Paste this entire block into the VPS terminal as root.\n"
        f"# It creates/updates tenant {safe_slug} without opening editors.\n"
        f"set -e\n"
        f"\n"
        f"SLUG={safe_slug}\n"
        f"TENANT_DIR=/root/clients/{safe_slug}\n"
        f"NGINX_SITE=/etc/nginx/sites-enabled/api-unboks\n"
        f"BRIDGE_TOKEN_DIR=/root/clients/_shared/nr3_bridge_tokens\n"
        f"BRIDGE_TOKEN_FILE=\"$BRIDGE_TOKEN_DIR/$SLUG\"\n"
        f"ANTHROPIC_KEY_FILE=/root/clients/_shared/anthropic_api_key\n"
        f"LATE_API_KEY_FILE=/root/clients/_shared/late_api_key\n"
        f"ZERNIO_WEBHOOK_SECRET_FILE=/root/clients/_shared/zernio_webhook_secret\n"
        f"mkdir -p \"$BRIDGE_TOKEN_DIR\"\n"
        f"chmod 700 \"$BRIDGE_TOKEN_DIR\"\n"
        f"if [ ! -s \"$BRIDGE_TOKEN_FILE\" ]; then\n"
        f"  python3 - <<'UNBOKS_BRIDGE_TOKEN' > \"$BRIDGE_TOKEN_FILE\"\n"
        f"import secrets\n"
        f"print(secrets.token_urlsafe(48))\n"
        f"UNBOKS_BRIDGE_TOKEN\n"
        f"  chmod 600 \"$BRIDGE_TOKEN_FILE\"\n"
        f"fi\n"
        f"if [ ! -s \"$ANTHROPIC_KEY_FILE\" ]; then\n"
        f"  echo \"ERROR: Claude key file is missing: $ANTHROPIC_KEY_FILE\"\n"
        f"  echo \"Ask Codex to repair shared Claude configuration before creating this tenant.\"\n"
        f"  exit 1\n"
        f"fi\n"
        f"BRIDGE_TOKEN=$(tr -d '\\r\\n' < \"$BRIDGE_TOKEN_FILE\")\n"
        f"ANTHROPIC_API_KEY=$(tr -d '\\r\\n' < \"$ANTHROPIC_KEY_FILE\")\n"
        f"LATE_API_KEY=\"\"\n"
        f"ZERNIO_WEBHOOK_SECRET=\"\"\n"
        f"if [ -s \"$LATE_API_KEY_FILE\" ]; then\n"
        f"  LATE_API_KEY=$(tr -d '\\r\\n' < \"$LATE_API_KEY_FILE\")\n"
        f"fi\n"
        f"if [ -s \"$ZERNIO_WEBHOOK_SECRET_FILE\" ]; then\n"
        f"  ZERNIO_WEBHOOK_SECRET=$(tr -d '\\r\\n' < \"$ZERNIO_WEBHOOK_SECRET_FILE\")\n"
        f"fi\n"
        f"\n"
        f"mkdir -p \"$TENANT_DIR/config\" \"$TENANT_DIR/data\" \"$TENANT_DIR/logs\"\n"
        f"cd \"$TENANT_DIR\"\n"
        f"\n"
        f"cat > \"$TENANT_DIR/config/client.json\" <<'UNBOKS_CLIENT_JSON'\n"
        f"{client_json_text}\n"
        f"UNBOKS_CLIENT_JSON\n"
        f"\n"
        f"cat > \"$TENANT_DIR/config/platform.env\" <<UNBOKS_PLATFORM_ENV\n"
        f"# platform.env for tenant {safe_slug}\n"
        f"# Generated by Nr3 at {created_at}\n"
        f"DASHBOARD_PASSWORD={initial_token}\n"
        f"TENANT_ID={safe_slug}\n"
        f"TENANT_SLUG={safe_slug}\n"
        f"NR3_INTERNAL_OVERRIDES_URL=http://wtyj-admin:8010\n"
        f"NR3_INTERNAL_API_TOKEN=${{BRIDGE_TOKEN}}\n"
        f"ICP_OVERRIDES_TTL_SECONDS=5\n"
        f"ANTHROPIC_API_KEY=${{ANTHROPIC_API_KEY}}\n"
        f"LATE_API_KEY=${{LATE_API_KEY}}\n"
        f"ZERNIO_WEBHOOK_SECRET=${{ZERNIO_WEBHOOK_SECRET}}\n"
        f"UNBOKS_PLATFORM_ENV\n"
        f"\n"
        f"cat > \"$TENANT_DIR/docker-compose.yml\" <<'UNBOKS_DOCKER_COMPOSE'\n"
        f"{docker_compose_text.rstrip()}\n"
        f"UNBOKS_DOCKER_COMPOSE\n"
        f"\n"
        f"python3 - <<'UNBOKS_NGINX_INSERT'\n"
        f"from pathlib import Path\n"
        f"path = Path('/etc/nginx/sites-enabled/api-unboks')\n"
        f"block = {json.dumps(managed_nginx_block_text)}\n"
        f"start = '# BEGIN UNBOKS TENANT {safe_slug}'\n"
        f"end = '# END UNBOKS TENANT {safe_slug}'\n"
        f"text = path.read_text()\n"
        f"while start in text and end in text:\n"
        f"    before, rest = text.split(start, 1)\n"
        f"    _, after = rest.split(end, 1)\n"
        f"    text = before.rstrip() + '\\n' + after.lstrip('\\n')\n"
        f"marker = '    server_name api.unboks.org;\\n'\n"
        f"if marker not in text:\n"
        f"    marker = 'server_name api.unboks.org;\\n'\n"
        f"if marker not in text:\n"
        f"    raise SystemExit('Could not find server_name api.unboks.org in nginx site file')\n"
        f"text = text.replace(marker, marker + '\\n' + block + '\\n', 1)\n"
        f"path.write_text(text)\n"
        f"UNBOKS_NGINX_INSERT\n"
        f"\n"
        f"docker network inspect unboks-control >/dev/null 2>&1 || docker network create unboks-control >/dev/null\n"
        f"docker compose down || true\n"
        f"docker compose up -d\n"
        f"nginx -t\n"
        f"systemctl reload nginx\n"
        f"\n"
        f"echo \"Done. Test login: https://dashboard.unboks.org/login?workspace={safe_slug}\"\n"
        f"echo \"Direct health check: curl -s http://127.0.0.1:{host_port}/health\"\n"
        f"echo \"Tenant-scoped ICP bridge token loaded from $BRIDGE_TOKEN_FILE. No manual token paste was needed.\"\n"
    )

    provision_result = auto_provision_tenant(
        slug=safe_slug,
        host_port=host_port,
        client_data=client_data,
        docker_compose_text=docker_compose_text,
        managed_nginx_block_text=managed_nginx_block_text,
        dashboard_url=dashboard_url,
    )
    logger.info(
        "tenant_create.auto_provision slug=%s status=%s job_id=%s",
        safe_slug,
        provision_result.status,
        provision_result.job_id,
    )

    return templates.TemplateResponse(
        request,
        "admin_tenant_created.html",
        {
            **_shell_context("tenant_create"),
            "slug": safe_slug,
            "name": name,
            "host_port": host_port,
            "dashboard_url": dashboard_url,
            "welcome_status": welcome_status,
            "welcome_error": welcome_error,
            "contact_email": contact_email_clean,
            "temporary_password": initial_token,
            "client_json_text": client_json_text,
            "platform_env_text": platform_env_text,
            "docker_compose_text": docker_compose_text,
            "nginx_snippet_text": nginx_snippet_text,
            "deploy_script_text": deploy_script_text,
            "full_vps_setup_script_text": full_vps_setup_script_text,
            "provision_result": provision_result,
        },
    )


def _create_error_response(request: Request, message: str, form_echo: dict) -> Response:
    """Re-render the wizard with an inline error + pre-filled values
    so the operator does not retype everything."""
    safe_echo = {k: form_echo.get(k, "") for k in (
        "name", "slug", "contact_person", "contact_email", "phone",
        "status", "tone", "notes", "send_welcome",
    )}
    return templates.TemplateResponse(
        request,
        "admin_tenant_create.html",
        {
            **_shell_context("tenant_create"),
            "error": message,
            "form": safe_echo,
        },
        status_code=400,
    )


@router.get("/admin/tenants/{tenant_id}", response_class=HTMLResponse)
def admin_tenant_workspace(request: Request, tenant_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app import channel_state as _channel_state
    from app import icp_overrides as _icp_overrides
    from app import tenant_notes as _tenant_notes
    override_toggles = _icp_overrides.feature_toggles_for_tenant(tenant.id)
    ai_settings = _icp_overrides.ai_agent_settings_for_tenant(tenant.id)
    tone_override = ai_settings.get("tone")
    escalation_rules_override = ai_settings.get("escalation_rules")
    agent_name_override = ai_settings.get("agent_name")
    response_timing_override = _icp_overrides.response_timing_for_tenant(tenant.id)
    sot_entries = _icp_overrides.sot_entries_for_tenant(tenant.id)
    agent_feature_states = {
        "learning": override_toggles.get(
            "learning_from_operator", {}
        ).get("value", tenant.agent.learning_enabled),
    }
    agent_source = (
        "icp_override"
        if (
            any(key in override_toggles for key in AGENT_FEATURE_ACTIONS.values())
            or bool(tone_override)
            or bool(agent_name_override)
            or bool(response_timing_override)
            or bool(sot_entries)
            or bool(escalation_rules_override)
        )
        else "backend"
    )
    stored_notes = _tenant_notes.list_notes(tenant.id)
    notes = sorted_notes(stored_notes + tenant.notes)
    nr2_knowledge = fetch_nr2_knowledge(tenant.id)
    prompt_conflict_report = build_prompt_conflict_report(
        tenant.id,
        nr2_knowledge=nr2_knowledge,
    )
    auto_block_sync = fetch_auto_block_settings(tenant.id)
    account_details = tenant_account_details(tenant.id)
    return templates.TemplateResponse(
        request,
        "admin_tenant_workspace.html",
        {
            "channels": _channel_state.read_channels(tenant.id),
            "channel_keys": _channel_state.CHANNEL_KEYS,
            **_shell_context("tenants", active_tenant=tenant),
            "tenant": tenant,
            "tenant_account": account_details,
            "action_message": request.query_params.get("action_message", ""),
            "action_level": request.query_params.get("action_level", "ok"),
            "agent_feature_states": agent_feature_states,
            "agent_source": agent_source,
            "ai_settings": ai_settings,
            "tone_override": tone_override,
            "escalation_rules_override": escalation_rules_override,
            "agent_name_override": agent_name_override,
            "response_timing_override": response_timing_override,
            "sot_entries": sot_entries,
            "is_reserved_tenant": tenant.id in RESERVED_SLUGS,
            "escalation_modes": ESCALATION_MODES,
            "notes": notes,
            "note_priorities": NOTE_PRIORITIES,
            "nr2_knowledge": nr2_knowledge,
            "prompt_conflict_report": prompt_conflict_report,
            "auto_block_sync": auto_block_sync,
            "auto_block_settings": auto_block_sync.settings,
        },
    )


@router.post("/admin/tenants/{tenant_id}/nr2-knowledge/refresh")
def admin_refresh_nr2_knowledge(request: Request, tenant_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)

    sync = fetch_nr2_knowledge(tenant.id, refresh=True)
    level = "ok" if sync.status in {"ok", "partial"} else "warn"
    if sync.status == "ok":
        message = "Nr2 company knowledge refreshed."
    elif sync.status == "partial":
        message = "Nr2 company knowledge refreshed with partial data."
    else:
        message = f"Nr2 refresh did not complete: {sync.status}."
    return RedirectResponse(
        url=(
            f"/admin/tenants/{tenant.id}"
            f"?action_message={quote_plus(message)}"
            f"&action_level={level}"
            "#nr2-knowledge-section"
        ),
        status_code=303,
    )


@router.post("/admin/tenants/{tenant_id}/prompt-conflicts/{conflict_id}/reviewed")
def admin_mark_prompt_conflict_reviewed(
    request: Request,
    tenant_id: str,
    conflict_id: str,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    tenant = get_tenant(tenant_id)
    if tenant is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    mark_reviewed(tenant.id, conflict_id)
    return RedirectResponse(
        url=(
            f"/admin/tenants/{tenant.id}"
            "?action_message=Prompt+conflict+marked+reviewed."
            "#prompt-conflicts-section"
        ),
        status_code=303,
    )


@router.get("/admin/tenants/{tenant_id}/backup/export")
def admin_export_tenant_backup(
    request: Request,
    tenant_id: str,
    include_history: Optional[str] = None,
    include_files: Optional[str] = None,
    include_logs: Optional[str] = None,
    include_inactive: Optional[str] = None,
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app.tenant_backup import build_export_package
    try:
        package = build_export_package(
            tenant_id,
            include_history=include_history is not None,
            include_files=include_files is not None,
            include_logs=include_logs is not None,
            include_inactive=include_inactive is not None,
        )
    except ValueError as exc:
        return _workspace_redirect(tenant_id, "backup-section", message=str(exc), level="warn")
    return FileResponse(package, media_type="application/zip", filename=package.name)


@router.post("/admin/tenants/{tenant_id}/backup/import")
def admin_import_tenant_backup(
    request: Request,
    tenant_id: str,
    backup_file: UploadFile = File(...),
    import_mode: str = Form(default="validate"),
    new_slug: str = Form(default=""),
    confirmation: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    if get_tenant(tenant_id) is None:
        return RedirectResponse(url="/admin/tenants", status_code=303)
    from app.tenant_backup import import_uploaded_package
    try:
        result = import_uploaded_package(
            backup_file.file,
            target_tenant=tenant_id,
            mode=import_mode,
            new_slug=new_slug,
            confirmation=confirmation,
        )
    except ValueError as exc:
        return _workspace_redirect(tenant_id, "backup-section", message=str(exc), level="warn")

    if result["status"] == "validated":
        summary = result["summary"]
        msg = (
            f"Backup validated: {summary['tenant_slug']} "
            f"({summary.get('export_timestamp') or 'unknown date'}). "
            "No data was changed."
        )
        return _workspace_redirect(tenant_id, "backup-section", message=msg)

    target = result["target_tenant"]
    msg = (
        f"Tenant backup imported to {target}. "
        f"Rollback package: {result['rollback_package']}. "
        "Provider channels may need reconnecting."
    )
    return _workspace_redirect(target, "backup-section", message=msg)


@router.get("/admin/onboarding", response_class=HTMLResponse)
def admin_onboarding(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return render_onboarding(request)


@router.get("/admin/reviews", response_class=HTMLResponse)
def admin_reviews(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return render_reviews(request)


@router.get("/admin/signups", response_class=HTMLResponse)
def admin_public_signups(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return render_public_signups(request, settings)


@router.get("/admin/signups/{signup_id}", response_class=HTMLResponse)
def admin_public_signup_detail(request: Request, signup_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return render_public_signup_detail(
        request,
        settings,
        signup_id,
        generated_link=request.query_params.get("generated_link", ""),
        notice=request.query_params.get("notice", ""),
        error=request.query_params.get("error", ""),
    )


@router.post("/admin/signups/{signup_id}/approve", response_class=HTMLResponse)
def admin_public_signup_approve(
    request: Request,
    signup_id: str,
    review_note: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        update_signup_request(
            signup_id,
            settings,
            status="approved",
            review_status="approved",
            reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            review_note=review_note.strip(),
        )
        audit_log.record_event(
            action="public_signup.approved",
            result="ok",
            safe_summary="Public signup approved.",
            metadata={"signup_id": signup_id},
        )
    except TenantCreateError as exc:
        return _signup_detail_redirect(signup_id, error=str(exc))
    return _signup_detail_redirect(signup_id, notice="Signup approved.")


@router.post("/admin/signups/{signup_id}/reject", response_class=HTMLResponse)
def admin_public_signup_reject(
    request: Request,
    signup_id: str,
    reject_reason: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    clean_reason = reject_reason.strip()
    if not clean_reason:
        return _signup_detail_redirect(signup_id, error="Reject reason is required.")
    try:
        update_signup_request(
            signup_id,
            settings,
            status="archived",
            review_status="rejected",
            reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            archived_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            reject_reason=clean_reason,
        )
        audit_log.record_event(
            action="public_signup.rejected",
            result="ok",
            safe_summary="Public signup rejected.",
            metadata={"signup_id": signup_id},
        )
    except TenantCreateError as exc:
        return _signup_detail_redirect(signup_id, error=str(exc))
    return _signup_detail_redirect(signup_id, notice="Signup rejected and archived.")


@router.post("/admin/signups/{signup_id}/request-info", response_class=HTMLResponse)
def admin_public_signup_request_info(request: Request, signup_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        signup = get_signup_request(signup_id, settings)
        if signup.get("archived_at") or signup.get("status") in {
            "approved",
            "onboarding_link_generated",
            "onboarding_link_sent",
            "provisioned",
        }:
            return _signup_detail_redirect(
                signup_id,
                error="Information requests are only available before approval.",
            )
        lead = _ensure_onboarding_lead_for_signup(signup, settings)
        result = prepare_or_send_onboarding_email(lead.id)
        update_signup_request(
            signup_id,
            settings,
            onboarding_lead_id=lead.id,
            info_request_sent_at=(
                datetime.now(timezone.utc).isoformat(timespec="seconds")
                if result.sent
                else None
            ),
            info_request_error=result.error or "",
            status="info_requested" if result.sent else "info_request_failed",
        )
        audit_log.record_event(
            action="public_signup.info_requested",
            result="ok" if result.sent else "failed",
            safe_summary=(
                "Public signup information request sent."
                if result.sent
                else "Public signup information request failed."
            ),
            metadata={"signup_id": signup_id, "lead_id": lead.id},
        )
    except (TenantCreateError, LeadValidationError, LeadNotFoundError) as exc:
        return _signup_detail_redirect(signup_id, error=str(exc))
    if not result.sent:
        return _signup_detail_redirect(
            signup_id,
            error=result.error or "Information request email could not be sent.",
        )
    return _signup_detail_redirect(
        signup_id,
        notice=f"Information request sent to {signup.get('email')}.",
    )


@router.post("/admin/signups/{signup_id}/generate-link", response_class=HTMLResponse)
def admin_public_signup_generate_link(request: Request, signup_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        signup = get_signup_request(signup_id, settings)
        if signup.get("status") not in {
            "approved",
            "onboarding_link_generated",
            "onboarding_link_sent",
            "failed",
        }:
            return _signup_detail_redirect(
                signup_id,
                error="Approve this signup before generating an onboarding link.",
            )
        lead = _ensure_onboarding_lead_for_signup(signup, settings)
        lead, raw_token = create_or_refresh_token(lead.id)
        link = build_onboarding_link(raw_token, settings)
        update_signup_request(
            signup_id,
            settings,
            onboarding_lead_id=lead.id,
            onboarding_link_generated_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            status="onboarding_link_generated",
        )
        audit_log.record_event(
            action="public_signup.onboarding_link_generated",
            result="ok",
            safe_summary="Public signup onboarding link generated.",
            metadata={"signup_id": signup_id, "lead_id": lead.id},
        )
    except (TenantCreateError, LeadValidationError, LeadNotFoundError) as exc:
        return _signup_detail_redirect(signup_id, error=str(exc))
    return _signup_detail_redirect(
        signup_id,
        notice="Onboarding link generated.",
        generated_link=link,
    )


@router.post("/admin/signups/{signup_id}/send-onboarding", response_class=HTMLResponse)
def admin_public_signup_send_onboarding(request: Request, signup_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        signup = get_signup_request(signup_id, settings)
        if signup.get("status") not in {
            "approved",
            "onboarding_link_generated",
            "onboarding_link_sent",
            "failed",
        }:
            return _signup_detail_redirect(
                signup_id,
                error="Approve this signup before sending an onboarding link.",
            )
        lead = _ensure_onboarding_lead_for_signup(signup, settings)
        result = prepare_or_send_onboarding_email(lead.id)
        update_signup_request(
            signup_id,
            settings,
            onboarding_lead_id=lead.id,
            onboarding_email_sent_at=(
                result.draft and datetime.now(timezone.utc).isoformat(timespec="seconds")
                if result.sent
                else None
            ),
            onboarding_email_error=result.error or "",
            status="onboarding_link_sent" if result.sent else "failed",
        )
        audit_log.record_event(
            action="public_signup.onboarding_email_sent",
            result="ok" if result.sent else "failed",
            safe_summary=(
                "Public signup onboarding email sent."
                if result.sent
                else "Public signup onboarding email failed."
            ),
            metadata={"signup_id": signup_id, "lead_id": lead.id},
        )
    except (TenantCreateError, LeadValidationError, LeadNotFoundError) as exc:
        return _signup_detail_redirect(signup_id, error=str(exc))
    if not result.sent:
        return _signup_detail_redirect(
            signup_id,
            error=result.error or "Onboarding email could not be sent.",
            generated_link=result.draft.onboarding_link,
        )
    return _signup_detail_redirect(
        signup_id,
        notice=f"Onboarding email sent to {result.draft and signup.get('email')}.",
    )


@router.post("/admin/signups/{signup_id}/create-workspace", response_class=HTMLResponse)
def admin_public_signup_create_workspace(request: Request, signup_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        signup = get_signup_request(signup_id, settings)
        if signup.get("status") not in {
            "approved",
            "onboarding_link_generated",
            "onboarding_link_sent",
            "failed",
        }:
            return _signup_detail_redirect(
                signup_id,
                error="Approve this signup before creating a workspace.",
            )
        result = create_public_signup_tenant(
            full_name=str(signup.get("full_name") or ""),
            business_name=str(signup.get("business_name") or ""),
            email=str(signup.get("email") or ""),
            phone=str(signup.get("phone") or ""),
            settings=settings,
        )
        mark_provisioned(signup_id, result.slug, settings)
        audit_log.record_event(
            action="public_signup.workspace_created",
            tenant_id=result.slug,
            result="ok",
            safe_summary="Workspace created from public signup.",
            metadata={"signup_id": signup_id, "slug": result.slug},
        )
    except TenantCreateError as exc:
        update_signup_request(
            signup_id,
            settings,
            status="failed",
            workspace_error=str(exc),
        )
        audit_log.record_event(
            action="public_signup.workspace_create_failed",
            result="failed",
            safe_summary="Workspace creation from public signup failed.",
            metadata={"signup_id": signup_id, "error": str(exc)},
        )
        return _signup_detail_redirect(signup_id, error=str(exc))
    except Exception as exc:
        update_signup_request(
            signup_id,
            settings,
            status="failed",
            workspace_error="Workspace creation failed.",
        )
        audit_log.record_event(
            action="public_signup.workspace_create_failed",
            result="failed",
            safe_summary="Workspace creation from public signup failed.",
            metadata={"signup_id": signup_id, "error": type(exc).__name__},
        )
        return _signup_detail_redirect(
            signup_id,
            error="Workspace creation failed. Check logs before retrying.",
        )
    return _signup_detail_redirect(
        signup_id,
        notice=f"Workspace created: {result.slug}.",
    )


@router.get("/admin/todos", response_class=HTMLResponse)
def admin_todos(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_todos.html",
        {
            **_shell_context("todos"),
            "todos": todo_store.list_todos(),
            "todo_error": request.query_params.get("todo_error", ""),
        },
    )


@router.post("/admin/todos", response_class=HTMLResponse)
def admin_create_todo(
    request: Request,
    content_html: str = Form(default=""),
    content_plain: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        todo_store.create_todo(content_html=content_html, content_plain=content_plain)
    except ValueError as exc:
        return RedirectResponse(
            url=f"/admin/todos?todo_error={quote_plus(str(exc))}",
            status_code=303,
        )
    return RedirectResponse(url="/admin/todos", status_code=303)


@router.post("/admin/todos/{todo_id}/toggle", response_class=HTMLResponse)
def admin_toggle_todo(request: Request, todo_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    todo_store.toggle_todo(todo_id)
    return RedirectResponse(url="/admin/todos", status_code=303)


@router.post("/admin/todos/{todo_id}/delete", response_class=HTMLResponse)
def admin_delete_todo(request: Request, todo_id: str) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    todo_store.delete_todo(todo_id)
    return RedirectResponse(url="/admin/todos", status_code=303)


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    from app import audit_log as _audit_log
    audit_events = [
        {
            "time": event.created_at,
            "actor": event.actor,
            "tenant": event.tenant_id or "—",
            "action": f"{event.action} ({event.result})",
        }
        for event in _audit_log.list_events(limit=50)
    ]
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            **_shell_context("settings"),
            "audit_events": audit_events,
        },
    )


# ---------------------------------------------------------------------------
# Onboarding lead actions (live under /admin/onboarding)
# ---------------------------------------------------------------------------


@router.post("/admin/onboarding/leads", response_class=HTMLResponse)
def create_onboarding_lead(
    request: Request,
    email: str = Form(default=""),
    business_name: str = Form(default=""),
    contact_name: str = Form(default=""),
    language: str = Form(default=""),
    notes: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect

    lead_input = LeadInput(
        email=email,
        business_name=clean_optional(business_name),
        contact_name=clean_optional(contact_name),
        language=clean_optional(language),
        notes=clean_optional(notes),
    )
    try:
        create_lead(lead_input)
    except LeadValidationError as exc:
        return render_onboarding(
            request,
            error=str(exc),
            form={
                "email": email,
                "business_name": business_name,
                "contact_name": contact_name,
                "language": language,
                "notes": notes,
            },
            status_code=400,
        )
    return RedirectResponse(url="/admin/onboarding", status_code=303)


@router.post("/admin/onboarding/leads/{lead_id}/send-email", response_class=HTMLResponse)
def send_onboarding_email(request: Request, lead_id: int) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        result = prepare_or_send_onboarding_email(lead_id)
    except LeadNotFoundError:
        return render_onboarding(
            request,
            error="Onboarding lead not found.",
            status_code=404,
        )
    return render_onboarding(
        request,
        email_result=result,
        sent_notice="Onboarding email sent." if result.sent else None,
    )


@router.get("/admin/api/onboarding/leads")
def onboarding_leads_api(request: Request):
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return {
        "leads": [
            {
                "id": lead.id,
                "email": lead.email,
                "business_name": lead.business_name,
                "contact_name": lead.contact_name,
                "language": lead.language,
                "notes": lead.notes,
                "status": lead.status,
                "created_at": lead.created_at,
                "updated_at": lead.updated_at,
            }
            for lead in list_leads()
        ]
    }


@router.get("/admin/onboarding/leads/{lead_id}", response_class=HTMLResponse)
def onboarding_lead_detail(request: Request, lead_id: int) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        lead = get_lead(lead_id)
    except LeadNotFoundError:
        return render_onboarding(
            request,
            error="Onboarding lead not found.",
            status_code=404,
        )
    return render_lead_detail(request, lead)


@router.post("/admin/onboarding/leads/{lead_id}/review", response_class=HTMLResponse)
def onboarding_lead_review_decision(
    request: Request,
    lead_id: int,
    decision: str = Form(default=""),
    review_notes: str = Form(default=""),
) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        set_review_decision(lead_id, decision, review_notes)
    except LeadNotFoundError:
        return render_onboarding(
            request,
            error="Onboarding lead not found.",
            status_code=404,
        )
    except LeadValidationError as exc:
        lead = get_lead(lead_id)
        return render_lead_detail(
            request,
            lead,
            error=str(exc),
            review_notes=review_notes,
            status_code=400,
        )
    return RedirectResponse(url=f"/admin/onboarding/leads/{lead_id}", status_code=303)


@router.get("/admin/onboarding/leads/{lead_id}/setup-summary.txt")
def onboarding_lead_setup_summary(request: Request, lead_id: int) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    try:
        summary = build_setup_summary(lead_id)
    except LeadNotFoundError:
        return Response("Onboarding lead not found.\n", status_code=404, media_type="text/plain")
    return Response(
        summary,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="onboarding-lead-{lead_id}-setup-summary.txt"'
        },
    )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _pipeline_totals(leads) -> dict[str, int]:
    awaiting_email = 0
    in_intake = 0
    awaiting_review = 0
    for lead in leads:
        if lead.status in {"lead_created", "email_pending"}:
            awaiting_email += 1
        elif lead.status in {"email_sent", "form_started"}:
            in_intake += 1
        elif lead.status == "form_submitted":
            awaiting_review += 1
    return {
        "total": len(leads),
        "awaiting_email": awaiting_email,
        "in_intake": in_intake,
        "awaiting_review": awaiting_review,
    }


def _signup_detail_redirect(
    signup_id: str,
    *,
    notice: str = "",
    error: str = "",
    generated_link: str = "",
) -> RedirectResponse:
    params = []
    if notice:
        params.append(f"notice={quote_plus(notice)}")
    if error:
        params.append(f"error={quote_plus(error)}")
    if generated_link:
        params.append(f"generated_link={quote_plus(generated_link)}")
    suffix = "?" + "&".join(params) if params else ""
    return RedirectResponse(url=f"/admin/signups/{signup_id}{suffix}", status_code=303)


def _ensure_onboarding_lead_for_signup(signup: dict, settings) -> object:
    lead_id = signup.get("onboarding_lead_id")
    if lead_id:
        try:
            return get_lead(int(lead_id))
        except (TypeError, ValueError, LeadNotFoundError):
            pass
    email = str(signup.get("email") or "").strip().lower()
    for lead in list_leads():
        if lead.email.strip().lower() == email:
            update_signup_request(
                str(signup.get("id") or ""),
                settings,
                onboarding_lead_id=lead.id,
            )
            return lead
    note_parts = ["Created from public free-trial signup."]
    if signup.get("id"):
        note_parts.append(f"Signup ID: {signup.get('id')}")
    if signup.get("phone"):
        note_parts.append(f"Phone: {signup.get('phone')}")
    return create_lead(
        LeadInput(
            email=str(signup.get("email") or ""),
            business_name=clean_optional(str(signup.get("business_name") or "")),
            contact_name=clean_optional(str(signup.get("full_name") or "")),
            language=None,
            notes="\n".join(note_parts),
        )
    )


def render_onboarding(
    request: Request,
    error: Optional[str] = None,
    form: Optional[dict[str, str]] = None,
    email_result: Optional[EmailSendResult] = None,
    sent_notice: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_onboarding.html",
        {
            **_shell_context("onboarding"),
            "error": error,
            "sent_notice": sent_notice,
            "email_result": email_result,
            "form": form or {},
            "leads": list_leads(),
            "intake_answer_counts": list_intake_answer_counts(),
            "intake_total": len(INTAKE_QUESTIONS),
        },
        status_code=status_code,
    )


def render_reviews(request: Request) -> HTMLResponse:
    leads = list_leads()
    awaiting = [lead for lead in leads if lead.status in REVIEW_AWAITING_STATUSES]
    decided = [lead for lead in leads if lead.status in REVIEW_DECIDED_STATUSES]
    return templates.TemplateResponse(
        request,
        "admin_reviews.html",
        {
            **_shell_context("reviews"),
            "awaiting": awaiting,
            "decided": decided,
            "intake_answer_counts": list_intake_answer_counts(),
            "intake_total": len(INTAKE_QUESTIONS),
        },
    )


def render_public_signups(request: Request, settings) -> HTMLResponse:
    show_archived = request.query_params.get("archived") == "1"
    signups = list_signup_requests(settings, include_archived=show_archived)
    archived_count = len(
        [
            signup
            for signup in list_signup_requests(settings, include_archived=True)
            if is_archived_signup(signup)
        ]
    )
    pending_review = [
        signup
        for signup in signups
        if signup.get("status") in {
            "verified_pending_review",
            "info_requested",
            "info_request_failed",
        }
    ]
    pending_verification = [
        signup
        for signup in signups
        if signup.get("status") == "verification_pending"
    ]
    return templates.TemplateResponse(
        request,
        "admin_public_signups.html",
        {
            **_shell_context("signups"),
            "signups": signups,
            "pending_review": pending_review,
            "pending_verification": pending_verification,
            "show_archived": show_archived,
            "archived_count": archived_count,
        },
    )


def render_public_signup_detail(
    request: Request,
    settings,
    signup_id: str,
    *,
    generated_link: str = "",
    notice: str = "",
    error: str = "",
) -> HTMLResponse:
    try:
        signup = get_signup_request(signup_id, settings)
    except TenantCreateError:
        return templates.TemplateResponse(
            request,
            "admin_public_signup_detail.html",
            {
                **_shell_context("signups"),
                "signup": None,
                "generated_link": "",
                "notice": "",
                "error": "Signup request not found.",
                "linked_lead": None,
            },
            status_code=404,
        )
    linked_lead = None
    lead_id = signup.get("onboarding_lead_id")
    if lead_id:
        try:
            linked_lead = get_lead(int(lead_id))
        except (TypeError, ValueError, LeadNotFoundError):
            linked_lead = None
    return templates.TemplateResponse(
        request,
        "admin_public_signup_detail.html",
        {
            **_shell_context("signups"),
            "signup": signup,
            "generated_link": generated_link,
            "notice": notice,
            "error": error,
            "linked_lead": linked_lead,
        },
    )


def render_lead_detail(
    request: Request,
    lead,
    error: Optional[str] = None,
    review_notes: Optional[str] = None,
    status_code: int = 200,
) -> HTMLResponse:
    answers = list_intake_answers(lead.id)
    return templates.TemplateResponse(
        request,
        "onboarding_lead_detail.html",
        {
            **_shell_context("reviews"),
            "error": error,
            "lead": lead,
            "answers": answers,
            "questions": INTAKE_QUESTIONS,
            "answer_count": len(answers),
            "intake_total": len(INTAKE_QUESTIONS),
            "setup_summary": build_setup_summary(lead.id),
            "review_notes_value": (
                review_notes if review_notes is not None else lead.review_notes or ""
            ),
        },
        status_code=status_code,
    )
