"""Placeholder tenant registry for J3-P0-04 / J3-P0-05.

The internal control panel is tenant-first. Real tenant storage will land in a
later milestone; for now we expose a small, hard-coded list so the UI shell can
be wired end-to-end without faking persistence.
"""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Tenant:
    id: str
    name: str
    status: str  # "active" | "paused"
    dashboard_status: str
    channels_status: str
    sot_status: str
    onboarding_status: str
    last_sync: str
    recent_changes: tuple[str, ...]


_TENANTS: tuple[Tenant, ...] = (
    Tenant(
        id="unboks-demo",
        name="Unboks Demo",
        status="active",
        dashboard_status="ok",
        channels_status="ok",
        sot_status="ok",
        onboarding_status="complete",
        last_sync="not yet wired",
        recent_changes=(
            "Placeholder: tenant created",
            "Placeholder: dashboard provisioned",
        ),
    ),
    Tenant(
        id="consulta-despertares",
        name="Consulta Despertares",
        status="active",
        dashboard_status="unknown",
        channels_status="unknown",
        sot_status="unknown",
        onboarding_status="unknown",
        last_sync="not yet wired",
        recent_changes=(),
    ),
    Tenant(
        id="bluefinn-charters",
        name="BlueFinn Charters",
        status="active",
        dashboard_status="unknown",
        channels_status="unknown",
        sot_status="unknown",
        onboarding_status="unknown",
        last_sync="not yet wired",
        recent_changes=(),
    ),
)


def list_tenants() -> tuple[Tenant, ...]:
    return _TENANTS


def get_tenant(tenant_id: str) -> Optional[Tenant]:
    for tenant in _TENANTS:
        if tenant.id == tenant_id:
            return tenant
    return None
