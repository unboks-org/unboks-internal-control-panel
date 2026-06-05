from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import audit_log
from app.config import get_settings
from app.emailer import send_email, smtp_is_configured
from app.public_signup_requests import (
    client_ip_from_headers,
    create_signup_request,
    mark_provisioned,
    mark_verified,
    update_signup_request,
    utc_now,
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


def _signup_check_email_html(*, email: str, expires_hours: int) -> str:
    safe_email = _escape(email.strip())
    email_sentence = (
        f'We sent a confirmation link to <strong>{safe_email}</strong>.'
        if safe_email
        else "We sent a confirmation link to your email address."
    )
    safe_expires_hours = _escape(str(expires_hours))
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Check your email | Unboks</title>
    <style>
      :root {{
        color-scheme: light;
        --blue: #2563eb;
        --ink: #0f172a;
        --muted: #5f6368;
        --line: #e1e7ef;
        --soft: #f7faff;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background:
          radial-gradient(circle at 50% 0%, rgba(37, 99, 235, 0.10), transparent 34%),
          #f6f8fb;
        color: var(--ink);
        font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        padding: 24px 14px;
      }}
      main {{
        width: min(100%, 560px);
      }}
      .brand {{
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 10px;
        margin: 0 0 18px;
        font-size: 30px;
        font-weight: 800;
        letter-spacing: -0.03em;
      }}
      .brand-mark {{
        width: 38px;
        height: 38px;
        border-radius: 12px;
        display: grid;
        place-items: center;
        background: var(--blue);
        color: #fff;
        font-weight: 800;
      }}
      .card {{
        border: 1px solid var(--line);
        border-radius: 18px;
        background: #fff;
        box-shadow: 0 16px 48px rgba(15, 23, 42, 0.08);
        padding: 34px;
        text-align: center;
      }}
      .icon {{
        width: 74px;
        height: 74px;
        border-radius: 22px;
        display: grid;
        place-items: center;
        margin: 0 auto 20px;
        background: linear-gradient(180deg, #eef5ff, #ffffff);
        border: 1px solid #d9e7ff;
        color: var(--blue);
        font-size: 34px;
      }}
      h1 {{
        margin: 0 0 12px;
        font-size: clamp(26px, 5vw, 34px);
        line-height: 1.12;
        letter-spacing: -0.03em;
      }}
      p {{
        margin: 0;
        font-size: 16px;
        line-height: 1.58;
        color: #1f2937;
      }}
      .lead {{
        max-width: 430px;
        margin: 0 auto;
      }}
      .helper {{
        margin: 24px 0;
        padding: 16px 18px;
        border: 1px solid #cfe0ff;
        border-radius: 12px;
        background: var(--soft);
        color: #1f2937;
        text-align: left;
      }}
      .helper strong {{
        color: var(--ink);
      }}
      .actions {{
        display: flex;
        justify-content: center;
        flex-wrap: wrap;
        gap: 10px 16px;
        margin: 4px 0 28px;
      }}
      .actions a {{
        color: var(--blue);
        font-weight: 650;
        font-size: 14px;
        text-decoration: none;
      }}
      .steps {{
        border-top: 1px solid var(--line);
        padding-top: 24px;
      }}
      .steps h2 {{
        margin: 0 0 16px;
        font-size: 15px;
        letter-spacing: 0.01em;
      }}
      .step-grid {{
        display: grid;
        grid-template-columns: repeat(3, 1fr);
        gap: 14px;
      }}
      .step {{
        min-width: 0;
      }}
      .step-number {{
        width: 28px;
        height: 28px;
        display: grid;
        place-items: center;
        margin: 0 auto 8px;
        border-radius: 999px;
        background: var(--blue);
        color: #fff;
        font-size: 13px;
        font-weight: 800;
      }}
      .step p {{
        font-size: 13px;
        line-height: 1.35;
        color: #111827;
      }}
      footer {{
        margin-top: 18px;
        text-align: center;
        color: #6b7280;
        font-size: 13px;
      }}
      @media (max-width: 560px) {{
        .card {{ padding: 26px 20px; }}
        .step-grid {{ grid-template-columns: 1fr; text-align: left; }}
        .step {{
          display: grid;
          grid-template-columns: 32px 1fr;
          align-items: center;
          gap: 10px;
        }}
        .step-number {{ margin: 0; }}
      }}
    </style>
  </head>
  <body>
    <main>
      <div class="brand" aria-label="Unboks">
        <span class="brand-mark">U</span>
        <span>unboks</span>
      </div>
      <section class="card" aria-labelledby="check-email-title">
        <div class="icon" aria-hidden="true">✓</div>
        <h1 id="check-email-title">Check your email</h1>
        <p class="lead">
          {email_sentence}
          <br>
          Confirm your email address to continue setting up your <strong>14-day Unboks trial</strong>.
        </p>
        <div class="helper" role="note">
          <p><strong>The link expires in {safe_expires_hours} hours.</strong><br>If you do not see it, check your spam or promotions folder.</p>
        </div>
        <nav class="actions" aria-label="Signup next actions">
          <a href="https://unboks.org">Back to Unboks website</a>
          <a href="mailto:calvin@gaimin.io?subject=Unboks%20signup%20help">Need help? Contact us</a>
        </nav>
        <section class="steps" aria-labelledby="what-next-title">
          <h2 id="what-next-title">What happens next?</h2>
          <div class="step-grid">
            <div class="step"><span class="step-number">1</span><p>Confirm your email.</p></div>
            <div class="step"><span class="step-number">2</span><p>We activate your workspace.</p></div>
            <div class="step"><span class="step-number">3</span><p>You receive your login details and can start your trial.</p></div>
          </div>
        </section>
      </section>
      <footer>&copy; 2026 Unboks. All rights reserved.</footer>
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

Please confirm your Unboks signup.

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
                <p style="margin:0 0 18px;font-size:16px;line-height:1.55;color:#111827;font-weight:700;">
                  Please confirm your Unboks signup.
                </p>
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


def _build_admin_signup_alert_email(
    *,
    signup_id: str,
    full_name: str,
    business_name: str,
    email: str,
    phone: str,
    created_at: str,
    status: str,
    review_url: str,
) -> tuple[str, str]:
    subject = f"New Unboks free-trial signup: {business_name}"
    body = f"""Admin alert: New free-trial signup received.

Prospect:
- Name: {full_name}
- Email: {email}
- Business: {business_name}
- Phone: {phone or "Not provided"}
- Website: Not provided
- Timestamp: {created_at}
- Status: {status}
- Signup ID: {signup_id}

Review in Nr3:
{review_url}

This alert does not include verification token hashes or secrets.
"""
    return subject, body


def _send_admin_signup_alert(signup_request) -> None:
    """Best-effort admin notification for public trial requests.

    This must never fail the public signup flow. Missing SMTP or missing
    admin recipient is stored visibly for Nr3 and recorded in audit logs.
    """
    settings = get_settings()
    review_url = f"{settings.base_url}/admin/signups/{signup_request.id}"
    metadata = {
        "signup_id": signup_request.id,
        "email": signup_request.email,
        "business_name": signup_request.business_name,
        "status": signup_request.status,
    }

    if not smtp_is_configured(settings):
        update_signup_request(
            signup_request.id,
            settings,
            admin_alert_status="not_configured",
            admin_alert_sent_at=None,
            admin_alert_error="SMTP is not configured.",
            admin_alert_recipient=settings.admin_alert_email or "",
        )
        audit_log.record_event(
            action="signup_admin_alert_skipped",
            result="warning",
            safe_summary="Admin signup alert skipped because SMTP is not configured.",
            metadata=metadata,
        )
        return

    if not settings.admin_alert_email:
        update_signup_request(
            signup_request.id,
            settings,
            admin_alert_status="not_configured",
            admin_alert_sent_at=None,
            admin_alert_error="Admin alert recipient is not configured.",
            admin_alert_recipient="",
        )
        audit_log.record_event(
            action="signup_admin_alert_skipped",
            result="warning",
            safe_summary="Admin signup alert skipped because recipient is not configured.",
            metadata=metadata,
        )
        return

    subject, body = _build_admin_signup_alert_email(
        signup_id=signup_request.id,
        full_name=signup_request.full_name,
        business_name=signup_request.business_name,
        email=signup_request.email,
        phone=signup_request.phone,
        created_at=signup_request.created_at,
        status=signup_request.status,
        review_url=review_url,
    )
    try:
        send_email(settings.admin_alert_email, subject, body, settings)
    except Exception as exc:
        update_signup_request(
            signup_request.id,
            settings,
            admin_alert_status="failed",
            admin_alert_sent_at=None,
            admin_alert_error="Admin alert email failed to send.",
            admin_alert_recipient=settings.admin_alert_email,
        )
        audit_log.record_event(
            action="signup_admin_alert_failed",
            result="error",
            safe_summary="Admin signup alert email failed to send.",
            metadata={**metadata, "error": type(exc).__name__},
        )
        return

    update_signup_request(
        signup_request.id,
        settings,
        admin_alert_status="sent",
        admin_alert_sent_at=utc_now().isoformat(),
        admin_alert_error=None,
        admin_alert_recipient=settings.admin_alert_email,
    )
    audit_log.record_event(
        action="signup_admin_alert_sent",
        result="ok",
        safe_summary="Admin signup alert email sent.",
        metadata={**metadata, "recipient": settings.admin_alert_email},
    )


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
            update_signup_request(
                signup_request.id,
                settings,
                confirmation_email_status="failed",
                confirmation_email_sent_at=None,
                confirmation_email_recipient=signup_request.email,
                confirmation_email_error="Confirmation email failed to send.",
            )
            audit_log.record_event(
                action="signup_confirmation_email_failed",
                result="error",
                safe_summary="Signup confirmation email failed to send.",
                metadata={
                    "signup_id": signup_request.id,
                    "email": signup_request.email,
                    "business_name": signup_request.business_name,
                },
            )
            _send_admin_signup_alert(signup_request)
            return HTMLResponse(
                _signup_info_html(
                    "Signup received",
                    "We received your signup request. Unboks will review it and contact you shortly.",
                ),
                status_code=202,
            )
        update_signup_request(
            signup_request.id,
            settings,
            confirmation_email_status="sent",
            confirmation_email_sent_at=utc_now().isoformat(),
            confirmation_email_recipient=signup_request.email,
            confirmation_email_error=None,
        )
        audit_log.record_event(
            action="signup_confirmation_email_sent",
            result="ok",
            safe_summary="Signup confirmation email sent.",
            metadata={
                "signup_id": signup_request.id,
                "email": signup_request.email,
                "business_name": signup_request.business_name,
                "recipient": signup_request.email,
            },
        )
        _send_admin_signup_alert(signup_request)
        return HTMLResponse(
            _signup_check_email_html(
                email=signup_request.email,
                expires_hours=settings.public_signup_verification_ttl_hours,
            ),
            status_code=202,
        )

    _send_admin_signup_alert(signup_request)
    update_signup_request(
        signup_request.id,
        settings,
        confirmation_email_status="not_configured",
        confirmation_email_sent_at=None,
        confirmation_email_recipient=signup_request.email,
        confirmation_email_error="SMTP is not configured.",
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
