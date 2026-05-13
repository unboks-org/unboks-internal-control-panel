from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates
from typing import Optional

from app.config import get_settings
from app.onboarding import (
    LeadInput,
    LeadValidationError,
    clean_optional,
    create_lead,
    list_leads,
)
from app.security import (
    clear_session_cookie,
    create_session_value,
    require_admin,
    set_session_cookie,
    verify_admin_password,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/admin", status_code=303)


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

    response = RedirectResponse(url="/admin", status_code=303)
    set_session_cookie(response, create_session_value(settings), settings)
    return response


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request) -> Response:
    settings = get_settings()
    redirect = require_admin(request, settings)
    if redirect:
        return redirect
    return render_admin(request)


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
        return render_admin(
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
    return RedirectResponse(url="/admin", status_code=303)


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


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response


def render_admin(
    request: Request,
    error: Optional[str] = None,
    form: Optional[dict[str, str]] = None,
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "error": error,
            "form": form or {},
            "leads": list_leads(),
        },
        status_code=status_code,
    )
