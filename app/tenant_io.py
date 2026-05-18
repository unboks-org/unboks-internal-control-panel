import os
import json
import subprocess
from pathlib import Path


def provision_new_tenant(slug: str, password: str):
    vps_host = os.environ.get("VPS_HOST")
    if not vps_host:
        print("[ICP ERROR] VPS_HOST env var is not set")
        return False

    ssh_key_path = os.environ.get("VPS_SSH_KEY", "/home/runner/.ssh/id_ed25519_vps")
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
        remote_cmd
    ]

    try:
        proc = subprocess.run(
            ssh_cmd,
            input=json_content,
            text=True,
            capture_output=True,
            timeout=25
        )
        if proc.returncode == 0:
            print(f"[ICP] Successfully provisioned tenant on VPS: {slug}")
            return True
        else:
            print(f"[ICP ERROR] SSH failed (exit {proc.returncode}): {proc.stderr}")
            return False
    except Exception as e:
        print(f"[ICP ERROR] Provisioning exception: {str(e)}")
        return False