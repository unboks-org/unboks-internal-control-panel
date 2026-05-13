import hashlib
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

from app.config import get_settings


LEAD_STATUSES = {
    "lead_created",
    "email_pending",
    "email_sent",
    "form_started",
    "form_submitted",
    "tenant_ready",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class OnboardingLead:
    id: int
    email: str
    business_name: Optional[str]
    contact_name: Optional[str]
    language: Optional[str]
    notes: Optional[str]
    status: str
    created_at: str
    updated_at: str
    onboarding_token_hash: Optional[str]
    token_created_at: Optional[str]
    token_expires_at: Optional[str]
    email_sent_at: Optional[str]
    email_last_error: Optional[str]


@dataclass(frozen=True)
class LeadInput:
    email: str
    business_name: Optional[str]
    contact_name: Optional[str]
    language: Optional[str]
    notes: Optional[str]


class LeadValidationError(ValueError):
    pass


class LeadNotFoundError(ValueError):
    pass


def _connect() -> sqlite3.Connection:
    settings = get_settings()
    db_path = Path(settings.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                email_key TEXT NOT NULL UNIQUE,
                business_name TEXT,
                contact_name TEXT,
                language TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'lead_created',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                CHECK (
                    status IN (
                        'lead_created',
                        'email_pending',
                        'email_sent',
                        'form_started',
                        'form_submitted',
                        'tenant_ready'
                    )
                )
            )
            """
        )
        existing = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(onboarding_leads)").fetchall()
        }
        migrations = {
            "onboarding_token_hash": "ALTER TABLE onboarding_leads ADD COLUMN onboarding_token_hash TEXT",
            "token_created_at": "ALTER TABLE onboarding_leads ADD COLUMN token_created_at TEXT",
            "token_expires_at": "ALTER TABLE onboarding_leads ADD COLUMN token_expires_at TEXT",
            "email_sent_at": "ALTER TABLE onboarding_leads ADD COLUMN email_sent_at TEXT",
            "email_last_error": "ALTER TABLE onboarding_leads ADD COLUMN email_last_error TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        raise LeadValidationError("Email is required.")
    if not EMAIL_RE.match(normalized):
        raise LeadValidationError("Enter a valid email address.")
    return normalized


def clean_optional(value: str) -> Optional[str]:
    stripped = value.strip()
    return stripped if stripped else None


def create_lead(lead: LeadInput) -> OnboardingLead:
    init_db()
    email_key = validate_email(lead.email)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with _connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO onboarding_leads (
                    email,
                    email_key,
                    business_name,
                    contact_name,
                    language,
                    notes,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'lead_created', ?, ?)
                """,
                (
                    lead.email.strip(),
                    email_key,
                    lead.business_name,
                    lead.contact_name,
                    lead.language,
                    lead.notes,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM onboarding_leads WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise LeadValidationError("An onboarding lead for this email already exists.") from exc

    return row_to_lead(row)


def get_lead(lead_id: int) -> OnboardingLead:
    init_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM onboarding_leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if row is None:
        raise LeadNotFoundError("Onboarding lead not found.")
    return row_to_lead(row)


def list_leads() -> list[OnboardingLead]:
    init_db()
    with _connect() as conn:
        rows: Iterable[sqlite3.Row] = conn.execute(
            """
            SELECT * FROM onboarding_leads
            ORDER BY datetime(created_at) DESC, id DESC
            """
        ).fetchall()
    return [row_to_lead(row) for row in rows]


def create_or_refresh_token(lead_id: int) -> tuple[OnboardingLead, str]:
    init_db()
    token = secrets.token_urlsafe(48)
    token_hash = hash_token(token)
    now = utc_now()
    expires_at = (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(
        timespec="seconds"
    )
    with _connect() as conn:
        conn.execute(
            """
            UPDATE onboarding_leads
            SET onboarding_token_hash = ?,
                token_created_at = ?,
                token_expires_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (token_hash, now, expires_at, now, lead_id),
        )
        row = conn.execute(
            "SELECT * FROM onboarding_leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if row is None:
        raise LeadNotFoundError("Onboarding lead not found.")
    return row_to_lead(row), token


def update_email_result(
    lead_id: int,
    status: str,
    email_sent_at: Optional[str],
    email_last_error: Optional[str],
) -> OnboardingLead:
    if status not in LEAD_STATUSES:
        raise LeadValidationError("Invalid lead status.")
    now = utc_now()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE onboarding_leads
            SET status = ?,
                email_sent_at = ?,
                email_last_error = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, email_sent_at, email_last_error, now, lead_id),
        )
        row = conn.execute(
            "SELECT * FROM onboarding_leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if row is None:
        raise LeadNotFoundError("Onboarding lead not found.")
    return row_to_lead(row)


def find_lead_by_token(token: str) -> Optional[OnboardingLead]:
    token_hash = hash_token(token)
    init_db()
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM onboarding_leads
            WHERE onboarding_token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
    if row is None:
        return None
    lead = row_to_lead(row)
    if lead.token_expires_at and lead.token_expires_at < utc_now():
        return None
    return lead


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_lead(row: sqlite3.Row) -> OnboardingLead:
    return OnboardingLead(
        id=int(row["id"]),
        email=str(row["email"]),
        business_name=row["business_name"],
        contact_name=row["contact_name"],
        language=row["language"],
        notes=row["notes"],
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        onboarding_token_hash=row["onboarding_token_hash"],
        token_created_at=row["token_created_at"],
        token_expires_at=row["token_expires_at"],
        email_sent_at=row["email_sent_at"],
        email_last_error=row["email_last_error"],
    )
