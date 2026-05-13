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
    "review_needs_changes",
    "review_approved",
    "tenant_ready",
}

REVIEW_DECISIONS = {
    "needs_changes": "review_needs_changes",
    "approved": "review_approved",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass(frozen=True)
class IntakeQuestion:
    key: str
    label: str
    help_text: str
    input_type: str = "textarea"
    required: bool = True


@dataclass(frozen=True)
class IntakeAnswer:
    question_key: str
    answer: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class IntakeProgress:
    lead: "OnboardingLead"
    answers: dict[str, IntakeAnswer]
    current_index: int
    complete: bool

    @property
    def total_questions(self) -> int:
        return len(INTAKE_QUESTIONS)

    @property
    def answered_count(self) -> int:
        return len(self.answers)

    @property
    def current_question(self) -> Optional[IntakeQuestion]:
        if self.complete:
            return None
        return INTAKE_QUESTIONS[self.current_index]


INTAKE_QUESTIONS = [
    IntakeQuestion(
        key="business_summary",
        label="What does your business do?",
        help_text="Give a plain-language description of the business and the customers you serve.",
    ),
    IntakeQuestion(
        key="services",
        label="Which services or products should the Agent know about?",
        help_text="List the main services, products, packages, or booking types.",
    ),
    IntakeQuestion(
        key="prices",
        label="What prices, rates, or quote rules should the Agent use?",
        help_text="Use 'unknown' if pricing depends on the situation.",
    ),
    IntakeQuestion(
        key="opening_hours",
        label="What are your opening hours and availability rules?",
        help_text="Include holidays, emergency availability, and appointment rules if relevant.",
    ),
    IntakeQuestion(
        key="policies",
        label="What policies should customers know before booking or buying?",
        help_text="Cancellations, deposits, refunds, travel fees, guarantees, or restrictions.",
    ),
    IntakeQuestion(
        key="faqs",
        label="What questions do customers ask most often?",
        help_text="Add common questions and the answers the Agent should give.",
    ),
    IntakeQuestion(
        key="tone",
        label="What tone of voice should the Agent use?",
        help_text="Examples: short and direct, friendly, formal, Dutch first, Spanish allowed.",
    ),
    IntakeQuestion(
        key="escalation_rules",
        label="When should the Agent escalate to a human?",
        help_text="List topics, customer types, risks, or situations that need human follow-up.",
    ),
]

INTAKE_QUESTION_KEYS = {question.key for question in INTAKE_QUESTIONS}


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
    review_status: Optional[str]
    review_notes: Optional[str]
    reviewed_at: Optional[str]


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
                        'review_needs_changes',
                        'review_approved',
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
            "review_status": "ALTER TABLE onboarding_leads ADD COLUMN review_status TEXT",
            "review_notes": "ALTER TABLE onboarding_leads ADD COLUMN review_notes TEXT",
            "reviewed_at": "ALTER TABLE onboarding_leads ADD COLUMN reviewed_at TEXT",
        }
        for column, statement in migrations.items():
            if column not in existing:
                conn.execute(statement)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS onboarding_intake_answers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                question_key TEXT NOT NULL,
                answer TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (lead_id, question_key),
                FOREIGN KEY (lead_id) REFERENCES onboarding_leads(id)
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


def get_intake_progress(token: str) -> Optional[IntakeProgress]:
    lead = find_lead_by_token(token)
    if lead is None:
        return None
    answers = list_intake_answers(lead.id)
    current_index = 0
    for index, question in enumerate(INTAKE_QUESTIONS):
        if question.key not in answers:
            current_index = index
            break
    else:
        current_index = len(INTAKE_QUESTIONS)
    return IntakeProgress(
        lead=lead,
        answers=answers,
        current_index=current_index,
        complete=current_index >= len(INTAKE_QUESTIONS),
    )


def list_intake_answers(lead_id: int) -> dict[str, IntakeAnswer]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT question_key, answer, created_at, updated_at
            FROM onboarding_intake_answers
            WHERE lead_id = ?
            ORDER BY id ASC
            """,
            (lead_id,),
        ).fetchall()
    return {str(row["question_key"]): row_to_answer(row) for row in rows}


def list_intake_answer_counts() -> dict[int, int]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT lead_id, COUNT(*) AS answer_count
            FROM onboarding_intake_answers
            GROUP BY lead_id
            """
        ).fetchall()
    return {int(row["lead_id"]): int(row["answer_count"]) for row in rows}


def build_setup_summary(lead_id: int) -> str:
    lead = get_lead(lead_id)
    answers = list_intake_answers(lead_id)
    lines = [
        "Unboks onboarding setup summary",
        "",
        f"Lead ID: {lead.id}",
        f"Email: {lead.email}",
        f"Business name: {lead.business_name or 'Unknown'}",
        f"Contact name: {lead.contact_name or 'Unknown'}",
        f"Language: {lead.language or 'Unknown'}",
        f"Status: {lead.status}",
        f"Review status: {lead.review_status or 'Not reviewed'}",
        f"Created: {lead.created_at}",
        f"Updated: {lead.updated_at}",
    ]
    if lead.notes:
        lines.extend(["", "Internal notes:", lead.notes])
    if lead.review_notes:
        lines.extend(["", "Review notes:", lead.review_notes])
    lines.append("")
    lines.append("Intake answers:")
    for question in INTAKE_QUESTIONS:
        answer = answers.get(question.key)
        lines.append("")
        lines.append(question.label)
        lines.append(answer.answer if answer else "Not answered.")
    return "\n".join(lines) + "\n"


def set_review_decision(lead_id: int, decision: str, notes: str) -> OnboardingLead:
    if decision not in REVIEW_DECISIONS:
        raise LeadValidationError("Invalid review decision.")
    clean_notes = notes.strip() or None
    now = utc_now()
    status = REVIEW_DECISIONS[decision]
    with _connect() as conn:
        conn.execute(
            """
            UPDATE onboarding_leads
            SET status = ?,
                review_status = ?,
                review_notes = ?,
                reviewed_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, decision, clean_notes, now, now, lead_id),
        )
        row = conn.execute(
            "SELECT * FROM onboarding_leads WHERE id = ?",
            (lead_id,),
        ).fetchone()
    if row is None:
        raise LeadNotFoundError("Onboarding lead not found.")
    return row_to_lead(row)


def save_intake_answer(token: str, question_key: str, answer: str) -> IntakeProgress:
    progress = get_intake_progress(token)
    if progress is None:
        raise LeadNotFoundError("Onboarding link is invalid or expired.")
    if question_key not in INTAKE_QUESTION_KEYS:
        raise LeadValidationError("Invalid onboarding question.")
    current_question = progress.current_question
    if current_question is None:
        return progress
    if question_key != current_question.key:
        raise LeadValidationError("Please answer the current onboarding question.")
    cleaned_answer = answer.strip()
    if current_question.required and not cleaned_answer:
        raise LeadValidationError("Answer is required.")

    now = utc_now()
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO onboarding_intake_answers (
                lead_id,
                question_key,
                answer,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (lead_id, question_key)
            DO UPDATE SET answer = excluded.answer,
                          updated_at = excluded.updated_at
            """,
            (progress.lead.id, question_key, cleaned_answer, now, now),
        )
        answers = conn.execute(
            """
            SELECT COUNT(*) AS answer_count
            FROM onboarding_intake_answers
            WHERE lead_id = ?
            """,
            (progress.lead.id,),
        ).fetchone()
        answer_count = int(answers["answer_count"])
        status = "form_submitted" if answer_count >= len(INTAKE_QUESTIONS) else "form_started"
        conn.execute(
            """
            UPDATE onboarding_leads
            SET status = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now, progress.lead.id),
        )
    updated_progress = get_intake_progress(token)
    if updated_progress is None:
        raise LeadNotFoundError("Onboarding link is invalid or expired.")
    return updated_progress


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
        review_status=row["review_status"],
        review_notes=row["review_notes"],
        reviewed_at=row["reviewed_at"],
    )


def row_to_answer(row: sqlite3.Row) -> IntakeAnswer:
    return IntakeAnswer(
        question_key=str(row["question_key"]),
        answer=str(row["answer"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )
