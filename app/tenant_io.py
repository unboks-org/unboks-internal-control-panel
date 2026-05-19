"""Provisioning hook for a new tenant.

THIS IS THE SINGLE INSERTION POINT for the upcoming VPS
provisioning service. Today the body is a no-op stub that just
logs the intent — the actual write to <NR3_TENANTS_CLIENT_DIR>/
<slug>/config/client.json on the VPS is handled by a separate
service that Nr3 will call over HTTP.

Contract (intentional and stable):
    provision_new_tenant(slug, password) -> bool
    - Returns True on success, False on failure.
    - Never raises. The wizard wraps the call in try/except
      (belt-and-braces) but documented behaviour is to return
      False on every recoverable error.
    - Logs success at INFO and any failure at ERROR via
      logging.getLogger(__name__) so the wizard's structured
      events (tenant_create.provisioning_*) line up with the
      provisioning module's own emissions.

When the HTTP provisioning service goes live, replace the body of
provision_new_tenant with an httpx / requests POST to the service
endpoint. The call site in app.routes.admin
(admin_tenant_create_submit) does NOT have to change.
"""
import logging


logger = logging.getLogger(__name__)


def provision_new_tenant(slug: str, password: str) -> bool:
    """Stub: the real call to the VPS provisioning service ships in
    a follow-up brief. Returns True so the wizard records a clean
    "tenant_create.provisioning_succeeded" event for now. When the
    service is wired, change the body here — nothing else."""
    logger.info(
        "provision_new_tenant.stub slug=%s "
        "(VPS provisioning service not yet wired; no remote write performed)",
        slug)
    return True
