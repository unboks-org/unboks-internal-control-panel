from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.signup_service import create_public_signup_tenant
from app.tenants import TenantCreateError


router = APIRouter(tags=["signup"])


def _signup_error_html(message: str) -> str:
    safe = (
        message.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Unboks signup</title>
    <style>
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #f8f9fb;
        color: #202124;
        font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(92vw, 460px);
        border: 1px solid #e8eaed;
        border-radius: 16px;
        background: #fff;
        box-shadow: 0 10px 40px rgba(60, 64, 67, 0.08);
        padding: 28px;
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 22px;
        font-weight: 650;
      }}
      p {{
        margin: 0 0 18px;
        color: #5f6368;
        font-size: 14px;
        line-height: 1.5;
      }}
      a {{
        color: #1a73e8;
        font-size: 14px;
        text-decoration: none;
      }}
    </style>
  </head>
  <body>
    <main>
      <h1>Signup could not be completed</h1>
      <p>{safe}</p>
      <a href="https://unboks.org/signup">Return to signup</a>
    </main>
  </body>
</html>"""


@router.get("/signup", response_class=HTMLResponse)
def signup_fallback_form() -> str:
    """Small no-JS fallback. The public Nr1 site owns the polished form."""
    return """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Start Unboks</title>
  </head>
  <body>
    <form method="post" action="/signup">
      <label>Full name <input name="full_name" required></label><br>
      <label>Business name <input name="business_name" required></label><br>
      <label>Email <input name="email" type="email" required></label><br>
      <label>Phone <input name="phone" type="tel"></label><br>
      <button type="submit">Create my free account</button>
    </form>
  </body>
</html>"""


@router.post("/signup")
async def public_signup_submit(
    request: Request,
    full_name: str = Form(default=""),
    business_name: str = Form(default=""),
    email: str = Form(default=""),
    phone: str = Form(default=""),
):
    """Create a tenant from the public free-trial form and redirect to Nr2."""
    settings = get_settings()
    try:
        result = create_public_signup_tenant(
            full_name=full_name,
            business_name=business_name,
            email=email,
            phone=phone,
            settings=settings,
        )
    except TenantCreateError as exc:
        return HTMLResponse(_signup_error_html(str(exc)), status_code=400)
    except Exception:
        return HTMLResponse(
            _signup_error_html(
                "We could not create the workspace right now. Please contact Unboks."
            ),
            status_code=500,
        )

    return RedirectResponse(url=result.dashboard_url, status_code=303)
