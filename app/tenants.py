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
    state: str            # connected | disconnected | error | unknown
    last_message: str = "—"
    last_sync: str = "—"


CHANNEL_CATALOG: tuple[str, ...] = (
    "WhatsApp",
    "Email",
    "Instagram",
    "Facebook",
    "Telegram",
    "Website chat",
)


@dataclass(frozen=True)
class TenantActivityEntry:
    type: str       # one of ACTIVITY_TYPES values
    summary: str
    actor: str
    when: str       # human-readable timestamp


ACTIVITY_TYPES: tuple[tuple[str, str], ...] = (
    ("tenant_created", "Tenant created"),
    ("sot_updated", "SOT updated"),
    ("agent_setting_changed", "Agent setting changed"),
    ("channel_connected", "Channel connected"),
    ("onboarding_sent", "Onboarding sent"),
    ("review_approved", "Review approved"),
    ("tenant_paused", "Tenant paused"),
    ("changes_pushed", "Changes pushed"),
)


_ACTIVITY_TYPE_LABELS = dict(ACTIVITY_TYPES)


def activity_type_label(value: str) -> str:
    return _ACTIVITY_TYPE_LABELS.get(value, value)


@dataclass(frozen=True)
class TenantOnboarding:
    status: str = "not_started"           # not_started | sent | started | submitted | reviewed | ready
    intake_link_status: str = "not_generated"  # not_generated | active | expired
    intake_submitted_at: str = "—"
    review_status: str = "—"
    missing_items_count: int = 0
    next_action: str = "Send onboarding link"


@dataclass(frozen=True)
class TenantEscalations:
    open_count: int = 0
    soft_count: int = 0
    hard_count: int = 0
    avg_response_time: str = "—"
    rules_summary: str = "No rules configured."
    alert_whatsapp: bool = False
    alert_email: bool = False
    alert_telegram: bool = False
    operator_on_duty: str = "—"


@dataclass(frozen=True)
class TenantOperator:
    name: str
    email: str
    role: str             # owner | admin | operator
    invite_state: str     # active | needs_invite | disabled
    alert_recipient: bool = False
    last_login: str = "—"


@dataclass(frozen=True)
class TenantAccess:
    status: str = "active"   # active | needs_invite | disabled
    operators: tuple[TenantOperator, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class TenantPushState:
    status: str = "clean"            # clean | pending | failed
    pending_count: int = 0
    last_pushed_at: str = "—"
    last_pushed_by: str = "—"
    target: str = "Tenant dashboard"


@dataclass(frozen=True)
class TenantBilling:
    status: str = "trial"            # trial | active | overdue | paused | cancelled
    trial_days_left: Optional[int] = None
    plan: str = "—"
    monthly_price: str = "—"
    next_billing_date: str = "—"
    payment_status: str = "—"        # ok | pending | failed | —


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
    billing: TenantBilling = field(default_factory=TenantBilling)
    push: TenantPushState = field(default_factory=TenantPushState)
    access: TenantAccess = field(default_factory=TenantAccess)
    escalations: TenantEscalations = field(default_factory=TenantEscalations)
    onboarding: TenantOnboarding = field(default_factory=TenantOnboarding)
    activity: tuple[TenantActivityEntry, ...] = field(default_factory=tuple)


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
            TenantChannel("WhatsApp", "disconnected"),
            TenantChannel("Email", "disconnected"),
            TenantChannel("Instagram", "disconnected"),
            TenantChannel("Facebook", "disconnected"),
            TenantChannel("Telegram", "disconnected"),
            TenantChannel("Website chat", "connected"),
        ),
        billing=TenantBilling(
            status="active",
            trial_days_left=None,
            plan="Demo",
            monthly_price="—",
            next_billing_date="—",
            payment_status="—",
        ),
        push=TenantPushState(
            status="clean",
            pending_count=0,
            last_pushed_at="—",
            last_pushed_by="—",
        ),
        escalations=TenantEscalations(
            open_count=0,
            soft_count=0,
            hard_count=0,
            avg_response_time="—",
            rules_summary=(
                "Soft escalation on refund or complaint intent. "
                "Hard escalation on explicit human request or repeated unresolved replies."
            ),
            alert_whatsapp=False,
            alert_email=True,
            alert_telegram=False,
            operator_on_duty="—",
        ),
        onboarding=TenantOnboarding(
            status="ready",
            intake_link_status="not_generated",
            intake_submitted_at="—",
            review_status="—",
            missing_items_count=0,
            next_action="No action required.",
        ),
        access=TenantAccess(
            status="active",
            operators=(
                TenantOperator(
                    name="Demo Owner",
                    email="owner@unboks.demo",
                    role="owner",
                    invite_state="active",
                    alert_recipient=True,
                    last_login="—",
                ),
                TenantOperator(
                    name="Demo Admin",
                    email="admin@unboks.demo",
                    role="admin",
                    invite_state="active",
                    alert_recipient=True,
                    last_login="—",
                ),
                TenantOperator(
                    name="Demo Operator",
                    email="ops@unboks.demo",
                    role="operator",
                    invite_state="active",
                    alert_recipient=False,
                    last_login="—",
                ),
            ),
        ),
        activity=(),
    ),
    Tenant(
        id="consulta-despertares",
        name="Consulta Despertares",
        status="active",
        plan="trial",
        channels=tuple(TenantChannel(name, "disconnected") for name in CHANNEL_CATALOG),
        billing=TenantBilling(
            status="trial",
            trial_days_left=12,
            plan="Trial",
            monthly_price="—",
            next_billing_date="—",
            payment_status="—",
        ),
        access=TenantAccess(status="needs_invite", operators=()),
    ),
    Tenant(
        id="bluefinn-charters",
        name="BlueFinn Charters",
        status="active",
        plan="trial",
        channels=tuple(TenantChannel(name, "disconnected") for name in CHANNEL_CATALOG),
        billing=TenantBilling(
            status="trial",
            trial_days_left=5,
            plan="Trial",
            monthly_price="—",
            next_billing_date="—",
            payment_status="—",
        ),
        access=TenantAccess(status="needs_invite", operators=()),
    ),
)


def list_tenants() -> tuple[Tenant, ...]:
    return _TENANTS


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    for tenant in _TENANTS:
        if tenant.id == tenant_id:
            return tenant
    return None


# Anomaly monitor (UI-only, no real detection backend yet).
ANOMALY_SIGNALS: tuple[tuple[str, str], ...] = (
    ("message_volume_spike", "Message volume spike"),
    ("escalation_spike", "Escalation spike"),
    ("agent_reply_failure", "Agent reply failure"),
    ("channel_disconnected", "Channel disconnected"),
    ("repeated_complaint", "Repeated customer complaint"),
    ("sot_missing_or_stale", "SOT missing / stale"),
    ("long_unanswered", "Long unanswered conversation"),
    ("unusual_order_pattern", "Unusual booking/order pattern"),
    ("error_rate_spike", "Error rate spike"),
)

ANOMALY_STATUSES: tuple[tuple[str, str], ...] = (
    ("new", "New"),
    ("investigating", "Investigating"),
    ("resolved", "Resolved"),
)


@dataclass(frozen=True)
class AnomalyFlag:
    tenant_id: str
    tenant_name: str
    signal: str           # key from ANOMALY_SIGNALS
    signal_label: str
    severity: str         # P0 | P1 | P2
    first_detected: str   # display string, e.g. "—"
    status: str           # new | investigating | resolved


def list_anomalies() -> tuple[AnomalyFlag, ...]:
    # No real detection backend yet — return empty to render the placeholder state.
    return ()
