"""Minimal persistent audit log for internal control actions."""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.channel_connections import utc_now


@dataclass(frozen=True)
class AuditEvent:
    id: str
    actor: str
    tenant_id: str | None
    action: str
    result: str
    safe_summary: str | None
    metadata_json: str
    created_at: str


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
            CREATE TABLE IF NOT EXISTS audit_events (
                id TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                tenant_id TEXT,
                action TEXT NOT NULL,
                result TEXT NOT NULL,
                safe_summary TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
            ON audit_events (created_at)
            """
        )


def record_event(
    *,
    action: str,
    tenant_id: str | None = None,
    result: str = "ok",
    safe_summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    actor: str = "internal_admin",
) -> AuditEvent:
    init_db()
    now = utc_now()
    event_id = f"aud_{secrets.token_urlsafe(18)}"
    metadata_json = json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False)
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO audit_events (
                id,
                actor,
                tenant_id,
                action,
                result,
                safe_summary,
                metadata_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                actor,
                tenant_id,
                action,
                result,
                safe_summary,
                metadata_json,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM audit_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    return row_to_audit_event(row)


def list_events(limit: int = 50) -> list[AuditEvent]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM audit_events
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [row_to_audit_event(row) for row in rows]


def row_to_audit_event(row: sqlite3.Row) -> AuditEvent:
    return AuditEvent(
        id=str(row["id"]),
        actor=str(row["actor"]),
        tenant_id=row["tenant_id"],
        action=str(row["action"]),
        result=str(row["result"]),
        safe_summary=row["safe_summary"],
        metadata_json=str(row["metadata_json"]),
        created_at=str(row["created_at"]),
    )
