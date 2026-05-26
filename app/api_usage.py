"""Read-only API usage / provider health view over tenant runtime databases."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from app.tenants import get_tenant_client_data, list_tenants, validate_slug


@dataclass(frozen=True)
class UsageWindow:
    calls: int = 0
    tokens: int = 0
    cost: float = 0.0


@dataclass(frozen=True)
class TenantApiHealth:
    tenant_id: str
    tenant_name: str
    today: UsageWindow
    seven_days: UsageWindow
    thirty_days: UsageWindow
    error_count: int
    fallback_count: int
    last_error: str
    last_success: str
    status: str
    tracked: bool
    warnings: tuple[str, ...] = ()


def _tenant_root() -> Path:
    return Path(os.getenv("NR3_TENANTS_CLIENT_DIR", "/root/clients").strip() or "/root/clients")


def _tenant_db_path(tenant_id: str) -> Path:
    return _tenant_root() / validate_slug(tenant_id) / "data" / "state_registry.db"


def _has_usage_table(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'api_usage_events'"
    ).fetchone()
    return bool(row)


def _window(conn: sqlite3.Connection, since: str) -> UsageWindow:
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(total_tokens),0), "
        "COALESCE(SUM(estimated_cost),0) FROM api_usage_events WHERE timestamp >= ?",
        (since,),
    ).fetchone()
    return UsageWindow(
        calls=int(row[0] or 0),
        tokens=int(row[1] or 0),
        cost=float(row[2] or 0),
    )


def _config_warnings(data: dict[str, Any]) -> tuple[str, ...]:
    business = data.get("business") if isinstance(data.get("business"), dict) else data
    warnings: list[str] = []
    if not business.get("primary_language") and not business.get("languages") and not data.get("languages"):
        warnings.append("Missing language config")
    safety_present = any(
        data.get(key) or business.get(key)
        for key in ("safety", "compliance", "clinical_guardrails", "safety_restrictions")
    )
    if safety_present:
        warnings.append("Safety notes present; verify prompt injection")
    for key in ("agent_name", "whatsapp", "website"):
        if not business.get(key) and not data.get(key):
            warnings.append(f"Missing {key}")
    return tuple(warnings)


def tenant_api_health(tenant_id: str) -> TenantApiHealth:
    tenant = next((item for item in list_tenants() if item.id == tenant_id), None)
    tenant_name = tenant.name if tenant else tenant_id
    warnings = _config_warnings(get_tenant_client_data(tenant_id))
    db_path = _tenant_db_path(tenant_id)
    if not db_path.exists():
        return TenantApiHealth(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            today=UsageWindow(),
            seven_days=UsageWindow(),
            thirty_days=UsageWindow(),
            error_count=0,
            fallback_count=0,
            last_error="Not tracked yet",
            last_success="Not tracked yet",
            status="warning" if warnings else "unknown",
            tracked=False,
            warnings=warnings,
        )
    conn = sqlite3.connect(db_path)
    try:
        if not _has_usage_table(conn):
            return TenantApiHealth(
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                today=UsageWindow(),
                seven_days=UsageWindow(),
                thirty_days=UsageWindow(),
                error_count=0,
                fallback_count=0,
                last_error="Not tracked yet",
                last_success="Not tracked yet",
                status="warning" if warnings else "unknown",
                tracked=False,
                warnings=warnings,
            )
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        seven = (now - timedelta(days=7)).isoformat()
        thirty = (now - timedelta(days=30)).isoformat()
        counts = conn.execute(
            "SELECT SUM(CASE WHEN success=0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN fallback_used=1 THEN 1 ELSE 0 END) "
            "FROM api_usage_events WHERE timestamp >= ?",
            (thirty,),
        ).fetchone()
        last_error = conn.execute(
            "SELECT timestamp, error_category FROM api_usage_events "
            "WHERE success=0 ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        last_success = conn.execute(
            "SELECT timestamp FROM api_usage_events "
            "WHERE success=1 ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        errors = int(counts[0] or 0)
        fallbacks = int(counts[1] or 0)
        status = "healthy"
        if warnings or errors or fallbacks:
            status = "warning"
        if errors + fallbacks >= 3:
            status = "critical"
        return TenantApiHealth(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            today=_window(conn, today_start),
            seven_days=_window(conn, seven),
            thirty_days=_window(conn, thirty),
            error_count=errors,
            fallback_count=fallbacks,
            last_error=(
                f"{last_error[0]} {last_error[1]}".strip()
                if last_error else "None"
            ),
            last_success=last_success[0] if last_success else "Not tracked yet",
            status=status,
            tracked=True,
            warnings=warnings,
        )
    finally:
        conn.close()


def platform_api_health() -> dict[str, Any]:
    tenants = [tenant_api_health(tenant.id) for tenant in list_tenants()]
    totals = {
        "today_calls": sum(t.today.calls for t in tenants),
        "seven_day_calls": sum(t.seven_days.calls for t in tenants),
        "thirty_day_calls": sum(t.thirty_days.calls for t in tenants),
        "today_tokens": sum(t.today.tokens for t in tenants),
        "seven_day_tokens": sum(t.seven_days.tokens for t in tenants),
        "thirty_day_tokens": sum(t.thirty_days.tokens for t in tenants),
        "estimated_cost": round(sum(t.thirty_days.cost for t in tenants), 4),
        "api_errors": sum(t.error_count for t in tenants),
        "fallbacks": sum(t.fallback_count for t in tenants),
    }
    if any(t.status == "critical" for t in tenants):
        provider_health = "critical"
    elif any(t.status == "warning" for t in tenants):
        provider_health = "warning"
    else:
        provider_health = "healthy"
    last_errors = [t.last_error for t in tenants if t.last_error not in ("None", "Not tracked yet")]
    return {
        "totals": totals,
        "provider_health": provider_health,
        "last_provider_error": last_errors[0] if last_errors else "None",
        "projected_monthly_spend": round(totals["estimated_cost"], 4),
        "tenants": tenants,
    }
