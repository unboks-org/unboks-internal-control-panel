from fastapi import APIRouter, Form, Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from app.onboarding import (
    LeadNotFoundError,
    LeadValidationError,
    get_intake_progress,
    save_intake_answer,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/onboarding/{token}", response_class=HTMLResponse)
def onboarding_intake(request: Request, token: str) -> HTMLResponse:
    progress = get_intake_progress(token)
    if progress is None:
        return templates.TemplateResponse(
            request,
            "onboarding_invalid.html",
            {},
            status_code=404,
        )
    return templates.TemplateResponse(
        request,
        "onboarding_intake.html",
        {"token": token, "progress": progress, "error": None},
    )


@router.post("/onboarding/{token}", response_class=HTMLResponse)
def onboarding_answer(
    request: Request,
    token: str,
    question_key: str = Form(default=""),
    answer: str = Form(default=""),
) -> Response:
    try:
        save_intake_answer(token, question_key, answer)
    except LeadNotFoundError:
        return templates.TemplateResponse(
            request,
            "onboarding_invalid.html",
            {},
            status_code=404,
        )
    except LeadValidationError as exc:
        progress = get_intake_progress(token)
        if progress is None:
            return templates.TemplateResponse(
                request,
                "onboarding_invalid.html",
                {},
                status_code=404,
            )
        return templates.TemplateResponse(
            request,
            "onboarding_intake.html",
            {
                "token": token,
                "progress": progress,
                "error": str(exc),
                "answer": answer,
            },
            status_code=400,
        )
    return RedirectResponse(url=f"/onboarding/{token}", status_code=303)
