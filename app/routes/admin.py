from fastapi import APIRouter, Form, Request, File, UploadFile
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates
from typing import Optional

from app.config import get_settings
from app.emailer import EmailSendResult, prepare_or_send_onboarding_email
from app.onboarding import (
    INTAKE_QUESTIONS,
    LeadInput,
    LeadNotFoundError,
    LeadValidationError,
    build_setup_summary,
    clean_optional,
    create_lead,
    get_lead,
    list_intake_answers,
    list_intake_answer_counts,
    list_leads,
    set_review_decision,
)
from app import todos as todo_store
from app.security import (
    clear_session_cookie,
    create_session_value,
    require_admin,
    set_session_cookie,
    verify_admin_password,
)
from app.provisioning import auto_provision_tenant, queue_tenant_host_action
from app.nr2_sync import fetch_nr2_knowledge
from app.port_registry import PortRegistryError, reserve_tenant_port
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
            statuses[tenant.id] = {
                "status": "connected",
                "label": "Connected",
                "badge_class": "tenant-wa-connected",
                "chip_class": "status-ok",
                "visible": True,
                "phone": connection.display_phone_number or "",
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
            }
        elif get_tenant_client_data(tenant.id).get("whatsapp_connect_token"):
            statuses[tenant.id] = {
                "status": "awaiting_activation",
                "label": "Awaiting activation",
                "badge_class": "tenant-wa-pending",
                "chip_class": "status-warn",
                "visible": True,
                "phone": "",
            }
        elif connection and connection.status == "failed":
            statuses[tenant.id] = {
                "status": "failed",
                "label": "Failed",
                "badge_class": "tenant-wa-failed",
                "chip_class": "status-error",
                "visible": True,
                "phone": connection.display_phone_number or "",
            }
        else:
            statuses[tenant.id] = {
                "status": "not_connected",
                "label": "Not connected",
                "badge_class": "tenant-wa-muted",
                "chip_class": "status-unknown",
                "visible": False,
                "phone": "",
            }
    return statuses


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
            message="That AI Agent control is not wired yet.",
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
        f'      - "{host_port}:8001"\n'
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
            or bool(sot_entries)
            or bool(escalation_rules_override)
        )
        else "backend"
    )
    stored_notes = _tenant_notes.list_notes(tenant.id)
    notes = sorted_notes(stored_notes + tenant.notes)
    nr2_knowledge = fetch_nr2_knowledge(tenant.id)
    return templates.TemplateResponse(
        request,
        "admin_tenant_workspace.html",
        {
            "channels": _channel_state.read_channels(tenant.id),
            "channel_keys": _channel_state.CHANNEL_KEYS,
            **_shell_context("tenants", active_tenant=tenant),
            "tenant": tenant,
            "action_message": request.query_params.get("action_message", ""),
            "action_level": request.query_params.get("action_level", "ok"),
            "agent_feature_states": agent_feature_states,
            "agent_source": agent_source,
            "ai_settings": ai_settings,
            "tone_override": tone_override,
            "escalation_rules_override": escalation_rules_override,
            "sot_entries": sot_entries,
            "is_reserved_tenant": tenant.id in RESERVED_SLUGS,
            "escalation_modes": ESCALATION_MODES,
            "notes": notes,
            "note_priorities": NOTE_PRIORITIES,
            "nr2_knowledge": nr2_knowledge,
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
