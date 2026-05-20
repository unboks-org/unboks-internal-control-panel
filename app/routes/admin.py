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
from app.security import (
    clear_session_cookie,
    create_session_value,
    require_admin,
    set_session_cookie,
    verify_admin_password,
)
from app.tenants import (
    ACTIVITY_TYPES,
    CLOUD_PROVIDERS,
    ESCALATION_MODES,
    NOTE_PRIORITIES,
    UPLOAD_CATEGORIES,
    Tenant,
    TenantCreateError,
    compute_setup_checklist,
    derive_slug_from_name,
    get_tenant,
    list_tenants,
    sorted_notes,
    validate_slug,
)

import json
import logging
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import quote_plus


logger = logging.getLogger(__name__)


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


REVIEW_AWAITING_STATUSES = {"form_submitted"}
REVIEW_DECIDED_STATUSES = {"review_needs_changes", "review_approved", "tenant_ready"}


def _shell_context(active: str, active_tenant: Optional[Tenant] = None) -> dict:
    """Context every admin template needs so the sidebar renders."""
    return {
        "active": active,
        "tenants": list_tenants(),
        "active_tenant": active_tenant,
    }


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
    plan: str = Form(default="trial"),
    status: str = Form(default="trial"),
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

    # Initial sign-in token. URL-safe so it pastes cleanly into an
    # email body without escaping.
    initial_token = secrets.token_urlsafe(12)
    created_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    dashboard_url = f"https://dashboard.unboks.org/{safe_slug}"

    # Manual-Mode client.json payload. Flat shape per the J3-BE-50
    # brief. The six required fields come first; optional wizard
    # fields are appended only when the operator filled them in.
    client_data: dict = {
        "slug": safe_slug,
        "name": name,
        "password": initial_token,
        "status": status.strip().lower() or "trial",
        "plan": plan.strip().lower() or "trial",
        "created_at": created_at,
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
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "tenant_create.disk_skipped slug=%s reason=root_mkdir_failed err=%r",
            safe_slug, exc)
        root = ""

    if root:
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

    # Pretty-print the JSON so the copy/download flow gives the
    # operator a readable file.
    client_json_text = json.dumps(client_data, indent=2, ensure_ascii=False)

    return templates.TemplateResponse(
        request,
        "admin_tenant_created.html",
        {
            **_shell_context("tenant_create"),
            "slug": safe_slug,
            "name": name,
            "client_json_text": client_json_text,
            "dashboard_url": dashboard_url,
            "welcome_status": welcome_status,
            "welcome_error": welcome_error,
            "contact_email": contact_email_clean,
        },
    )


def _create_error_response(request: Request, message: str, form_echo: dict) -> Response:
    """Re-render the wizard with an inline error + pre-filled values
    so the operator does not retype everything."""
    safe_echo = {k: form_echo.get(k, "") for k in (
        "name", "slug", "contact_person", "contact_email", "phone",
        "plan", "status", "tone", "notes", "send_welcome",
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
    return templates.TemplateResponse(
        request,
        "admin_tenant_workspace.html",
        {
            **_shell_context("tenants", active_tenant=tenant),
            "tenant": tenant,
            "cloud_providers": CLOUD_PROVIDERS,
            "upload_categories": UPLOAD_CATEGORIES,
            "escalation_modes": ESCALATION_MODES,
            "activity_type_labels": dict(ACTIVITY_TYPES),
            "notes": sorted_notes(tenant.notes),
            "note_priorities": NOTE_PRIORITIES,
            "setup_checklist": compute_setup_checklist(tenant),
            "contract": _build_contract(tenant),
            "feature_toggles": _build_feature_toggles(tenant),
            "runtime": _build_runtime(tenant),
            "backup": _build_backup(tenant),
            "comms_log": _build_comms_log(tenant),
            "invoices": _build_invoices(tenant),
        },
    )


def _build_contract(tenant: Tenant) -> dict:
    b = tenant.billing
    plan_label = b.plan if b.plan and b.plan != "—" else (
        "Trial" if b.status == "trial" else "—"
    )
    contract_status_map = {
        "trial": ("Draft", "warn"),
        "active": ("Active", "ok"),
        "paused": ("Paused", "warn"),
        "overdue": ("Active", "warn"),
        "cancelled": ("Cancelled", "down"),
    }
    contract_status, contract_class = contract_status_map.get(
        b.status, ("Draft", "unknown")
    )
    payment_status_map = {
        "ok": ("Paid", "ok"),
        "pending": ("Pending", "warn"),
        "failed": ("Overdue", "down"),
        "—": ("Not configured", "unknown"),
    }
    payment_status, payment_class = payment_status_map.get(
        b.payment_status, ("Not configured", "unknown")
    )
    return {
        "plan_label": plan_label,
        "trial_start": "—",
        "trial_end": "—" if b.trial_days_left is None else f"in {b.trial_days_left} days",
        "monthly_price": b.monthly_price,
        "setup_fee": "—",
        "contract_status": contract_status,
        "contract_class": contract_class,
        "payment_status": payment_status,
        "payment_class": payment_class,
    }


_FEATURE_TOGGLE_DEFS: tuple[tuple[str, str], ...] = (
    ("whatsapp_inbox", "WhatsApp inbox"),
    ("email_inbox", "Email inbox"),
    ("instagram_facebook", "Instagram / Facebook"),
    ("telegram_alerts", "Telegram alerts"),
    ("ai_auto_reply", "AI auto-reply"),
    ("soft_escalations", "Soft escalations"),
    ("hard_escalations", "Hard escalations / human takeover"),
    ("learning_from_operator", "Learning from operator answers"),
    ("sot_sync", "Source of Truth sync"),
    ("appointment_order_handling", "Appointment / order handling"),
    ("analytics", "Analytics"),
)


def _build_feature_toggles(tenant: Tenant) -> list[dict]:
    # Derive a few from real fields; the rest stay 'Not wired yet'.
    derived: dict[str, bool] = {}
    for ch in tenant.channels:
        name = ch.name.lower()
        if name == "whatsapp":
            derived["whatsapp_inbox"] = ch.state == "connected"
        elif name == "email":
            derived["email_inbox"] = ch.state == "connected"
        elif name in ("instagram", "facebook"):
            derived["instagram_facebook"] = derived.get("instagram_facebook", False) or ch.state == "connected"
        elif name == "telegram":
            derived["telegram_alerts"] = ch.state == "connected"
    if tenant.agent.auto_reply_enabled:
        derived["ai_auto_reply"] = True
    if tenant.agent.escalation_mode in ("soft", "both"):
        derived["soft_escalations"] = True
    if tenant.agent.escalation_mode in ("hard", "both"):
        derived["hard_escalations"] = True
    if tenant.agent.learning_enabled:
        derived["learning_from_operator"] = True
    if tenant.sot.cloud_status == "connected":
        derived["sot_sync"] = True

    items: list[dict] = []
    for key, label in _FEATURE_TOGGLE_DEFS:
        if key in derived:
            state = "enabled" if derived[key] else "disabled"
            wired = True
        else:
            state = "unknown"
            wired = False
        items.append({"key": key, "label": label, "state": state, "wired": wired})
    return items


def _build_runtime(tenant: Tenant) -> dict:
    return {
        "dashboard_status": ("Unknown", "unknown"),
        "agent_status": (
            ("Active", "ok") if tenant.agent.auto_reply_enabled else ("Paused", "warn")
        ),
        "api_status": ("Unknown", "unknown"),
        "webhook_status": ("Unknown", "unknown"),
        "last_sync": "—",
        "last_error": "—",
        "uptime": "—",
        "environment": "Not wired yet",
    }


def _build_backup(tenant: Tenant) -> dict:
    return {
        "last_backup": "—",
        "status": ("Not wired yet", "unknown"),
        "items": (
            "Tenant config",
            "Source of Truth",
            "Activity log",
            "Onboarding answers",
        ),
    }


def _build_comms_log(tenant: Tenant) -> dict:
    return {
        "last_email_sent": "—",
        "last_onboarding_link_sent": "—",
        "last_operator_note": (tenant.notes[0].created_at if tenant.notes else "—"),
        "last_client_reply": "—",
    }


def _build_invoices(tenant: Tenant) -> list[dict]:
    # No payment integration. Empty by default.
    return []


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


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        {
            **_shell_context("settings"),
            "audit_events": (),
            "admin_users": (
                {
                    "name": "Internal admin",
                    "email": "—",
                    "role": "Owner",
                    "last_login": "—",
                    "two_factor": "Not configured",
                    "status": "Active",
                },
            ),
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


_ATTENTION_KIND_LABELS: tuple[tuple[str, str], ...] = (
    ("problem", "Problem tenant"),
    ("setup_incomplete", "Setup incomplete"),
    ("trial_ending_soon", "Trial ending soon"),
    ("channels_disconnected", "Channels disconnected"),
    ("agent_paused", "Agent paused"),
    ("sot_missing", "SOT missing"),
)


def _compute_attention_items(tenants) -> list[dict]:
    items: list[dict] = []
    for t in tenants:
        # Problem (worst-case bucket): hard escalations or billing overdue/cancelled
        if t.escalations.hard_count > 0 or t.billing.status in ("overdue", "cancelled"):
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "problem", "label": "Problem tenant", "severity": "P0",
            })

        # Setup incomplete
        if t.onboarding.status != "ready":
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "setup_incomplete", "label": "Setup incomplete", "severity": "P1",
            })

        # Trial ending soon
        days_left = t.billing.trial_days_left
        if days_left is not None and days_left <= 7:
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "trial_ending_soon", "label": "Trial ending soon",
                "severity": "P0" if days_left <= 2 else "P1",
            })

        # Channels disconnected
        if t.health.channels in ("warn", "down") or (
            t.channels and all(ch.state != "connected" for ch in t.channels)
        ):
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "channels_disconnected", "label": "Channels disconnected", "severity": "P2",
            })

        # Agent paused (auto-reply off or human takeover active)
        if not t.agent.auto_reply_enabled or t.agent.human_takeover_active:
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "agent_paused", "label": "Agent paused", "severity": "P2",
            })

        # SOT missing
        if t.sot.status not in ("ok",) or t.sot.files_count == 0:
            items.append({
                "tenant_id": t.id, "tenant_name": t.name,
                "kind": "sot_missing", "label": "SOT missing", "severity": "P2",
            })

    severity_order = {"P0": 0, "P1": 1, "P2": 2}
    items.sort(key=lambda i: (severity_order.get(i["severity"], 9), i["tenant_name"]))
    return items


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