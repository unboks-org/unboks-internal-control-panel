import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Optional

from app.config import Settings, get_settings
from app.onboarding import (
    create_or_refresh_token,
    get_lead,
    update_email_result,
    utc_now,
)


SUBJECT = "Welcome to Unboks — your onboarding link"


@dataclass(frozen=True)
class EmailDraft:
    subject: str
    body: str
    onboarding_link: str


@dataclass(frozen=True)
class EmailSendResult:
    lead_id: int
    sent: bool
    smtp_configured: bool
    error: Optional[str]
    draft: EmailDraft


def build_onboarding_link(token: str, settings: Settings) -> str:
    return f"{settings.base_url}/onboarding/{token}"


def build_welcome_email(onboarding_link: str) -> EmailDraft:
    body = f"""Hi,

Welcome to Unboks.

Before we activate your AI inbox, we need a few details about your business so the Agent can answer customers accurately.

Please complete your onboarding here:
{onboarding_link}

It takes around 10-15 minutes. We will ask about your services, prices, opening hours, policies, frequently asked questions, tone of voice, and when the Agent should escalate to a human.

After we receive your answers, we will prepare your setup and send your dashboard access.

Kind regards,
The Unboks team
"""
    return EmailDraft(subject=SUBJECT, body=body, onboarding_link=onboarding_link)


def smtp_is_configured(settings: Settings) -> bool:
    return bool(
        settings.smtp_host
        and settings.smtp_port
        and settings.smtp_username
        and settings.smtp_password
        and settings.email_from
    )


def prepare_or_send_onboarding_email(lead_id: int) -> EmailSendResult:
    settings = get_settings()
    lead = get_lead(lead_id)
    lead, raw_token = create_or_refresh_token(lead.id)
    onboarding_link = build_onboarding_link(raw_token, settings)
    draft = build_welcome_email(onboarding_link)

    if not smtp_is_configured(settings):
        update_email_result(
            lead.id,
            status="email_pending",
            email_sent_at=None,
            email_last_error="Email not configured.",
        )
        return EmailSendResult(
            lead_id=lead.id,
            sent=False,
            smtp_configured=False,
            error="Email not configured.",
            draft=draft,
        )

    try:
        send_email(
            to_email=lead.email,
            subject=draft.subject,
            body=draft.body,
            settings=settings,
        )
    except Exception as exc:
        update_email_result(
            lead.id,
            status="email_pending",
            email_sent_at=None,
            email_last_error="Email send failed.",
        )
        return EmailSendResult(
            lead_id=lead.id,
            sent=False,
            smtp_configured=True,
            error=f"Email send failed: {exc}",
            draft=draft,
        )

    update_email_result(
        lead.id,
        status="email_sent",
        email_sent_at=utc_now(),
        email_last_error=None,
    )
    return EmailSendResult(
        lead_id=lead.id,
        sent=True,
        smtp_configured=True,
        error=None,
        draft=draft,
    )


def send_email(to_email: str, subject: str, body: str, settings: Settings) -> None:
    if not smtp_is_configured(settings):
        raise RuntimeError("Email is not configured.")

    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)

    if settings.smtp_use_tls:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
