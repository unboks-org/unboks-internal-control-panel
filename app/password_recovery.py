from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import audit_log
from app.config import Settings, get_settings
from app.emailer import send_email, smtp_is_configured
from app.provisioning import AutoProvisionResult, queue_tenant_host_action
from app.tenants import get_tenant, tenant_contact_details, validate_slug


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
TOKEN_TTL_MINUTES = 60
MAX_IP_REQUESTS_PER_HOUR = 5
MAX_EMAIL_REQUESTS_PER_DAY = 3


@dataclass(frozen=True)
class PasswordResetToken:
    id: str
    tenant_id: str
    email: str
    token_hash: str
    created_at: str
    expires_at: str
    used_at: str | None


@dataclass(frozen=True)
class ResetApplyResult:
    ok: bool
    status: str
    message: str
    tenant_id: str = ""
    job_id: str | None = None


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _email_key(email: str) -> str:
    return email.strip().lower()


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS password_reset_tokens (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                email TEXT NOT NULL,
                email_key TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE,
                requested_ip TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash
            ON password_reset_tokens (token_hash)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_email_created
            ON password_reset_tokens (email_key, created_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_ip_created
            ON password_reset_tokens (requested_ip, created_at)
            """
        )


def _row_to_token(row: sqlite3.Row | None) -> PasswordResetToken | None:
    if row is None:
        return None
    return PasswordResetToken(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        email=str(row["email"]),
        token_hash=str(row["token_hash"]),
        created_at=str(row["created_at"]),
        expires_at=str(row["expires_at"]),
        used_at=row["used_at"],
    )


def _rate_limited(email: str, ip_address: str) -> bool:
    init_db()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    hour_ago = (now - timedelta(hours=1)).isoformat()
    day_ago = (now - timedelta(days=1)).isoformat()
    with _connect() as conn:
        ip_count = conn.execute(
            """
            SELECT COUNT(*) FROM password_reset_tokens
            WHERE requested_ip = ? AND created_at >= ?
            """,
            (ip_address, hour_ago),
        ).fetchone()[0]
        email_count = conn.execute(
            """
            SELECT COUNT(*) FROM password_reset_tokens
            WHERE email_key = ? AND created_at >= ?
            """,
            (_email_key(email), day_ago),
        ).fetchone()[0]
    return ip_count >= MAX_IP_REQUESTS_PER_HOUR or email_count >= MAX_EMAIL_REQUESTS_PER_DAY


def _tenant_email_matches(tenant_id: str, email: str) -> bool:
    details = tenant_contact_details(tenant_id)
    configured = _email_key(details.get("email", ""))
    return bool(configured and hmac.compare_digest(configured, _email_key(email)))


def _build_reset_email(*, first_name: str, reset_url: str) -> tuple[str, str]:
    name = first_name.strip() or "there"
    subject = "Reset your Unboks password"
    body = f"""Hi {name},

Someone requested a password reset for your Unboks dashboard.

Reset your password here:
{reset_url}

This link expires in {TOKEN_TTL_MINUTES} minutes and can only be used once.

If you did not request this, you can ignore this email.

Kind regards,
The Unboks team
"""
    return subject, body


def request_reset(
    *,
    tenant_id: str,
    email: str,
    ip_address: str,
    settings: Settings | None = None,
    actor: str = "tenant_user",
) -> None:
    """Create and send a reset link when the tenant/email pair is valid.

    Public callers always show a generic response; this function mirrors that
    behavior by returning None for unknown tenant, email mismatch, rate limits,
    and SMTP failures. Audit events carry safe operational state without raw
    tokens or passwords.
    """
    settings = settings or get_settings()
    clean_email = email.strip()
    try:
        safe_tenant = validate_slug(tenant_id)
    except Exception:
        return
    tenant = get_tenant(safe_tenant)
    if tenant is None or not EMAIL_RE.match(clean_email):
        return

    if _rate_limited(clean_email, ip_address):
        audit_log.record_event(
            tenant_id=safe_tenant,
            action="password_reset.rate_limited",
            result="blocked",
            safe_summary="Password reset request rate limited.",
            metadata={"ip": ip_address, "email_key": _email_key(clean_email)},
            actor=actor,
        )
        return
    if not _tenant_email_matches(safe_tenant, clean_email):
        audit_log.record_event(
            tenant_id=safe_tenant,
            action="password_reset.request_ignored",
            result="ignored",
            safe_summary="Password reset request did not match tenant contact email.",
            metadata={"email_key": _email_key(clean_email)},
            actor=actor,
        )
        return

    raw_token = secrets.token_urlsafe(40)
    token_hash = _hash_token(raw_token)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(minutes=TOKEN_TTL_MINUTES)
    reset_id = f"pr_{secrets.token_urlsafe(18)}"
    init_db()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO password_reset_tokens (
                id, tenant_id, email, email_key, token_hash, requested_ip,
                created_at, expires_at, used_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                reset_id,
                safe_tenant,
                clean_email,
                _email_key(clean_email),
                token_hash,
                ip_address,
                now.isoformat(),
                expires.isoformat(),
            ),
        )

    details = tenant_contact_details(safe_tenant)
    reset_url = f"{settings.base_url}/password/reset/{raw_token}"
    subject, body = _build_reset_email(
        first_name=details.get("first_name", ""),
        reset_url=reset_url,
    )
    if not smtp_is_configured(settings):
        audit_log.record_event(
            tenant_id=safe_tenant,
            action="password_reset.email_failed",
            result="failed",
            safe_summary="Password reset email could not be sent because SMTP is not configured.",
            actor=actor,
        )
        return
    try:
        send_email(clean_email, subject, body, settings)
    except Exception as exc:
        audit_log.record_event(
            tenant_id=safe_tenant,
            action="password_reset.email_failed",
            result="failed",
            safe_summary="Password reset email send failed.",
            metadata={"error": str(exc)[:160]},
            actor=actor,
        )
        return

    audit_log.record_event(
        tenant_id=safe_tenant,
        action="password_reset.email_sent",
        result="ok",
        safe_summary="Password reset email sent.",
        metadata={"email_key": _email_key(clean_email), "expires_at": expires.isoformat()},
        actor=actor,
    )


def get_valid_token(raw_token: str) -> PasswordResetToken | None:
    if not raw_token or len(raw_token) < 20:
        return None
    init_db()
    token_hash = _hash_token(raw_token)
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    token = _row_to_token(row)
    if token is None or token.used_at:
        return None
    try:
        if _parse_dt(token.expires_at) < datetime.now(timezone.utc):
            return None
    except ValueError:
        return None
    return token


def validate_new_password(password: str, confirm: str) -> str:
    value = password.strip()
    if value != confirm.strip():
        raise ValueError("Passwords do not match.")
    if len(value) < 12:
        raise ValueError("Use at least 12 characters.")
    if re.search(r"https?://|www\\.", value, flags=re.I):
        raise ValueError("Password cannot contain a URL.")
    classes = sum(
        bool(pattern.search(value))
        for pattern in (
            re.compile(r"[a-z]"),
            re.compile(r"[A-Z]"),
            re.compile(r"[0-9]"),
            re.compile(r"[^A-Za-z0-9]"),
        )
    )
    if classes < 3:
        raise ValueError("Use a stronger password with at least three character types.")
    return value


def mark_token_used(token: PasswordResetToken) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "UPDATE password_reset_tokens SET used_at = ? WHERE id = ? AND used_at IS NULL",
            (utc_now(), token.id),
        )


def apply_reset(raw_token: str, new_password: str, confirm_password: str) -> ResetApplyResult:
    token = get_valid_token(raw_token)
    if token is None:
        audit_log.record_event(
            action="password_reset.invalid_token",
            result="failed",
            safe_summary="Invalid, expired, or already used reset token submitted.",
            actor="tenant_user",
        )
        return ResetApplyResult(False, "invalid", "This reset link is invalid or expired.")
    try:
        clean_password = validate_new_password(new_password, confirm_password)
    except ValueError as exc:
        return ResetApplyResult(False, "validation", str(exc), tenant_id=token.tenant_id)

    result: AutoProvisionResult = queue_tenant_host_action(
        slug=token.tenant_id,
        action="reset_dashboard_password",
        dashboard_url=f"https://dashboard.unboks.org/login?workspace={token.tenant_id}",
        new_password=clean_password,
    )
    if result.status in {"failed", "disabled"}:
        audit_log.record_event(
            tenant_id=token.tenant_id,
            action="password_reset.apply_failed",
            result="failed",
            safe_summary=result.message,
            metadata={"job_id": result.job_id, "status": result.status},
            actor="tenant_user",
        )
        return ResetApplyResult(False, result.status, result.message, token.tenant_id, result.job_id)

    mark_token_used(token)
    audit_log.record_event(
        tenant_id=token.tenant_id,
        action="password_reset.completed",
        result="ok" if result.status == "succeeded" else "queued",
        safe_summary="Dashboard password reset accepted.",
        metadata={"job_id": result.job_id, "status": result.status},
        actor="tenant_user",
    )
    message = (
        "Your password has been reset. You can sign in with the new password."
        if result.status == "succeeded"
        else "Your password reset is being applied. Try signing in again shortly."
    )
    return ResetApplyResult(True, result.status, message, token.tenant_id, result.job_id)


def safe_request_metadata(request_headers: dict[str, Any], fallback_ip: str) -> str:
    forwarded = str(request_headers.get("x-forwarded-for") or "").split(",", 1)[0].strip()
    return forwarded or fallback_ip or "unknown"
