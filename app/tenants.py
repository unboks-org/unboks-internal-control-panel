"""Placeholder tenant registry for the ICP command center.

The internal control panel is tenant-first. Real persistence will land in a
later milestone; for now we expose a small, hard-coded list so the UI can be
wired end-to-end without faking storage.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class TenantHealth:
    inbox: str = "unknown"        # ok | warn | down | unknown
    ai_agent: str = "unknown"
    channels: str = "unknown"
    sot: str = "unknown"
    escalations: str = "unknown"
    billing: str = "unknown"      # ok | trial | overdue | unknown


@dataclass(frozen=True)
class TenantSourceOfTruth:
    summary: str = "Not configured"
    last_edited: str = "—"
    status: str = "unknown"          # ok | warn | down | unknown
    files_count: int = 0
    cloud_provider: Optional[str] = None     # google_drive | dropbox | onedrive | None
    cloud_status: str = "disconnected"        # connected | disconnected | error | unknown
    last_sync: str = "—"
    pending_review: int = 0


CLOUD_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("google_drive", "Google Drive"),
    ("dropbox", "Dropbox"),
    ("onedrive", "OneDrive"),
)


UPLOAD_CATEGORIES: tuple[str, ...] = (
    "Documents / PDFs",
    "Images",
    "Price lists",
    "Menus / brochures",
    "FAQ files",
    "Policies",
    "Services / product sheets",
)


@dataclass(frozen=True)
class TenantAgent:
    model: str = "—"
    tone: str = "—"
    handoff: str = "—"
    replies_enabled: bool = False
    auto_reply_enabled: bool = False
    escalation_mode: str = "both"   # soft | hard | both
    human_takeover_active: bool = False
    learning_enabled: bool = False
    tone_summary: str = "Not configured"
    escalation_rules_summary: str = "Not configured"
    recent_replies: tuple[str, ...] = field(default_factory=tuple)


ESCALATION_MODES: tuple[tuple[str, str], ...] = (
    ("soft", "Soft escalation allowed"),
    ("hard", "Hard escalation allowed"),
    ("both", "Both allowed"),
)


@dataclass(frozen=True)
class TenantChannel:
    name: str
    state: str  # connected | disconnected | unknown


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    status: str  # active | paused | suspended
    plan: str   # demo | trial | paid
    health: TenantHealth = field(default_factory=TenantHealth)
    sot: TenantSourceOfTruth = field(default_factory=TenantSourceOfTruth)
    agent: TenantAgent = field(default_factory=TenantAgent)
    channels: tuple[TenantChannel, ...] = field(default_factory=tuple)
    activity: tuple[str, ...] = field(default_factory=tuple)


_TENANTS: tuple[Tenant, ...] = (
    Tenant(
        id="unboks-demo",
        name="Unboks Demo",
        status="active",
        plan="demo",
        health=TenantHealth(
            inbox="ok",
            ai_agent="ok",
            channels="ok",
            sot="ok",
            escalations="ok",
            billing="ok",
        ),
        sot=TenantSourceOfTruth(
            summary="Demo SoT seeded with 12 entries.",
            last_edited="—",
            status="ok",
            files_count=12,
            cloud_provider="google_drive",
            cloud_status="connected",
            last_sync="—",
            pending_review=0,
        ),
        agent=TenantAgent(
            model="gpt-4o-mini",
            tone="friendly",
            handoff="manual",
            replies_enabled=True,
            auto_reply_enabled=True,
            escalation_mode="both",
            human_takeover_active=False,
            learning_enabled=True,
            tone_summary="Friendly, concise, neutral. Mirrors customer language.",
            escalation_rules_summary=(
                "Soft: ask operator when unsure about price, stock, or policy. "
                "Hard: hand over on refund, complaint, or explicit human request."
            ),
            recent_replies=(),
        ),
        channels=(
            TenantChannel("Web widget", "connected"),
            TenantChannel("WhatsApp", "disconnected"),
            TenantChannel("Email", "disconnected"),
        ),
        activity=(),
    ),
    Tenant(
        id="consulta-despertares",
        name="Consulta Despertares",
        status="active",
        plan="trial",
        channels=(
            TenantChannel("Web widget", "unknown"),
            TenantChannel("WhatsApp", "unknown"),
            TenantChannel("Email", "unknown"),
        ),
    ),
    Tenant(
        id="bluefinn-charters",
        name="BlueFinn Charters",
        status="active",
        plan="trial",
        channels=(
            TenantChannel("Web widget", "unknown"),
            TenantChannel("WhatsApp", "unknown"),
            TenantChannel("Email", "unknown"),
        ),
    ),
)


def list_tenants() -> tuple[Tenant, ...]:
    return _TENANTS


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    for tenant in _TENANTS:
        if tenant.id == tenant_id:
            return tenant
    return None
