from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates

from app.onboarding import find_lead_by_token

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/onboarding/{token}", response_class=HTMLResponse)
def onboarding_placeholder(request: Request, token: str) -> HTMLResponse:
    lead = find_lead_by_token(token)
    if lead is None:
        return templates.TemplateResponse(
            request,
            "onboarding_invalid.html",
            {},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "onboarding_placeholder.html",
        {"lead": lead},
    )
