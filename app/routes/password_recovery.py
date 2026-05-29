from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.config import get_settings
from app.password_recovery import (
    apply_reset,
    get_valid_token,
    request_reset,
    safe_request_metadata,
)


router = APIRouter(tags=["password-recovery"])


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _page(title: str, body: str) -> str:
    safe_title = _escape(title)
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{safe_title}</title>
    <style>
      :root {{ color-scheme: light; }}
      body {{
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        background: #f7f8fb;
        color: #202124;
        font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}
      main {{
        width: min(92vw, 440px);
        border: 1px solid #e2e5ea;
        border-radius: 18px;
        background: #fff;
        box-shadow: 0 18px 48px rgba(60, 64, 67, 0.10);
        padding: 28px;
      }}
      h1 {{ margin: 0 0 8px; font-size: 23px; line-height: 1.2; }}
      p {{ margin: 0 0 18px; color: #5f6368; font-size: 14px; line-height: 1.5; }}
      form {{ display: grid; gap: 14px; margin-top: 20px; }}
      label {{ display: grid; gap: 6px; font-size: 13px; font-weight: 650; }}
      input {{
        height: 44px;
        border: 1px solid #d7dce3;
        border-radius: 12px;
        padding: 0 12px;
        font: inherit;
        outline: none;
      }}
      input:focus {{ border-color: #1a73e8; box-shadow: 0 0 0 3px rgba(26,115,232,.14); }}
      button {{
        height: 44px;
        border: 0;
        border-radius: 12px;
        background: #1a73e8;
        color: #fff;
        font-weight: 700;
        cursor: pointer;
      }}
      .error {{
        border: 1px solid #f5c2c7;
        background: #fff5f5;
        color: #b42318;
        border-radius: 12px;
        padding: 12px;
        font-size: 13px;
      }}
      a {{ color: #1a73e8; text-decoration: none; font-size: 14px; }}
    </style>
  </head>
  <body><main>{body}</main></body>
</html>"""


@router.get("/password/forgot", response_class=HTMLResponse)
def forgot_password_form(workspace: str = "", email: str = "") -> str:
    return _page(
        "Reset your Unboks password",
        f"""
        <h1>Reset your password</h1>
        <p>Enter your workspace and email address. If this email exists, we will send password reset instructions.</p>
        <form method="post" action="/password/forgot">
          <label>Workspace
            <input name="workspace" value="{_escape(workspace)}" autocomplete="organization" required>
          </label>
          <label>Email
            <input name="email" type="email" value="{_escape(email)}" autocomplete="email" required>
          </label>
          <button type="submit">Send reset instructions</button>
        </form>
        """,
    )


@router.post("/password/forgot", response_class=HTMLResponse)
def forgot_password_submit(
    request: Request,
    workspace: str = Form(default=""),
    email: str = Form(default=""),
) -> str:
    settings = get_settings()
    request_reset(
        tenant_id=workspace,
        email=email,
        ip_address=safe_request_metadata(
            dict(request.headers),
            request.client.host if request.client else "unknown",
        ),
        settings=settings,
    )
    return _page(
        "Check your email",
        """
        <h1>Check your email</h1>
        <p>If this email exists, we sent password reset instructions.</p>
        <a href="https://dashboard.unboks.org/login">Back to sign in</a>
        """,
    )


@router.get("/password/reset/{token}", response_class=HTMLResponse)
def reset_password_form(token: str) -> str:
    if get_valid_token(token) is None:
        return _page(
            "Reset link expired",
            """
            <h1>This reset link is invalid or expired</h1>
            <p>Please request a new password reset from the sign-in page.</p>
            <a href="/password/forgot">Request a new link</a>
            """,
        )
    return _page(
        "Choose a new password",
        f"""
        <h1>Choose a new password</h1>
        <p>Use at least 12 characters. The reset link can only be used once.</p>
        <form method="post" action="/password/reset/{_escape(token)}">
          <label>New password
            <input name="password" type="password" autocomplete="new-password" required minlength="12">
          </label>
          <label>Confirm new password
            <input name="confirm_password" type="password" autocomplete="new-password" required minlength="12">
          </label>
          <button type="submit">Reset password</button>
        </form>
        """,
    )


@router.post("/password/reset/{token}", response_class=HTMLResponse)
def reset_password_submit(
    token: str,
    password: str = Form(default=""),
    confirm_password: str = Form(default=""),
) -> str:
    result = apply_reset(token, password, confirm_password)
    if not result.ok:
        return _page(
            "Password reset failed",
            f"""
            <h1>Password reset failed</h1>
            <div class="error">{_escape(result.message)}</div>
            <p>Request a new link if this one expired.</p>
            <a href="/password/forgot">Request a new link</a>
            """,
        )
    return _page(
        "Password reset complete",
        f"""
        <h1>Password reset complete</h1>
        <p>{_escape(result.message)}</p>
        <a href="https://dashboard.unboks.org/login?workspace={_escape(result.tenant_id)}">Back to sign in</a>
        """,
    )
