import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
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


@dataclass(frozen=True)
class LeadInput:
    email: str
    business_name: Optional[str]
    contact_name: Optional[str]
    language: Optional[str]
    notes: Optional[str]


class LeadValidationError(ValueError):
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
    )
