from fastapi import APIRouter, Form, Request
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
    ANOMALY_SIGNALS,
    ANOMALY_STATUSES,
    CLOUD_PROVIDERS,
    ESCALATION_MODES,
    NOTE_PRIORITIES,
    UPLOAD_CATEGORIES,
    Tenant,
    TenantCreateError,
    compute_setup_checklist,
    create_tenant_directory,
    derive_slug_from_name,
    get_tenant,
    list_anomalies,
    list_tenants,
    sorted_notes,
    validate_slug,
)
from fastapi import File, UploadFile

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
    """Single-submit tenant creation."""
    import os
    import re
    import secrets
    from urllib.parse import quote_plus

    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect

    name = (name or "").strip()
    if not name:
        return _create_error_response(
            request, "Business / tenant name is required.",
            form_echo=locals())
    candidate_slug = (slug or "").strip() or derive_slug_from_name(name)
    try:
        safe_slug = validate_slug(candidate_slug)
    except TenantCreateError as exc:
        return _create_error_response(request, str(exc), form_echo=locals())

    business: dict = {
        "slug": safe_slug,
        "name": name,
        "plan": plan.strip().lower() or "trial",
        "status": status.strip().lower() or "trial",
    }
    if contact_person.strip():
        business["contact_person"] = contact_person.strip()
    if contact_email.strip():
        business["email"] = contact_email.strip()
    if phone.strip():
        business["whatsapp"] = phone.strip()
    if tone.strip():
        business["agent_tone"] = tone.strip()
    if notes.strip():
        business["notes"] = notes.strip()

    try:
        tenant_root = create_tenant_directory(safe_slug, business)
    except TenantCreateError as exc:
        return _create_error_response(request, str(exc), form_echo=locals())
    except OSError as exc:
        return _create_error_response(
            request, f"Filesystem error creating tenant: {exc}",
            form_echo=locals())

    upload_warnings: list[str] = []
    if files:
        uploads_dir = os.path.join(tenant_root, "data", "uploads")
        os.makedirs(uploads_dir, exist_ok=True)
        for uploaded in files:
            if not uploaded or not uploaded.filename:
                continue
            raw = await uploaded.read()
            if len(raw) == 0:
                continue
            if len(raw) > 25 * 1024 * 1024:
                upload_warnings.append(
                    f"{uploaded.filename}: file too large (25 MB max)")
                continue
            safe_name = re.sub(r"[^A-Za-z0-9._\- ]", "_",
                                uploaded.filename.rsplit("/", 1)[-1]).strip()[:120] or "upload"
            with open(os.path.join(uploads_dir, safe_name), "wb") as f:
                f.write(raw)

    op_username = safe_slug
    op_token = secrets.token_urlsafe(12)
    dashboard_url = f"https://dashboard.unboks.org/?workspace={safe_slug}"

    welcome_warning = ""
    if send_welcome.strip() and contact_email.strip():
        from app.emailer import (build_tenant_welcome_email, send_email,
                                  smtp_is_configured)
        if not smtp_is_configured(settings):
            welcome_warning = "Welcome email skipped: SMTP not configured."
        else:
            draft = build_tenant_welcome_email(
                tenant_name=name,
                dashboard_url=dashboard_url,
                username=op_username,
                initial_token=op_token,
            )
            try:
                send_email(
                    contact_email.strip(),
                    draft.subject,
                    draft.body,
                    settings,
                )
            except Exception as exc:
                welcome_warning = f"Welcome email failed: {exc}"

    qs_bits = ["created=1"]
    if upload_warnings:
        qs_bits.append("warn=" + quote_plus(
            "Uploads: " + "; ".join(upload_warnings)))
    if welcome_warning:
        qs_bits.append("warn=" + quote_plus(welcome_warning))
    return RedirectResponse(
        url=f"/admin/tenants/{safe_slug}?" + "&".join(qs_bits),
        status_code=303)


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


@router.get("/admin/attention", response_class=HTMLResponse)
def admin_attention(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_attention.html",
        {
            **_shell_context("attention"),
            "attention_items": _compute_attention_items(list_tenants()),
        },
    )


@router.get("/admin/anomalies", response_class=HTMLResponse)
def admin_anomalies(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_anomalies.html",
        {
            **_shell_context("anomalies"),
            "anomalies": list_anomalies(),
            "anomaly_signals": ANOMALY_SIGNALS,
            "anomaly_statuses": dict(ANOMALY_STATUSES),
        },
    )


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
