"""Tenant registry for the ICP command center."""

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class TenantHealth:
    inbox: str = "unknown"        # ok | warn | down | unknown
    ai_agent: str = "unknown"
    channels: str = "unknown"
    escalations: str = "unknown"


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


NOTE_PRIORITIES: tuple[tuple[str, str], ...] = (
    ("normal", "Normal"),
    ("important", "Important"),
    ("critical", "Critical"),
)


@dataclass(frozen=True)
class TenantNote:
    id: str
    body: str
    author: str = "—"
    created_at: str = "—"
    priority: str = "normal"    # normal | important | critical
    pinned: bool = False
    follow_up_date: Optional[str] = None
    follow_up_done: bool = False


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    status: str  # active | inactive
    health: TenantHealth = field(default_factory=TenantHealth)
    agent: TenantAgent = field(default_factory=TenantAgent)
    escalations: TenantEscalations = field(default_factory=TenantEscalations)
    notes: tuple[TenantNote, ...] = field(default_factory=tuple)


_TENANTS: tuple[Tenant, ...] = (
    Tenant(id="unboks", name="Unboks", status="active"),
)


def sorted_notes(notes: tuple[TenantNote, ...]) -> tuple[TenantNote, ...]:
    """Pinned first, then in original order (newest-first is the caller's job)."""
    return tuple(sorted(notes, key=lambda n: (0 if n.pinned else 1,)))


_DEFAULT_TENANTS_CLIENT_DIR = "/root/clients"
_DEFAULT_TENANT_REGISTRY_PATH = "data/tenant_registry.json"
_ALLOWED_STATUSES = ("active", "inactive")


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
    if isinstance(raw_status, str) and raw_status.strip():
        normalized = raw_status.strip().lower()
        status = normalized if normalized in _ALLOWED_STATUSES else "inactive"
    else:
        status = "active"
    return Tenant(
        id=tenant_id,
        name=name,
        status=status,
    )


def _load_tenants_from_disk(client_dir: str) -> tuple[Tenant, ...]:
    """J3-BE-01: discover tenants by globbing {client_dir}/*/config/client.json.

    Mapping (only id/name/status are pulled from disk; workspace-specific
    details are loaded from their dedicated stores later in the request):
      tenant.id     = business.slug, fallback to the parent directory name
      tenant.name   = business.name, fallback to tenant.id
      tenant.status = 'active' or 'inactive'

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
    with exclusive_file_lock(Path(path).with_suffix(Path(path).suffix + ".lock")):
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
        }
        _save_registry(data)


def update_tenant_status(slug: str, status: str) -> bool:
    """Best-effort status update for the ICP-visible tenant row.

    The host worker remains responsible for runtime state. This updates
    any mounted client.json plus the lightweight registry so the operator
    sees active/inactive honestly on the next render.
    """
    safe_slug = validate_slug(slug)
    normalized = (status or "").strip().lower()
    if normalized not in _ALLOWED_STATUSES:
        raise TenantCreateError(f"Unsupported tenant status: {status}")

    changed = False
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    if client_dir:
        client_path = Path(client_dir) / safe_slug / "config" / "client.json"
        if client_path.exists():
            with exclusive_file_lock(client_path.with_suffix(client_path.suffix + ".lock")):
                try:
                    data = json.loads(client_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                    data = None
                if isinstance(data, dict):
                    data["status"] = normalized
                    business = data.get("business")
                    if isinstance(business, dict) and business:
                        business["status"] = normalized
                    client_path.write_text(
                        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    changed = True

    existing = get_tenant(safe_slug)
    register_tenant({
        "slug": safe_slug,
        "name": existing.name if existing else safe_slug,
        "status": normalized,
    })
    return changed


def unregister_tenant(slug: str) -> bool:
    """Remove ``slug`` from the lightweight ICP registry.

    The registry is the second source of truth for the sidebar (alongside
    the disk glob). Removing the directory alone leaves a ghost entry
    here, which is exactly the bug we hit on 2026-05-20 with `roberto`.
    Returns True if a registry row was actually removed.

    Best-effort: silently returns False if the registry file is unset,
    missing, or malformed. Never raises -- the disk delete already
    succeeded by the time this is called and the caller is on the
    cleanup path.
    """
    path = _registry_path()
    if not path:
        return False
    with exclusive_file_lock(Path(path).with_suffix(Path(path).suffix + ".lock")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        tenants = data.get("tenants")
        if not isinstance(tenants, dict) or slug not in tenants:
            return False
        tenants.pop(slug, None)
        try:
            _save_registry(data)
        except OSError:
            return False
    return True


def forget_tenant_state(slug: str) -> None:
    """Best-effort cleanup of every local Nr3 state store for a tenant."""
    unregister_tenant(slug)
    try:
        from app.port_registry import release_tenant_port
        release_tenant_port(slug)
    except Exception:  # pragma: no cover -- defensive
        pass
    try:
        from app import channel_state, icp_overrides, tenant_notes
        channel_state.forget_tenant(slug)
        icp_overrides.forget_tenant(slug)
        tenant_notes.forget_tenant(slug)
    except Exception:  # pragma: no cover -- defensive
        pass
    try:
        from app import channel_connections
        channel_connections.forget_tenant(slug)
    except Exception:  # pragma: no cover -- defensive
        pass


def list_tenants() -> tuple[Tenant, ...]:
    """Return every tenant Nr3 can know about.

    Priority:
    - If a real client root is mounted, load every
      {root}/*/config/client.json folder dynamically.
    - Add tenants registered inside ICP.
    - Use a minimal built-in Unboks row only when no disk tenant and no
      registry tenant exists. This keeps local development usable.
    """
    try:
        from app.provisioning import reconcile_host_action_results
        reconcile_host_action_results()
    except Exception:  # pragma: no cover -- sidebar must never fail on reconciliation.
        pass
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


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    for tenant in list_tenants():
        if tenant.id == tenant_id:
            return tenant
    return None


def get_tenant_client_data(tenant_id: str) -> dict:
    """Return raw client.json data for a tenant when the client root is mounted.

    This is read-only and used for operator workflows that need safe tenant
    contact metadata such as email/name. Returns an empty dict when the runtime
    config file is unavailable.
    """
    safe_id = tenant_id.strip()
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", _DEFAULT_TENANTS_CLIENT_DIR).strip()
    if not safe_id or not client_dir:
        return {}
    path = os.path.join(client_dir, safe_id, "config", "client.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def tenant_contact_details(tenant_id: str) -> dict[str, str]:
    data = get_tenant_client_data(tenant_id)
    business = data.get("business")
    source = business if isinstance(business, dict) and business else data

    def first_text(*keys: str) -> str:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    email = first_text("email", "contact_email", "owner_email")
    contact_name = first_text("contact_person", "contact_name", "owner_name", "name")
    first_name = contact_name.split()[0] if contact_name.split() else ""
    return {
        "email": email,
        "contact_name": contact_name,
        "first_name": first_name,
    }


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
    # Belt-and-braces cleanup of every other place a tenant leaves
    # state. Each call is best-effort: the on-disk delete already
    # succeeded and a single ghost JSON row must not raise.
    forget_tenant_state(safe_slug)
from app.file_lock import exclusive_file_lock
