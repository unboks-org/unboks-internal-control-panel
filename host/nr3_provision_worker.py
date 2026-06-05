#!/usr/bin/env python3
"""Host-side Nr 3 tenant provisioner.

Runs as root on the VPS, outside the FastAPI container. It consumes JSON
jobs written by the Nr 3 app into ./data/provisioning/jobs and performs
the privileged host operations: writing /root/clients, Docker Compose,
nginx config, nginx reload, and health check.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,49}$")
RESERVED_SLUGS = {"unboks"}
PROVIDER_JSON_KEYS_TO_CLEAR = {
    "channel_account_allowlist",
    "whatsapp_connect_token",
    "zernio_account_id",
    "zernio_profile_id",
    "phone_number_id",
    "selected_phone_number_id",
    "display_phone_number",
    "waba_id",
    "whatsapp_phone_number_id",
    "whatsapp_display_phone_number",
    "whatsapp_waba_id",
    "whatsapp_provider_account_id",
    "meta_phone_number_id",
    "meta_waba_id",
}
PROVIDER_ENV_KEYS_TO_CLEAR = {
    "CHANNEL_ACCOUNT_ALLOWLIST",
    "META_PHONE_NUMBER_ID",
    "META_WABA_ID",
    "PHONE_NUMBER_ID",
    "WABA_ID",
    "WHATSAPP_CONNECT_TOKEN",
    "WHATSAPP_DISPLAY_PHONE_NUMBER",
    "WHATSAPP_PHONE_NUMBER_ID",
    "WHATSAPP_PROVIDER_ACCOUNT_ID",
    "WHATSAPP_WABA_ID",
    "ZERNIO_ACCOUNT_ID",
    "ZERNIO_PHONE_NUMBER_ID",
    "ZERNIO_PROFILE_ID",
}


def env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default).strip() or default)


QUEUE_DIR = env_path(
    "NR3_PROVISION_QUEUE_DIR",
    "/root/unboks-internal-control-panel/data/provisioning/jobs",
)
RESULT_DIR = env_path(
    "NR3_PROVISION_RESULT_DIR",
    "/root/unboks-internal-control-panel/data/provisioning/results",
)
FAILED_DIR = env_path(
    "NR3_PROVISION_FAILED_DIR",
    "/root/unboks-internal-control-panel/data/provisioning/failed",
)
CLIENTS_ROOT = env_path("NR3_PROVISION_CLIENTS_ROOT", "/root/clients")
NGINX_SITE = env_path("NR3_PROVISION_NGINX_SITE", "/etc/nginx/sites-enabled/api-unboks")
BRIDGE_TOKEN_FILE = env_path(
    "NR3_PROVISION_BRIDGE_TOKEN_FILE",
    "/root/clients/_shared/nr3_internal_api_token",
)
BRIDGE_TOKEN_DIR = env_path(
    "NR3_PROVISION_BRIDGE_TOKEN_DIR",
    "/root/clients/_shared/nr3_bridge_tokens",
)
ANTHROPIC_KEY_FILE = env_path(
    "NR3_PROVISION_ANTHROPIC_KEY_FILE",
    "/root/clients/_shared/anthropic_api_key",
)
LATE_API_KEY_FILE = env_path(
    "NR3_PROVISION_LATE_API_KEY_FILE",
    "/root/clients/_shared/late_api_key",
)
ZERNIO_WEBHOOK_SECRET_FILE = env_path(
    "NR3_PROVISION_ZERNIO_WEBHOOK_SECRET_FILE",
    "/root/clients/_shared/zernio_webhook_secret",
)
NGINX_BACKUP_DIR = env_path(
    "NR3_PROVISION_NGINX_BACKUP_DIR",
    "/root/nginx-sites-enabled-backups",
)
DELETED_TENANTS_ROOT = env_path(
    "NR3_DELETED_TENANTS_ROOT",
    "/root/_deleted_tenants",
)
ICP_DATA_DIR = env_path(
    "NR3_ICP_DATA_DIR",
    "/root/unboks-internal-control-panel/data",
)
IMPORT_PAYLOAD_DIR = ICP_DATA_DIR / "tenant_import_payloads"
POLL_SECONDS = float(os.getenv("NR3_PROVISION_POLL_SECONDS", "2"))


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def write_result(job_id: str, payload: dict[str, Any]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    payload.setdefault("finished_at", utc_now())
    tmp = RESULT_DIR / f".{job_id}.tmp"
    final = RESULT_DIR / f"{job_id}.json"
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_or_create_tenant_bridge_token(slug: str) -> str:
    BRIDGE_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BRIDGE_TOKEN_DIR.chmod(0o700)
    except OSError:
        pass
    path = BRIDGE_TOKEN_DIR / slug
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(48)
        atomic_write(path, token + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    if len(token) < 32:
        raise RuntimeError(f"Tenant bridge token is too short: {path}")
    return token


def read_optional_anthropic_key() -> str:
    return read_optional_secret(ANTHROPIC_KEY_FILE)


def read_optional_secret(path: Path) -> str:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token


def platform_env_text(slug: str, password: str, created_at: str, token: str) -> str:
    text = (
        f"# platform.env for tenant {slug}\n"
        f"# Generated by Nr3 at {created_at}\n"
        f"DASHBOARD_PASSWORD={password}\n"
        f"TENANT_ID={slug}\n"
        f"TENANT_SLUG={slug}\n"
        f"NR3_INTERNAL_OVERRIDES_URL=http://wtyj-admin:8010\n"
        f"NR3_INTERNAL_API_TOKEN={token}\n"
        f"ICP_OVERRIDES_TTL_SECONDS=5\n"
    )
    anthropic_key = read_optional_anthropic_key()
    if anthropic_key:
        text += f"ANTHROPIC_API_KEY={anthropic_key}\n"
    late_api_key = read_optional_secret(LATE_API_KEY_FILE)
    if late_api_key:
        text += f"LATE_API_KEY={late_api_key}\n"
    zernio_webhook_secret = read_optional_secret(ZERNIO_WEBHOOK_SECRET_FILE)
    if zernio_webhook_secret:
        text += f"ZERNIO_WEBHOOK_SECRET={zernio_webhook_secret}\n"
    return text


def insert_nginx_block(slug: str, block: str) -> None:
    text = NGINX_SITE.read_text(encoding="utf-8")
    # Never place backups inside sites-enabled; nginx reads every file
    # there and backup copies create duplicate server_name warnings.
    NGINX_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = NGINX_BACKUP_DIR / (
        f"{NGINX_SITE.name}.bak-nr3-{slug}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(NGINX_SITE, backup)

    start = f"# BEGIN UNBOKS TENANT {slug}"
    end = f"# END UNBOKS TENANT {slug}"
    while start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = before.rstrip() + "\n" + after.lstrip("\n")

    marker = "    server_name api.unboks.org;\n"
    if marker not in text:
        marker = "server_name api.unboks.org;\n"
    if marker not in text:
        raise RuntimeError("Could not find server_name api.unboks.org in nginx site file")
    text = text.replace(marker, marker + "\n" + block + "\n", 1)
    NGINX_SITE.write_text(text, encoding="utf-8")

    try:
        run(["nginx", "-t"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, NGINX_SITE)
        raise


def remove_nginx_block(slug: str) -> str:
    text = NGINX_SITE.read_text(encoding="utf-8")
    NGINX_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = NGINX_BACKUP_DIR / (
        f"{NGINX_SITE.name}.bak-nr3-delete-{slug}-"
        f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    )
    shutil.copy2(NGINX_SITE, backup)

    start = f"# BEGIN UNBOKS TENANT {slug}"
    end = f"# END UNBOKS TENANT {slug}"
    removed = 0
    while start in text and end in text:
        before, rest = text.split(start, 1)
        _, after = rest.split(end, 1)
        text = before.rstrip() + "\n" + after.lstrip("\n")
        removed += 1
    if removed == 0:
        return "nginx tenant block was not present"

    NGINX_SITE.write_text(text, encoding="utf-8")
    try:
        run(["nginx", "-t"])
    except subprocess.CalledProcessError:
        shutil.copy2(backup, NGINX_SITE)
        raise
    run(["systemctl", "reload", "nginx"])
    return f"removed {removed} nginx tenant block(s)"


def wait_for_health(host_port: int, timeout: int = 45) -> str:
    url = f"http://127.0.0.1:{host_port}/health"
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                body = response.read(300).decode("utf-8", errors="replace")
                if 200 <= response.status < 500:
                    return f"{url} -> HTTP {response.status} {body}".strip()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Tenant health check timed out for {url}: {last_error}")


def rollback_failed_provision(slug: str, tenant_dir: Path, details: list[str]) -> None:
    """Best-effort rollback for a tenant provision job that failed mid-flight."""
    if (tenant_dir / "docker-compose.yml").exists():
        down = run(
            ["docker", "compose", "down", "-v", "--remove-orphans"],
            cwd=tenant_dir,
            check=False,
        )
        details.append(f"rollback docker compose down returned {down.returncode}")
    rm = run(["docker", "rm", "-f", f"wtyj-{slug}"], check=False)
    if rm.returncode == 0:
        details.append(f"rollback removed container wtyj-{slug}")
    try:
        nginx_detail = remove_nginx_block(slug)
        details.append(f"rollback nginx: {nginx_detail}")
    except Exception as exc:
        details.append(f"rollback nginx failed: {str(exc)[:200]}")
    if tenant_dir.exists():
        shutil.rmtree(tenant_dir, ignore_errors=True)
        details.append(f"rollback removed tenant folder {tenant_dir}")


def validate_slug(raw: object) -> str:
    slug = str(raw or "")
    if not SLUG_RE.match(slug):
        raise RuntimeError(f"Invalid slug in provisioning job: {slug!r}")
    return slug


def update_client_status(tenant_dir: Path, status: str) -> None:
    client_path = tenant_dir / "config" / "client.json"
    data = json.loads(client_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"client.json is not an object: {client_path}")
    business = data.get("business")
    if isinstance(business, dict) and business:
        business["status"] = status
    data["status"] = status
    client_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def update_dashboard_password(tenant_dir: Path, slug: str, new_password: str) -> None:
    client_path = tenant_dir / "config" / "client.json"
    data = json.loads(client_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"client.json is not an object: {client_path}")
    data["password"] = new_password
    data["dashboard_access_key"] = new_password
    data["password_updated_at"] = utc_now()
    business = data.get("business")
    if isinstance(business, dict) and business:
        business["password_updated_at"] = data["password_updated_at"]
    client_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    env_path = tenant_dir / "config" / "platform.env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [f"# platform.env for tenant {slug}"]
    replaced = False
    out: list[str] = []
    for line in lines:
        if line.startswith("DASHBOARD_PASSWORD="):
            out.append(f"DASHBOARD_PASSWORD={new_password}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"DASHBOARD_PASSWORD={new_password}")
    env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")


def repair_whatsapp_allowlist(
    tenant_dir: Path,
    *,
    zernio_account_id: str,
    note: str,
) -> None:
    account_id = str(zernio_account_id or "").strip()
    if not account_id:
        raise RuntimeError("Zernio account id is required for allowlist repair.")
    client_path = tenant_dir / "config" / "client.json"
    data = json.loads(client_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"client.json is not an object: {client_path}")
    existing = data.get("channel_account_allowlist")
    accounts: list[str] = []
    if isinstance(existing, dict):
        raw_accounts = existing.get("zernio_accounts")
        if isinstance(raw_accounts, list):
            accounts = [str(item).strip() for item in raw_accounts if str(item).strip()]
    if account_id not in accounts:
        accounts.append(account_id)
    data["channel_account_allowlist"] = {
        "mode": "strict",
        "zernio_accounts": accounts,
        "notes": str(note or "").strip()
        or "Strict account allowlist maintained by Nr3 WhatsApp connection state.",
    }
    client_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def backup_tenant_before_delete(slug: str, tenant_dir: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup_dir = DELETED_TENANTS_ROOT / f"{slug}-{stamp}"
    backup_dir.mkdir(parents=True, exist_ok=False)
    if tenant_dir.is_dir():
        shutil.copytree(tenant_dir, backup_dir / "client")
        tenant_folder_present = True
    else:
        (backup_dir / "client-missing.txt").write_text(
            f"Tenant folder was already missing at delete time: {tenant_dir}\n",
            encoding="utf-8",
        )
        tenant_folder_present = False
    if ICP_DATA_DIR.exists():
        shutil.copytree(
            ICP_DATA_DIR,
            backup_dir / "icp-data",
            ignore=shutil.ignore_patterns("provisioning"),
        )
    manifest = {
        "slug": slug,
        "deleted_at": utc_now(),
        "tenant_dir": str(tenant_dir),
        "tenant_folder_present": tenant_folder_present,
        "backup_dir": str(backup_dir),
    }
    (backup_dir / "DELETE_MANIFEST.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return backup_dir


def process_delete_tenant(job_id: str, job: dict[str, Any], slug: str) -> None:
    if str(job.get("typed_slug") or "") != slug:
        raise RuntimeError("Typed slug confirmation does not match tenant slug.")
    if str(job.get("final_confirmation") or "") != "DELETE FOREVER":
        raise RuntimeError("Final delete confirmation text is invalid.")

    tenant_dir = CLIENTS_ROOT / slug
    details: list[str] = []
    backup_dir = backup_tenant_before_delete(slug, tenant_dir)
    details.append(f"backup created at {backup_dir}")
    if not tenant_dir.is_dir():
        details.append(f"tenant folder was already missing: {tenant_dir}")

    if (tenant_dir / "docker-compose.yml").exists():
        down = run(
            ["docker", "compose", "down", "-v", "--remove-orphans"],
            cwd=tenant_dir,
            check=False,
        )
        details.append(f"docker compose down returned {down.returncode}")
    rm = run(["docker", "rm", "-f", f"wtyj-{slug}"], check=False)
    if rm.returncode == 0:
        details.append(f"removed container wtyj-{slug}")

    if tenant_dir.is_dir():
        shutil.rmtree(tenant_dir)
        details.append(f"deleted tenant folder {tenant_dir}")
    details.append(remove_nginx_block(slug))

    write_result(job_id, {
        "status": "succeeded",
        "job_type": "tenant_action",
        "action": "delete_tenant",
        "slug": slug,
        "message": f"Tenant {slug} was permanently deleted on the VPS.",
        "details": details,
        "backup_path": str(backup_dir),
    })


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def clear_provider_json_values(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in PROVIDER_JSON_KEYS_TO_CLEAR:
                if isinstance(item, list):
                    out[str(key)] = []
                elif isinstance(item, dict):
                    out[str(key)] = {}
                else:
                    out[str(key)] = ""
            else:
                out[str(key)] = clear_provider_json_values(item)
        return out
    if isinstance(value, list):
        return [clear_provider_json_values(item) for item in value]
    return value


def extract_client_tree_from_backup(package_path: Path) -> Path:
    allowed_root = IMPORT_PAYLOAD_DIR.resolve()
    resolved = package_path.resolve()
    if allowed_root not in (resolved, *resolved.parents):
        raise RuntimeError("Backup package path is outside the approved import payload directory.")
    if not zipfile.is_zipfile(resolved):
        raise RuntimeError("Backup package is not a valid Unboks backup file.")
    root = Path(tempfile.mkdtemp(prefix="nr3-client-tree-"))
    extracted = 0
    with zipfile.ZipFile(resolved) as zf:
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith("client_tree/"):
                continue
            rel = Path(info.filename[len("client_tree/"):])
            if rel.is_absolute() or ".." in rel.parts:
                raise RuntimeError(f"Unsafe client tree path in backup: {info.filename}")
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted += 1
    if extracted == 0:
        raise RuntimeError("Backup package does not include a client_tree runtime folder.")
    return root


def rewrite_restored_runtime_identity(
    target: str,
    source_root: Path,
    target_dir: Path,
    previous_dir: Path,
    *,
    preserve_provider_connection: bool,
) -> None:
    previous_compose = ""
    previous_client = read_json_file(previous_dir / "config" / "client.json")
    if (previous_dir / "docker-compose.yml").exists():
        previous_compose = (previous_dir / "docker-compose.yml").read_text(encoding="utf-8")
    source_client = read_json_file(source_root / "config" / "client.json")
    source_slug = validate_slug(source_client.get("slug") or target)

    client_path = target_dir / "config" / "client.json"
    client = read_json_file(client_path)
    if not preserve_provider_connection:
        client = clear_provider_json_values(client)
    client["slug"] = target
    for key in ("host_port", "port"):
        if previous_client.get(key) is not None:
            client[key] = previous_client[key]
    business = client.get("business")
    if isinstance(business, dict):
        business["slug"] = target
    client_path.parent.mkdir(parents=True, exist_ok=True)
    client_path.write_text(json.dumps(client, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    env_path = target_dir / "config" / "platform.env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        seen_id = False
        seen_slug = False
        out: list[str] = []
        for line in lines:
            if line.startswith("TENANT_ID="):
                out.append(f"TENANT_ID={target}")
                seen_id = True
            elif line.startswith("TENANT_SLUG="):
                out.append(f"TENANT_SLUG={target}")
                seen_slug = True
            elif line.split("=", 1)[0].strip() in PROVIDER_ENV_KEYS_TO_CLEAR:
                continue
            elif line.startswith("# platform.env for tenant "):
                out.append(f"# platform.env for tenant {target}")
            else:
                out.append(line)
        if not seen_id:
            out.append(f"TENANT_ID={target}")
        if not seen_slug:
            out.append(f"TENANT_SLUG={target}")
        env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    compose_path = target_dir / "docker-compose.yml"
    if previous_compose:
        compose_path.write_text(previous_compose, encoding="utf-8")
    elif compose_path.exists():
        text = compose_path.read_text(encoding="utf-8")
        text = text.replace(f"container_name: wtyj-{source_slug}", f"container_name: wtyj-{target}")
        text = text.replace(f"/api/{source_slug}/", f"/api/{target}/")
        text = text.replace(f"tenant {source_slug}", f"tenant {target}")
        compose_path.write_text(text, encoding="utf-8")


def process_restore_tenant_runtime(job_id: str, job: dict[str, Any], slug: str) -> None:
    package_path = Path(str(job.get("backup_package_path") or ""))
    source_root = extract_client_tree_from_backup(package_path)
    tenant_dir = CLIENTS_ROOT / slug
    previous_dir = Path(tempfile.mkdtemp(prefix=f"nr3-prev-{slug}-"))
    if tenant_dir.exists():
        shutil.copytree(tenant_dir, previous_dir, dirs_exist_ok=True)
        shutil.rmtree(tenant_dir)
    tenant_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, tenant_dir)
    preserve_provider_connection = bool(job.get("preserve_provider_connection", True))
    rewrite_restored_runtime_identity(
        slug,
        source_root,
        tenant_dir,
        previous_dir,
        preserve_provider_connection=preserve_provider_connection,
    )
    run(["docker", "compose", "up", "-d", "--force-recreate"], cwd=tenant_dir)
    write_result(job_id, {
        "status": "succeeded",
        "job_type": "tenant_action",
        "action": "restore_tenant_runtime",
        "slug": slug,
        "message": f"Tenant {slug} runtime was restored from backup and recreated.",
        "details": [
            f"runtime restored to {tenant_dir}",
            f"docker compose up -d --force-recreate completed for {slug}",
            f"provider connection preserved: {preserve_provider_connection}",
        ],
        "dashboard_url": str(job.get("dashboard_url") or f"https://dashboard.unboks.org/{slug}"),
    })


def process_tenant_action(job_id: str, job: dict[str, Any]) -> None:
    action = str(job.get("action") or "")
    slug = validate_slug(job.get("slug"))
    if slug in RESERVED_SLUGS:
        raise RuntimeError(f"Tenant {slug!r} is reserved and cannot be changed by host action.")
    if action == "delete_tenant":
        process_delete_tenant(job_id, job, slug)
        return
    if action == "restore_tenant_runtime":
        process_restore_tenant_runtime(job_id, job, slug)
        return
    if action not in {
        "suspend_tenant",
        "unpause_tenant",
        "reset_dashboard_password",
        "restart_tenant",
        "repair_whatsapp_allowlist",
    }:
        raise RuntimeError(f"Unsupported tenant action: {action!r}")

    tenant_dir = CLIENTS_ROOT / slug
    if not tenant_dir.is_dir():
        raise RuntimeError(f"Tenant directory not found: {tenant_dir}")
    details: list[str] = []
    if action == "reset_dashboard_password":
        new_password = str(job.get("new_password") or "").strip()
        if len(new_password) < 10:
            raise RuntimeError("New dashboard password is missing or too short.")
        update_dashboard_password(tenant_dir, slug, new_password)
        details.append("client.json and platform.env dashboard password updated")
        running = run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=False,
        )
        if running.returncode == 0 and f"wtyj-{slug}" in running.stdout.splitlines():
            run(["docker", "compose", "up", "-d", "--force-recreate"], cwd=tenant_dir)
            details.append(f"running container wtyj-{slug} recreated")
        else:
            details.append(f"container wtyj-{slug} was not running; files updated only")
        message = f"Dashboard password reset for tenant {slug}."
    elif action == "repair_whatsapp_allowlist":
        account_id = str(job.get("zernio_account_id") or "").strip()
        repair_whatsapp_allowlist(
            tenant_dir,
            zernio_account_id=account_id,
            note=str(job.get("allowlist_note") or ""),
        )
        details.append("client.json strict channel_account_allowlist repaired")
        message = f"WhatsApp strict allowlist repaired for tenant {slug}."
    elif action == "restart_tenant":
        run(["docker", "compose", "up", "-d", "--force-recreate"], cwd=tenant_dir)
        details.append(f"docker compose up -d --force-recreate completed for {slug}")
        message = f"Tenant {slug} container was recreated on the VPS."
    elif action == "suspend_tenant":
        update_client_status(tenant_dir, "inactive")
        details.append("client.json status set to inactive")
        run(["docker", "compose", "stop"], cwd=tenant_dir)
        details.append(f"docker compose stop completed for {slug}")
        message = f"Tenant {slug} was made inactive on the VPS."
    else:
        update_client_status(tenant_dir, "active")
        details.append("client.json status set to active")
        run(["docker", "compose", "up", "-d"], cwd=tenant_dir)
        details.append(f"docker compose up -d completed for {slug}")
        message = f"Tenant {slug} was made active on the VPS."
    dashboard_url = str(job.get("dashboard_url") or f"https://dashboard.unboks.org/{slug}")
    write_result(job_id, {
        "status": "succeeded",
        "job_type": "tenant_action",
        "action": action,
        "slug": slug,
        "message": message,
        "details": details,
        "dashboard_url": dashboard_url,
    })


def process_job(job_path: Path) -> None:
    processing_path = job_path.with_suffix(".processing")
    try:
        os.replace(job_path, processing_path)
    except FileNotFoundError:
        return

    job_id = processing_path.stem
    details: list[str] = []
    slug = ""
    tenant_dir: Path | None = None
    rollback_on_failure = False
    try:
        job = json.loads(processing_path.read_text(encoding="utf-8"))
        job_id = str(job.get("job_id") or job_id)
        if job.get("job_type") == "tenant_action":
            process_tenant_action(job_id, job)
            processing_path.unlink(missing_ok=True)
            return
        slug = validate_slug(job.get("slug"))
        host_port = int(job["host_port"])
        if host_port < 1 or host_port > 65535:
            raise RuntimeError(f"Invalid host port in provisioning job: {host_port}")
        client_data = job.get("client_data")
        if not isinstance(client_data, dict):
            raise RuntimeError("client_data must be a JSON object")
        if client_data.get("slug") != slug:
            raise RuntimeError("client_data.slug does not match job slug")
        password = str(client_data.get("password") or "")
        if len(password) < 8:
            raise RuntimeError("client_data.password is missing or too short")
        docker_compose_text = str(job.get("docker_compose_text") or "")
        if f"container_name: wtyj-{slug}" not in docker_compose_text:
            raise RuntimeError("docker compose text does not target this tenant slug")
        nginx_block = str(job.get("managed_nginx_block_text") or "")
        if f"/api/{slug}/" not in nginx_block:
            raise RuntimeError("nginx block does not target this tenant slug")

        tenant_dir = CLIENTS_ROOT / slug
        if tenant_dir.exists():
            raise RuntimeError(f"Tenant directory already exists: {tenant_dir}")
        token = read_or_create_tenant_bridge_token(slug)

        (tenant_dir / "config").mkdir(parents=True)
        rollback_on_failure = True
        (tenant_dir / "data").mkdir()
        (tenant_dir / "logs").mkdir()
        (tenant_dir / "config" / "client.json").write_text(
            json.dumps(client_data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        (tenant_dir / "config" / "platform.env").write_text(
            platform_env_text(slug, password, str(client_data.get("created_at") or utc_now()), token),
            encoding="utf-8",
        )
        (tenant_dir / "docker-compose.yml").write_text(docker_compose_text.rstrip() + "\n", encoding="utf-8")
        details.append(f"wrote tenant files under {tenant_dir}")

        inspect = run(["docker", "network", "inspect", "unboks-control"], check=False)
        if inspect.returncode != 0:
            run(["docker", "network", "create", "unboks-control"])
            details.append("created docker network unboks-control")

        run(["docker", "compose", "up", "-d"], cwd=tenant_dir)
        details.append(f"started docker compose for {slug}")

        insert_nginx_block(slug, nginx_block)
        run(["systemctl", "reload", "nginx"])
        details.append("nginx config tested and reloaded")

        health = wait_for_health(host_port)
        details.append(health)

        dashboard_url = str(job.get("dashboard_url") or f"https://dashboard.unboks.org/{slug}")
        write_result(job_id, {
            "status": "succeeded",
            "job_type": "tenant_provision",
            "slug": slug,
            "message": f"Tenant {slug} was provisioned on the VPS.",
            "details": details,
            "dashboard_url": dashboard_url,
            "health_url": f"http://127.0.0.1:{host_port}/health",
        })
        processing_path.unlink(missing_ok=True)
    except Exception as exc:
        if rollback_on_failure and slug and tenant_dir is not None:
            try:
                rollback_failed_provision(slug, tenant_dir, details)
            except Exception as rollback_exc:
                details.append(f"rollback failed: {str(rollback_exc)[:200]}")
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        failed_copy = FAILED_DIR / processing_path.name
        try:
            os.replace(processing_path, failed_copy)
        except OSError:
            pass
        write_result(job_id, {
            "status": "failed",
            "job_type": "tenant_provision" if slug else "unknown",
            "slug": slug,
            "message": str(exc),
            "details": details,
        })


def run_forever() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Nr3 provision worker watching {QUEUE_DIR}", flush=True)
    while True:
        for job_path in sorted(QUEUE_DIR.glob("*.json")):
            process_job(job_path)
        time.sleep(POLL_SECONDS)


def run_once() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    for job_path in sorted(QUEUE_DIR.glob("*.json")):
        process_job(job_path)


if __name__ == "__main__":
    if os.geteuid() != 0:
        print("nr3_provision_worker.py must run as root", file=sys.stderr)
        raise SystemExit(1)
    if "--once" in sys.argv:
        run_once()
    else:
        run_forever()
