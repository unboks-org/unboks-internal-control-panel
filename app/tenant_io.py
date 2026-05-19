"""VPS provisioning for new tenants.

Called by the Add-New-Tenant wizard after the local folder is
written. SSHes to the configured VPS host and creates
<NR3_TENANTS_CLIENT_DIR>/<slug>/config/client.json on the remote.

Env vars:
    VPS_HOST                 — required. SSH target host.
    VPS_SSH_KEY              — optional. Path to the private key
                               (default: ~runner/.ssh/id_ed25519_vps).
    NR3_TENANTS_CLIENT_DIR   — optional. Tenants root on the VPS
                               (default: /root/wtyj/tenant_root).
"""
import json
import logging
import os
import subprocess


logger = logging.getLogger(__name__)


def provision_new_tenant(slug: str, password: str) -> bool:
    """Write <NR3_TENANTS_CLIENT_DIR>/<slug>/config/client.json on
    the VPS over SSH. Returns True on success, False on failure.
    Never raises — the wizard wraps this call in try/except but
    relying on a clean False return is the documented contract.
    """
    vps_host = os.environ.get("VPS_HOST")
    if not vps_host:
        logger.error("provision_new_tenant.config_missing var=VPS_HOST slug=%s", slug)
        return False

    ssh_key_path = os.environ.get(
        "VPS_SSH_KEY", "/home/runner/.ssh/id_ed25519_vps")
    base = os.environ.get("NR3_TENANTS_CLIENT_DIR", "/root/wtyj/tenant_root")

    client_data = {"business": {"name": slug, "password": password}}
    json_content = json.dumps(client_data, indent=2)

    remote_dir = f"{base}/{slug}/config"
    remote_file = f"{remote_dir}/client.json"
    remote_cmd = f"mkdir -p '{remote_dir}' && cat > '{remote_file}'"

    ssh_cmd = [
        "ssh",
        "-i", ssh_key_path,
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=15",
        f"root@{vps_host}",
        remote_cmd,
    ]

    try:
        proc = subprocess.run(
            ssh_cmd,
            input=json_content,
            text=True,
            capture_output=True,
            timeout=25,
        )
    except Exception as exc:
        logger.error(
            "provision_new_tenant.ssh_exception slug=%s exc=%r", slug, exc)
        return False

    if proc.returncode == 0:
        logger.info(
            "provision_new_tenant.ok slug=%s host=%s remote_file=%s",
            slug, vps_host, remote_file)
        return True

    logger.error(
        "provision_new_tenant.ssh_failed slug=%s exit=%d stderr=%r",
        slug, proc.returncode, proc.stderr)
    return False
