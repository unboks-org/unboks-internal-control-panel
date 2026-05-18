"""Placeholder tenant registry for the ICP command center.

The internal control panel is tenant-first. Real persistence will land in a
later milestone; for now we expose a small, hard-coded list so the UI can be
wired end-to-end without faking storage.
"""

import json
import os
import re
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
    "Messenger",
    "Telegram",
    "Tiktok",
    "X",
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


NOTE_PRIORITIES: tuple[tuple[str, str], ...] = (
    ("normal", "Normal"),
    ("important", "Important"),
    ("critical", "Critical"),
)


@dataclass(frozen=True)
class TenantNote:
    id: str
    body: str
    author: str = "—"           # placeholder
    created_at: str = "—"       # placeholder
    priority: str = "normal"    # normal | important | critical
    pinned: bool = False
    follow_up_date: Optional[str] = None  # placeholder
    follow_up_done: bool = False


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
    notes: tuple[TenantNote, ...] = field(default_factory=tuple)


_TENANTS: tuple[Tenant, ...] = (
    Tenant(
        id="unboks",
        name="Unboks",
        status="active",
        plan="demo",
        notes=(
            TenantNote(
                id="note-demo-1",
                body="Reference tenant — keep in sync with Nr2 demo content.",
                author="—",
                created_at="—",
                priority="normal",
                pinned=True,
            ),
            TenantNote(
                id="note-demo-2",
                body="Schedule quarterly review of demo SoT entries.",
                author="—",
                created_at="—",
                priority="normal",
                pinned=False,
                follow_up_date="—",
            ),
        ),
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
            TenantChannel("Messenger", "disconnected"),
            TenantChannel("Telegram", "disconnected"),
            TenantChannel("Tiktok", "disconnected"),
            TenantChannel("X", "disconnected"),
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
)


SETUP_CHECKLIST_STATUSES: tuple[str, ...] = ("done", "needs_review", "missing")
_SETUP_STATUS_LABELS: dict[str, str] = {
    "done": "Done",
    "needs_review": "Needs review",
    "missing": "Missing",
}


def compute_setup_checklist(tenant: "Tenant") -> dict:
    """Derive setup checklist from real tenant fields. Never invents 'done'."""
    items: list[dict] = []

    # 1. Tenant profile completed
    profile_done = bool(tenant.id and tenant.name and tenant.status and tenant.plan)
    items.append({
        "key": "profile",
        "label": "Tenant profile completed",
        "status": "done" if profile_done else "needs_review",
        "anchor": "tenant-header-anchor",
    })

    # 2. Onboarding form completed
    ob_status = tenant.onboarding.status
    if ob_status in ("submitted", "reviewed", "ready"):
        ob_state = "done"
    elif ob_status in ("started",):
        ob_state = "needs_review"
    else:
        ob_state = "missing"
    items.append({
        "key": "onboarding",
        "label": "Onboarding form completed",
        "status": ob_state,
        "anchor": "onboarding-section",
    })

    # 3. Source of Truth uploaded
    sot_state = "done" if tenant.sot.files_count > 0 else "missing"
    items.append({
        "key": "sot",
        "label": "Source of Truth uploaded",
        "status": sot_state,
        "anchor": "sot-section",
    })

    # 4. Channels connected
    has_connected = any(ch.state == "connected" for ch in tenant.channels)
    items.append({
        "key": "channels",
        "label": "Channels connected",
        "status": "done" if has_connected else "missing",
        "anchor": "channels-section",
    })

    # 5. AI Agent configured
    tone_set = tenant.agent.tone_summary != "Not configured"
    rules_set = tenant.agent.escalation_rules_summary != "Not configured"
    if tone_set and rules_set:
        agent_state = "done"
    elif tone_set or rules_set:
        agent_state = "needs_review"
    else:
        agent_state = "missing"
    items.append({
        "key": "agent",
        "label": "AI Agent configured",
        "status": agent_state,
        "anchor": "agent-section",
    })

    # 6. Escalation rules configured (derived from the Escalations subsystem)
    esc_rules = (tenant.escalations.rules_summary or "").strip()
    esc_rules_set = esc_rules not in ("", "No rules configured.", "Not configured")
    items.append({
        "key": "escalations",
        "label": "Escalation rules configured",
        "status": "done" if esc_rules_set else "missing",
        "anchor": "escalations-section",
    })

    # 7. Operators invited
    if tenant.access.operators:
        op_state = "done"
    elif tenant.access.status == "needs_invite":
        op_state = "missing"
    else:
        op_state = "needs_review"
    items.append({
        "key": "operators",
        "label": "Operators invited",
        "status": op_state,
        "anchor": "access-section",
    })

    # 8. Dashboard ready
    items.append({
        "key": "dashboard",
        "label": "Dashboard ready",
        "status": "done" if ob_status == "ready" else "missing",
        "anchor": "onboarding-section",
    })

    # 9. Trial / payment configured
    bs = tenant.billing.status
    ps = tenant.billing.payment_status
    if bs == "active" and ps == "ok":
        bill_state = "done"
    elif bs == "trial":
        bill_state = "needs_review"
    elif bs in ("overdue", "cancelled", "paused"):
        bill_state = "needs_review"
    else:
        bill_state = "missing"
    items.append({
        "key": "billing",
        "label": "Trial / payment configured",
        "status": bill_state,
        "anchor": "billing-section",
    })

    for item in items:
        item["status_label"] = _SETUP_STATUS_LABELS[item["status"]]

    total = len(items)
    done = sum(1 for i in items if i["status"] == "done")
    percent = int(round(100 * done / total)) if total else 0

    next_required = next(
        (i for i in items if i["status"] in ("missing", "needs_review")),
        None,
    )

    return {
        "items": items,
        "percent": percent,
        "done_count": done,
        "total": total,
        "next_action": next_required,
    }


def sorted_notes(notes: tuple[TenantNote, ...]) -> tuple[TenantNote, ...]:
    """Pinned first, then in original order (newest-first is the caller's job)."""
    return tuple(sorted(notes, key=lambda n: (0 if n.pinned else 1,)))


_DEFAULT_TENANTS_CLIENT_DIR = "/opt/wtyj/clients"
_ALLOWED_STATUSES = ("active", "trial", "paused", "suspended")


def _load_tenants_from_disk(client_dir: str) -> tuple[Tenant, ...]:
    """J3-BE-01: discover tenants by globbing {client_dir}/*/config/client.json.

    Mapping (only id/name/status/plan are pulled from disk for now; all other
    Tenant fields keep their placeholder defaults until subsequent J3 briefs
    wire them up):
      tenant.id     = business.slug, fallback to the parent directory name
      tenant.name   = business.name, fallback to tenant.id
      tenant.status = business.status if in {active, paused, suspended}, else 'active'
      tenant.plan   = business.plan, fallback to 'trial'

    Read-only: never writes to client.json. Invalid JSON, missing files, or
    files without a usable id are skipped without raising. Returns alphabetically
    sorted by tenant.id."""
    import glob
    import json

    pattern = os.path.join(client_dir, "*", "config", "client.json")
    discovered: list[Tenant] = []
    for path in glob.glob(pattern):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        business = data.get("business")
        if not isinstance(business, dict):
            business = {}
        # Parent directory name (e.g. "unboks" from /opt/wtyj/clients/unboks/config/client.json)
        directory_name = os.path.basename(os.path.dirname(os.path.dirname(path))).strip()
        slug = business.get("slug")
        if isinstance(slug, str):
            slug = slug.strip()
        else:
            slug = ""
        tenant_id = slug or directory_name
        if not tenant_id:
            continue
        raw_name = business.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            name = raw_name.strip()
        else:
            name = tenant_id
        raw_status = business.get("status")
        if isinstance(raw_status, str):
            normalized = raw_status.strip().lower()
            status = normalized if normalized in _ALLOWED_STATUSES else "active"
        else:
            status = "active"
        raw_plan = business.get("plan")
        if isinstance(raw_plan, str) and raw_plan.strip():
            plan = raw_plan.strip()
        else:
            plan = "trial"
        discovered.append(Tenant(
            id=tenant_id,
            name=name,
            status=status,
            plan=plan,
        ))
    discovered.sort(key=lambda t: t.id)
    return tuple(discovered)


def list_tenants() -> tuple[Tenant, ...]:
    """Return real tenants from disk when NR3_TENANTS_CLIENT_DIR is set AND
    the directory contains at least one parseable client.json; otherwise
    fall back to the hard-coded placeholder list."""
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    if client_dir and os.path.isdir(client_dir):
        loaded = _load_tenants_from_disk(client_dir)
        if loaded:
            return loaded
    return _TENANTS


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    for tenant in list_tenants():
        if tenant.id == tenant_id:
            return tenant
    return None


# Tenant creation (used by the Add-New-Tenant wizard).
#
# Pure filesystem operation — writes <client_dir>/<slug>/config/client.json
# and an empty <slug>/data/ dir. Tenant discovery via list_tenants() picks
# the new directory up immediately on the next request.

_SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,49}$")


class TenantCreateError(Exception):
    """Raised when create_tenant_directory cannot create a tenant (bad
    slug, slug already exists, client_dir not configured, etc.)."""


def validate_slug(slug: str) -> str:
    s = (slug or "").strip().lower()
    if not _SLUG_PATTERN.match(s):
        raise TenantCreateError(
            "Slug must be 2-50 chars, lowercase letters / digits / - / _, "
            "starting with a letter.")
    return s


def derive_slug_from_name(name: str) -> str:
    """Lowercase, replace runs of non-alphanumerics with '-', strip
    leading non-letters. Returns a candidate slug that may still fail
    validate_slug — callers should validate."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    s = re.sub(r"^[^a-z]+", "", s)
    return s[:50]


def get_tenants_client_dir() -> str:
    """Resolved tenants root from env, with the same fallback semantics
    list_tenants() uses. Empty string if the configured directory does
    not exist."""
    client_dir = os.getenv(
        "NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    return client_dir if client_dir and os.path.isdir(client_dir) else ""


def create_tenant_directory(slug: str, business: dict,
                             client_dir: Optional[str] = None) -> str:
    """Create <client_dir>/<slug>/{config/client.json, data/}. Returns
    the absolute tenant root path. Raises TenantCreateError on slug
    validation failure, missing client_dir, or pre-existing directory."""
    safe_slug = validate_slug(slug)
    root = client_dir or os.getenv(
        "NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    if not root:
        raise TenantCreateError(
            "NR3_TENANTS_CLIENT_DIR is not set — cannot create tenant.")
    if not os.path.isdir(root):
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            raise TenantCreateError(
                f"Could not create tenants root {root!r}: {exc}") from exc
    tenant_root = os.path.join(root, safe_slug)
    if os.path.exists(tenant_root):
        raise TenantCreateError(
            f"Tenant {safe_slug!r} already exists at {tenant_root!r}.")
    os.makedirs(os.path.join(tenant_root, "config"))
    os.makedirs(os.path.join(tenant_root, "data"))
    payload = {"business": dict(business)}
    payload["business"]["slug"] = safe_slug
    config_path = os.path.join(tenant_root, "config", "client.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return tenant_root
