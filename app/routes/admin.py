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
    CLOUD_PROVIDERS,
    ESCALATION_MODES,
    UPLOAD_CATEGORIES,
    Tenant,
    get_tenant,
    list_tenants,
)

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
            "attention_items": _compute_attention_items(list_tenants()),
        },
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


@router.get("/admin/settings", response_class=HTMLResponse)
def admin_settings(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        request,
        "admin_settings.html",
        _shell_context("settings"),
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
