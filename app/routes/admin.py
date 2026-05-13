from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app.config import get_settings
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
        "login.html",
        {"request": request, "error": None},
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(default="")) -> Response:
    settings = get_settings()
    if not settings.admin_password:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Admin password is not configured.",
            },
            status_code=500,
        )
    if not verify_admin_password(password, settings):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Invalid password."},
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
    return templates.TemplateResponse("admin.html", {"request": request})


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    clear_session_cookie(response)
    return response
