"""Placeholder tenant registry for the ICP command center.

The internal control panel is tenant-first. Real persistence will land in a
later milestone; for now we expose a small, hard-coded list so the UI can be
wired end-to-end without faking storage.
"""

import json
import os
import re
import tempfile
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


_DEFAULT_TENANTS_CLIENT_DIR = "/root/clients"
_DEFAULT_TENANT_REGISTRY_PATH = "data/tenant_registry.json"
_ALLOWED_STATUSES = ("active", "trial", "paused", "suspended")


def _tenant_from_source(source: dict, fallback_id: str) -> Optional[Tenant]:
    slug = source.get("slug")
    if isinstance(slug, str):
        slug = slug.strip()
    else:
        slug = ""
    tenant_id = slug or fallback_id
    if not tenant_id:
        return None
    raw_name = source.get("name")
    if isinstance(raw_name, str) and raw_name.strip():
        name = raw_name.strip()
    else:
        name = tenant_id
    raw_status = source.get("status")
    if isinstance(raw_status, str):
        normalized = raw_status.strip().lower()
        status = normalized if normalized in _ALLOWED_STATUSES else "active"
    else:
        status = "active"
    raw_plan = source.get("plan")
    if isinstance(raw_plan, str) and raw_plan.strip():
        plan = raw_plan.strip()
    else:
        plan = "trial"
    return Tenant(
        id=tenant_id,
        name=name,
        status=status,
        plan=plan,
    )


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
        # Two supported client.json shapes:
        #   wrapped (legacy): {"business": {"slug": ..., "name": ..., ...}}
        #   flat (J3-BE-50):  {"slug": ..., "name": ..., ...}
        # Read from the wrapped business dict if present and non-empty,
        # otherwise fall through to the top-level data dict.
        business = data.get("business")
        if isinstance(business, dict) and business:
            source = business
        else:
            source = data
        directory_name = os.path.basename(os.path.dirname(os.path.dirname(path))).strip()
        tenant = _tenant_from_source(source, directory_name)
        if tenant is not None:
            discovered.append(tenant)
    discovered.sort(key=lambda t: t.id)
    return tuple(discovered)


def _registry_path() -> str:
    return os.getenv(
        "NR3_TENANT_REGISTRY_PATH",
        _DEFAULT_TENANT_REGISTRY_PATH,
    ).strip()


def _load_tenants_from_registry() -> tuple[Tenant, ...]:
    """Load tenants created through ICP even when the VPS client root is
    not mounted into this Nr3 process."""
    path = _registry_path()
    if not path:
        return tuple()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return tuple()
    tenants_raw = data.get("tenants") if isinstance(data, dict) else {}
    if not isinstance(tenants_raw, dict):
        return tuple()
    loaded: list[Tenant] = []
    for fallback_id, source in tenants_raw.items():
        if not isinstance(fallback_id, str) or not isinstance(source, dict):
            continue
        tenant = _tenant_from_source(source, fallback_id)
        if tenant is not None:
            loaded.append(tenant)
    loaded.sort(key=lambda t: t.id)
    return tuple(loaded)


def _save_registry(data: dict) -> None:
    path = _registry_path()
    if not path:
        return
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=".tenant_registry.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register_tenant(client_data: dict) -> None:
    """Persist a lightweight tenant registry row for the ICP sidebar.

    This is separate from the VPS runtime client.json. It lets Nr3 show
    tenants created in ICP even when `/root/clients` lives on another
    machine and is not mounted into the control panel.
    """
    if not isinstance(client_data, dict):
        return
    slug = client_data.get("slug")
    if isinstance(slug, str):
        client_data = dict(client_data)
        client_data["slug"] = validate_slug(slug)
    tenant = _tenant_from_source(client_data, "")
    if tenant is None:
        return
    path = _registry_path()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        data = {"tenants": {}}
    if not isinstance(data, dict):
        data = {"tenants": {}}
    tenants = data.setdefault("tenants", {})
    if not isinstance(tenants, dict):
        tenants = {}
        data["tenants"] = tenants
    tenants[tenant.id] = {
        "slug": tenant.id,
        "name": tenant.name,
        "status": tenant.status,
        "plan": tenant.plan,
    }
    _save_registry(data)


def list_tenants() -> tuple[Tenant, ...]:
    """Return every tenant Nr3 can know about.

    Priority:
    - If a real client root is mounted, load every
      {root}/*/config/client.json folder dynamically.
    - Add tenants registered inside ICP.
    - Use built-in demo tenants only when no disk tenant and no registry
      tenant exists. Demo data must not leak into a real registry-only
      workspace.
    """
    registry = _load_tenants_from_registry()
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    loaded: tuple[Tenant, ...] = tuple()
    if client_dir and os.path.isdir(client_dir):
        loaded = _load_tenants_from_disk(client_dir)
    if loaded:
        by_id: dict[str, Tenant] = {tenant.id: tenant for tenant in registry}
        by_id.update({tenant.id: tenant for tenant in loaded})
        return tuple(sorted(by_id.values(), key=lambda t: t.id))
    if registry:
        return registry
    return _TENANTS


def using_placeholder_tenants() -> bool:
    """True only when the UI is showing the built-in demo tenant seed."""
    registry = _load_tenants_from_registry()
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    loaded: tuple[Tenant, ...] = tuple()
    if client_dir and os.path.isdir(client_dir):
        loaded = _load_tenants_from_disk(client_dir)
    return not loaded and not registry


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

# Slugs that the Internal Control Panel refuses to delete. `unboks`
# is the master / admin tenant -- it owns the control panel itself
# and is the source of operator logins, so wiping it would lock
# everyone out. Defense-in-depth lock requested by Benson 2026-05-20
# after the bulk-cleanup that left unboks as the only tenant.
RESERVED_SLUGS: frozenset[str] = frozenset({"unboks"})


class TenantCreateError(Exception):
    """Raised when create_tenant_directory cannot create a tenant (bad
    slug, slug already exists, client_dir not configured, etc.)."""


class TenantDeleteError(Exception):
    """Raised when delete_tenant_directory cannot delete a tenant
    (reserved slug, client_dir not configured, missing directory)."""


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


def delete_tenant_directory(slug: str,
                             client_dir: Optional[str] = None) -> None:
    """Remove <client_dir>/<slug>/ entirely (config + data + everything
    underneath).

    Raises ``TenantDeleteError`` if:
      * the slug fails ``validate_slug``
      * the slug is in ``RESERVED_SLUGS`` (the master tenant is locked)
      * ``NR3_TENANTS_CLIENT_DIR`` is not configured
      * ``<client_dir>/<slug>/`` does not exist

    Irreversible -- callers are expected to confirm with the operator
    before calling. The reserved-slug check happens BEFORE any disk
    work so a guarded slug can never be partially removed.
    """
    import shutil
    safe_slug = validate_slug(slug)
    if safe_slug in RESERVED_SLUGS:
        raise TenantDeleteError(
            f"Tenant {safe_slug!r} is reserved and cannot be deleted.")
    root = client_dir or os.getenv(
        "NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    if not root:
        raise TenantDeleteError(
            "NR3_TENANTS_CLIENT_DIR is not set -- cannot delete tenant.")
    tenant_root = os.path.join(root, safe_slug)
    if not os.path.isdir(tenant_root):
        raise TenantDeleteError(
            f"Tenant {safe_slug!r} not found at {tenant_root!r}.")
    shutil.rmtree(tenant_root)

