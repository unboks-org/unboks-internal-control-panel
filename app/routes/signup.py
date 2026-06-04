from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import get_settings
from app.emailer import send_email, smtp_is_configured
from app.public_signup_requests import (
    client_ip_from_headers,
    create_signup_request,
    mark_provisioned,
    mark_verified,
)
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


def _signup_info_html(title: str, message: str) -> str:
    safe_title = _escape(title)
    safe_message = _escape(message)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title}</title>
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
        width: min(92vw, 500px);
        border: 1px solid #e8eaed;
        border-radius: 16px;
        background: #fff;
        box-shadow: 0 10px 40px rgba(60, 64, 67, 0.08);
        padding: 28px;
      }}
      h1 {{ margin: 0 0 8px; font-size: 22px; font-weight: 650; }}
      p {{ margin: 0; color: #5f6368; font-size: 14px; line-height: 1.5; }}
    </style>
  </head>
  <body>
    <main>
      <h1>{safe_title}</h1>
      <p>{safe_message}</p>
    </main>
  </body>
</html>"""


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _build_signup_verification_email(
    *,
    full_name: str,
    business_name: str,
    verify_url: str,
    expires_hours: int,
) -> tuple[str, str, str]:
    first_name = full_name.strip().split(" ", 1)[0] or "there"
    safe_first_name = _escape(first_name)
    safe_business_name = _escape(business_name)
    safe_verify_url = _escape(verify_url)
    safe_expires_hours = _escape(str(expires_hours))
    subject = "Confirm your Unboks signup"
    body = f"""Hi {first_name},

We received a request to create an Unboks workspace for {business_name}.

Please confirm your email address to continue setting up your workspace:

{verify_url}

This link expires in {expires_hours} hours.

After confirmation, Unboks will review and activate your workspace.

If you did not request this, you can ignore this email.

Kind regards,
The Unboks team
"""
    html_body = f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Confirm your Unboks signup</title>
  </head>
  <body style="margin:0;padding:0;background:#f6f8fb;color:#111827;font-family:Arial,Helvetica,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f6f8fb;margin:0;padding:28px 12px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:620px;background:#ffffff;border:1px solid #dfe5ee;border-radius:14px;overflow:hidden;">
            <tr>
              <td align="center" style="padding:30px 24px 24px;border-bottom:1px solid #e6ebf2;">
                <div style="font-size:34px;line-height:1;font-weight:800;letter-spacing:-0.02em;color:#0f172a;">unboks</div>
              </td>
            </tr>
            <tr>
              <td style="padding:32px 32px 8px;">
                <h1 style="margin:0 0 22px;font-size:22px;line-height:1.3;font-weight:700;color:#111827;">Hi {safe_first_name},</h1>
                <p style="margin:0 0 20px;font-size:16px;line-height:1.55;color:#111827;">
                  We received a request to create an Unboks workspace for <strong>{safe_business_name}</strong>.
                </p>
                <p style="margin:0 0 26px;font-size:16px;line-height:1.55;color:#111827;">
                  Please confirm your email address to continue setting up your workspace.
                </p>
                <table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="margin:0 0 30px;">
                  <tr>
                    <td align="center">
                      <a href="{safe_verify_url}" style="display:inline-block;background:#2563EB;color:#ffffff;text-decoration:none;border-radius:8px;padding:12px 20px;font-size:16px;line-height:1.25;font-weight:600;min-width:260px;text-align:center;">
                        Confirm email address
                      </a>
                    </td>
                  </tr>
                </table>
                <table role="presentation" cellspacing="0" cellpadding="0" width="100%" style="border-top:1px solid #e6ebf2;border-bottom:1px solid #e6ebf2;margin:0 0 22px;">
                  <tr>
                    <td style="padding:16px 0;font-size:15px;line-height:1.5;color:#111827;">
                      This link expires in <strong>{safe_expires_hours} hours</strong>.
                    </td>
                  </tr>
                </table>
                <p style="margin:0 0 22px;font-size:16px;line-height:1.55;color:#111827;">
                  After confirmation, Unboks will review and activate your workspace.
                </p>
                <p style="margin:0 0 28px;padding-top:20px;border-top:1px solid #e6ebf2;font-size:15px;line-height:1.55;color:#374151;">
                  If you did not request this, you can safely ignore this email.
                </p>
                <p style="margin:0 0 4px;font-size:15px;line-height:1.5;color:#111827;">Kind regards,</p>
                <p style="margin:0;font-size:15px;line-height:1.5;color:#111827;font-weight:700;">The Unboks team</p>
              </td>
            </tr>
            <tr>
              <td align="center" style="padding:24px 24px 28px;background:#f8fafc;color:#6b7280;font-size:13px;line-height:1.5;">
                &copy; 2026 Unboks. All rights reserved.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>"""
    return subject, body, html_body


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
      <label style="position:absolute;left:-10000px" aria-hidden="true">
        Website <input name="website" tabindex="-1" autocomplete="off">
      </label>
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
    website: str = Form(default=""),
):
    """Accept a public signup request without provisioning privileged resources."""
    settings = get_settings()
    if website.strip():
        return HTMLResponse(
            _signup_info_html(
                "Signup received",
                "Thanks. If the request is valid, Unboks will follow up shortly.",
            ),
            status_code=202,
        )
    try:
        signup_request = create_signup_request(
            full_name=full_name,
            business_name=business_name,
            email=email,
            phone=phone,
            ip_address=client_ip_from_headers(
                dict(request.headers),
                request.client.host if request.client else "unknown",
            ),
            user_agent=request.headers.get("user-agent", ""),
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

    if smtp_is_configured(settings):
        verify_url = f"{settings.base_url}/signup/verify/{signup_request.token}"
        subject, body, html_body = _build_signup_verification_email(
            full_name=signup_request.full_name,
            business_name=signup_request.business_name,
            verify_url=verify_url,
            expires_hours=settings.public_signup_verification_ttl_hours,
        )
        try:
            send_email(
                signup_request.email,
                subject,
                body,
                settings,
                html_body=html_body,
            )
        except Exception:
            return HTMLResponse(
                _signup_info_html(
                    "Signup received",
                    "We received your signup request. Unboks will review it and contact you shortly.",
                ),
                status_code=202,
            )
        return HTMLResponse(
            _signup_info_html(
                "Check your email",
                "We sent a confirmation link. Confirm your email before your workspace can be activated.",
            ),
            status_code=202,
        )

    return HTMLResponse(
        _signup_info_html(
            "Signup received",
            "We received your signup request. Unboks will review it and contact you shortly.",
        ),
        status_code=202,
    )


@router.get("/signup/verify/{token}", response_class=HTMLResponse)
def public_signup_verify(token: str):
    settings = get_settings()
    try:
        record = mark_verified(token, settings)
    except TenantCreateError as exc:
        return HTMLResponse(_signup_error_html(str(exc)), status_code=400)

    if not settings.public_signup_auto_provision_after_verify:
        return HTMLResponse(
            _signup_info_html(
                "Email confirmed",
                "Your email is confirmed. Unboks will activate your workspace after review.",
            ),
            status_code=200,
        )

    try:
        result = create_public_signup_tenant(
            full_name=str(record.get("full_name") or ""),
            business_name=str(record.get("business_name") or ""),
            email=str(record.get("email") or ""),
            phone=str(record.get("phone") or ""),
            settings=settings,
        )
    except TenantCreateError as exc:
        return HTMLResponse(_signup_error_html(str(exc)), status_code=400)
    except Exception:
        return HTMLResponse(
            _signup_error_html(
                "We could not activate the workspace right now. Please contact Unboks."
            ),
            status_code=500,
        )

    mark_provisioned(str(record.get("id") or ""), result.slug, settings)
    return RedirectResponse(url=result.dashboard_url, status_code=303)
