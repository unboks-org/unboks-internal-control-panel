#!/usr/bin/env python3
"""Host-side Nr 3 tenant provisioner.

Runs as root on the VPS, outside the FastAPI container. It consumes JSON
jobs written by the Nr 3 app into ./data/provisioning/jobs and performs
the privileged host operations: writing /root/clients, Docker Compose,
nginx config, nginx reload, and health check.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]{1,49}$")
RESERVED_SLUGS = {"unboks"}
PROVIDER_JSON_KEYS_TO_CLEAR = {
    "channel_account_allowlist",
    "whatsapp_connect_token",
    "zernio_account_id",
    "zernio_account_verified",
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
    "ZERNIO_ACCOUNT_VERIFIED",
    "ZERNIO_PHONE_NUMBER_ID",
    "ZERNIO_PROFILE_ID",
}
INITIAL_CHANNEL_ACCOUNT_ALLOWLIST = {
    "mode": "strict",
    "zernio_accounts": [],
    "notes": "No provider account is authorized until Nr3 verifies and selects it.",
}
REQUIRED_RUNTIME_ENV = {
    "TENANT_RUNTIME_CONTROLS_REQUIRED": "true",
    "TENANT_ACCOUNT_ALLOWLIST_REQUIRED": "true",
}
COMPOSE_FILENAMES = {
    "compose.yaml",
    "compose.yml",
    "compose.override.yaml",
    "compose.override.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "docker-compose.override.yaml",
    "docker-compose.override.yml",
}
RESTORE_OWNER_MARKER = ".nr3-restore-owner.json"
WORKER_LOCK_FILENAME = ".nr3-provision-worker.lock"
TENANT_DETAIL_LIMITS = {
    "name": 200,
    "contact_person": 200,
    "email": 320,
    "phone": 80,
    "whatsapp": 80,
    "website": 2048,
    "address": 1000,
    "logo_url": 2048,
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


class HostActionFailure(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        details: list[str] | None = None,
        safe_to_release: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = list(details or [])
        self.safe_to_release = safe_to_release


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _fsync_directory_required(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_regular_file_required(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RuntimeError(f"Backup path is not a regular file: {path}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_directory_tree_required(root: Path) -> None:
    """Persist every directory entry in a completed staging tree bottom-up."""
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError(f"Backup staging root is not a trusted directory: {root}")
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Backup staging tree contains an unsafe symlink: {path}")
        if path.is_dir():
            directories.append(path)
    directories.sort(
        key=lambda path: len(path.relative_to(root).parts),
        reverse=True,
    )
    for directory in directories:
        _fsync_directory_required(directory)


def job_payload_digest(job: dict[str, Any]) -> str:
    canonical = json.dumps(
        job,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def run(cmd: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@contextmanager
def worker_execution_lock():
    """Hold the one host-worker lease for this queue without following links."""
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = QUEUE_DIR / WORKER_LOCK_FILENAME
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    acquired = False
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"Worker lock is not a regular file: {lock_path}")
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HostActionFailure(
                f"Another Nr3 host worker already owns queue {QUEUE_DIR}."
            ) from exc
        acquired = True
        yield
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextmanager
def exclusive_client_json_lock(client_path: Path):
    """Share the exact ``client.json.lock`` protocol used by Nr3/runtime writers."""
    lock_path = client_path.with_suffix(client_path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"Client config lock is not a regular file: {lock_path}")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_client_json_no_follow(client_path: Path) -> dict[str, Any]:
    """Read a tenant-writable client.json without following its final path.

    Tenant containers mount their config directory read/write. A compromised
    tenant must not be able to replace ``client.json`` with a symlink to a
    different tenant and make the privileged worker copy that tenant's config
    into the attacker's own runtime during a read/modify/replace operation.
    """
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError("Safe no-follow client.json reads are unavailable.")
    flags = os.O_RDONLY | no_follow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(client_path, flags)
    except OSError as exc:
        raise RuntimeError(
            f"client.json is not a readable regular file: {client_path}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"client.json is not a regular file: {client_path}")
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                data = json.load(handle)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"client.json is unreadable: {client_path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if not isinstance(data, dict):
        raise RuntimeError(f"client.json is not an object: {client_path}")
    return data


def read_regular_text_no_follow(
    path: Path,
    *,
    description: str,
    allow_missing: bool = False,
) -> str | None:
    """Read a managed text file without following a tenant-controlled symlink."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise RuntimeError(f"Safe no-follow {description} reads are unavailable.")
    flags = os.O_RDONLY | no_follow
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise RuntimeError(f"{description} is not a readable regular file: {path}")
    except OSError as exc:
        raise RuntimeError(
            f"{description} is not a readable regular file: {path}"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{description} is not a regular file: {path}")
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as handle:
                fd = -1
                return handle.read()
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError(f"{description} is unreadable: {path}") from exc
    finally:
        if fd >= 0:
            os.close(fd)


def write_result(job_id: str, payload: dict[str, Any]) -> None:
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    payload.setdefault("job_id", job_id)
    payload.setdefault("finished_at", utc_now())
    if payload.get("status") == "failed":
        payload.setdefault("safe_to_release", False)
    final = RESULT_DIR / f"{job_id}.json"
    atomic_write(
        final,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )


def atomic_write(path: Path, content: str, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            fd = -1
            if mode is not None:
                os.fchmod(f.fileno(), mode)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        # Persist the rename itself, not only the temporary file contents. In
        # particular, a final success result must be durable before its delete
        # recovery record is retired.
        _fsync_directory_required(path.parent)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _tenant_bridge_token_path(slug: str) -> Path:
    return BRIDGE_TOKEN_DIR / validate_slug(slug)


def _prepare_bridge_token_dir() -> None:
    BRIDGE_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        BRIDGE_TOKEN_DIR.chmod(0o700)
    except OSError:
        pass


def read_or_create_tenant_bridge_token(slug: str) -> str:
    """Return a target tenant token, creating one only when it is absent."""
    _prepare_bridge_token_dir()
    path = _tenant_bridge_token_path(slug)
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if not token:
        token = secrets.token_urlsafe(48)
        atomic_write(path, token + "\n", mode=0o600)
    if len(token) < 32:
        raise RuntimeError(f"Tenant bridge token is too short: {path}")
    return token


def rotate_tenant_bridge_token(slug: str) -> str:
    """Create a fresh per-tenant token for every new provision attempt."""
    _prepare_bridge_token_dir()
    path = _tenant_bridge_token_path(slug)
    token = secrets.token_urlsafe(48)
    atomic_write(path, token + "\n", mode=0o600)
    return token


def remove_tenant_bridge_token(slug: str, details: list[str]) -> bool:
    """Remove the isolated token after runtime teardown has been proven."""
    _prepare_bridge_token_dir()
    path = _tenant_bridge_token_path(slug)
    try:
        path.unlink(missing_ok=True)
        _fsync_directory_required(path.parent)
    except OSError as exc:
        details.append(f"could not remove tenant bridge token {path}: {str(exc)[:200]}")
        return False
    if path.exists() or path.is_symlink():
        details.append(f"tenant bridge token still exists after removal: {path}")
        return False
    details.append(f"removed tenant bridge token {path}")
    return True


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
        f"TENANT_RUNTIME_CONTROLS_REQUIRED=true\n"
        f"TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true\n"
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


def _atomic_replace_managed_text(path: Path, content: str) -> None:
    """Atomically replace a managed file while preserving its host metadata."""
    try:
        target = path.resolve(strict=True)
        metadata = target.stat()
    except OSError as exc:
        raise RuntimeError(f"Managed file cannot be resolved safely: {path}") from exc
    if not target.is_file():
        raise RuntimeError(f"Managed path is not a regular file: {target}")
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.nr3-",
        suffix=".tmp",
        dir=target.parent,
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            os.fchmod(handle.fileno(), metadata.st_mode & 0o7777)
            os.fchown(handle.fileno(), metadata.st_uid, metadata.st_gid)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
        _fsync_directory_required(target.parent)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
    _atomic_replace_managed_text(NGINX_SITE, text)

    try:
        run(["nginx", "-t"])
    except subprocess.CalledProcessError:
        _atomic_replace_managed_text(
            NGINX_SITE,
            backup.read_text(encoding="utf-8"),
        )
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
    if removed:
        _atomic_replace_managed_text(NGINX_SITE, text)
        try:
            run(["nginx", "-t"])
        except subprocess.CalledProcessError:
            _atomic_replace_managed_text(
                NGINX_SITE,
                backup.read_text(encoding="utf-8"),
            )
            raise
    else:
        # A prior attempt may have published the marker removal and then
        # failed during reload. Disk absence is not proof that nginx stopped
        # serving the route, so retries still have to validate and reload.
        run(["nginx", "-t"])
    run(["systemctl", "reload", "nginx"])
    if removed == 0:
        return "nginx tenant block was absent; config validated and reloaded"
    return f"removed {removed} nginx tenant block(s)"


def wait_for_health(host_port: int, timeout: int = 45) -> str:
    url = f"http://127.0.0.1:{host_port}/health"
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=4) as response:
                body = response.read(300).decode("utf-8", errors="replace")
                if 200 <= response.status < 300:
                    return f"{url} -> HTTP {response.status} {body}".strip()
                last_error = f"HTTP {response.status} {body}".strip()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Tenant health check timed out for {url}: {last_error}")


def exact_container_is_absent(slug: str, details: list[str], *, context: str) -> bool:
    """Prove the exact managed container name is absent from Docker state."""
    container_name = f"wtyj-{slug}"
    try:
        listed = run(
            ["docker", "ps", "-a", "--format", "{{.Names}}"],
            check=False,
        )
    except Exception as exc:
        details.append(
            f"{context}: docker ps -a could not prove {container_name} absent: "
            f"{str(exc)[:200]}"
        )
        return False
    if listed.returncode != 0:
        details.append(
            f"{context}: docker ps -a returned {listed.returncode}; "
            f"{container_name} absence is unproven"
        )
        return False
    names = {
        line.strip()
        for line in str(listed.stdout or "").splitlines()
        if line.strip()
    }
    if container_name in names:
        details.append(f"{context}: exact container {container_name} is still present")
        return False
    details.append(
        f"{context}: docker ps -a confirmed exact container {container_name} is absent"
    )
    return True


def exact_container_runtime_state(
    slug: str,
    details: list[str],
    *,
    context: str,
) -> str:
    """Return absent/running/stopped only after exact successful Docker reads."""
    container_name = f"wtyj-{slug}"
    listed = run(["docker", "ps", "-a", "--format", "{{.Names}}"], check=False)
    if listed.returncode != 0:
        raise RuntimeError(
            f"{context}: docker ps -a could not prove the container state."
        )
    names = {
        line.strip()
        for line in str(listed.stdout or "").splitlines()
        if line.strip()
    }
    if container_name not in names:
        details.append(f"{context}: exact container {container_name} is absent")
        return "absent"
    inspected = run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        check=False,
    )
    state = str(inspected.stdout or "").strip().lower()
    if inspected.returncode != 0 or state not in {"true", "false"}:
        raise RuntimeError(
            f"{context}: docker inspect could not prove the exact container state."
        )
    resolved = "running" if state == "true" else "stopped"
    details.append(f"{context}: exact container {container_name} is {resolved}")
    return resolved


def tenant_artifacts_are_absent(
    slug: str,
    tenant_dir: Path,
    details: list[str],
    *,
    context: str,
) -> bool:
    """Return true only with exact, read-only proof all managed artifacts are absent."""
    tree_absent = not tenant_dir.exists() and not tenant_dir.is_symlink()
    if tree_absent:
        details.append(f"{context}: tenant tree {tenant_dir} is absent")
    else:
        details.append(f"{context}: tenant tree {tenant_dir} is still present")

    container_absent = exact_container_is_absent(slug, details, context=context)

    start = f"# BEGIN UNBOKS TENANT {slug}"
    end = f"# END UNBOKS TENANT {slug}"
    try:
        nginx_text = NGINX_SITE.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        details.append(
            f"{context}: nginx route file is unreadable; marker absence is unproven: "
            f"{str(exc)[:200]}"
        )
        nginx_absent = False
    else:
        nginx_absent = start not in nginx_text and end not in nginx_text
        if nginx_absent:
            details.append(f"{context}: nginx tenant route markers are absent")
        else:
            details.append(f"{context}: nginx tenant route marker is still present")
    return tree_absent and container_absent and nginx_absent


def rollback_failed_provision(slug: str, tenant_dir: Path, details: list[str]) -> bool:
    """Rollback owned artifacts and return exact absence proof for safe release."""
    if (tenant_dir / "docker-compose.yml").exists():
        try:
            trusted_compose_for_existing_tenant(slug, tenant_dir)
        except Exception as exc:
            details.append(
                "rollback skipped untrusted docker compose configuration: "
                f"{str(exc)[:200]}"
            )
        else:
            try:
                down = run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        "docker-compose.yml",
                        "down",
                        "-v",
                        "--remove-orphans",
                    ],
                    cwd=tenant_dir,
                    check=False,
                )
            except Exception as exc:
                details.append(
                    f"rollback docker compose down failed: {str(exc)[:200]}"
                )
            else:
                details.append(
                    f"rollback docker compose down returned {down.returncode}"
                )
    try:
        rm = run(["docker", "rm", "-f", f"wtyj-{slug}"], check=False)
    except Exception as exc:
        details.append(f"rollback docker rm failed: {str(exc)[:200]}")
    else:
        if rm.returncode == 0:
            details.append(f"rollback removed container wtyj-{slug}")
        else:
            details.append(f"rollback docker rm returned {rm.returncode}")

    # Never detach routes, delete mounted files, or remove the bridge token
    # while Docker state is unreadable or the exact container still exists.
    if not exact_container_is_absent(slug, details, context="rollback teardown"):
        return False
    nginx_reloaded = False
    try:
        nginx_detail = remove_nginx_block(slug)
        details.append(f"rollback nginx: {nginx_detail}")
        nginx_reloaded = True
    except Exception as exc:
        details.append(f"rollback nginx failed: {str(exc)[:200]}")
    if tenant_dir.exists():
        try:
            shutil.rmtree(tenant_dir)
            _fsync_directory_required(CLIENTS_ROOT)
        except OSError as exc:
            details.append(
                f"rollback could not remove tenant folder {tenant_dir}: "
                f"{str(exc)[:200]}"
            )
        else:
            details.append(f"rollback removed tenant folder {tenant_dir}")

    absent = tenant_artifacts_are_absent(
        slug,
        tenant_dir,
        details,
        context="rollback proof",
    )
    if not absent or not nginx_reloaded:
        return False
    return remove_tenant_bridge_token(slug, details)


def validate_slug(raw: object) -> str:
    slug = str(raw or "")
    if not SLUG_RE.match(slug):
        raise RuntimeError(f"Invalid slug in provisioning job: {slug!r}")
    return slug


def validate_host_port(raw: object) -> int:
    if isinstance(raw, bool):
        raise RuntimeError(f"Invalid host port in provisioning job: {raw!r}")
    try:
        host_port = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid host port in provisioning job: {raw!r}") from exc
    if host_port < 1024 or host_port > 65535:
        raise RuntimeError(f"Invalid host port in provisioning job: {host_port}")
    return host_port


def canonical_docker_compose_text(slug: str, host_port: int) -> str:
    return (
        f"# docker-compose.yml for tenant {slug}\n"
        "services:\n"
        "  agent:\n"
        "    image: wtyj-agent\n"
        f"    container_name: wtyj-{slug}\n"
        "    restart: unless-stopped\n"
        "    ports:\n"
        f'      - "127.0.0.1:{host_port}:8001"\n'
        "    env_file:\n"
        "      - ./config/platform.env\n"
        "    environment:\n"
        "      - GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=/app/config/calendar-key.json\n"
        "      - TENANT_RUNTIME_CONTROLS_REQUIRED=true\n"
        "      - TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true\n"
        "    volumes:\n"
        "      - ./config:/app/config:rw\n"
        "      - ./data:/app/data\n"
        "      - ./logs:/app/logs\n"
        "    networks:\n"
        "      - default\n"
        "      - unboks-control\n"
        "networks:\n"
        "  unboks-control:\n"
        "    external: true\n"
    )


def validate_canonical_docker_compose_text(
    slug: str,
    host_port: int,
    supplied_text: str,
    *,
    allow_legacy: bool = False,
) -> str:
    if host_port < 1024 or host_port > 65535:
        raise RuntimeError(f"Invalid host port for tenant runtime: {host_port}")
    expected = canonical_docker_compose_text(slug, host_port)
    normalized = supplied_text.rstrip("\n") + "\n"
    if normalized != expected:
        # Existing tenants created by the prior canonical generator did not
        # carry the two explicit fail-closed environment lines. Continue to
        # recognize that exact historical artifact for lifecycle operations,
        # but return the strict canonical form so restores migrate it and new
        # provisioning jobs can only supply the hardened form.
        legacy = expected.replace(
            "      - TENANT_RUNTIME_CONTROLS_REQUIRED=true\n"
            "      - TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true\n",
            "",
        )
        if normalized != legacy or not allow_legacy:
            raise RuntimeError("docker compose text is not the canonical tenant runtime")
    return expected


def trusted_compose_for_existing_tenant(slug: str, tenant_dir: Path) -> str:
    """Validate host-owned compose and recover its canonical bound port."""
    compose_path = tenant_dir / "docker-compose.yml"
    try:
        text = compose_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(
            f"Existing tenant has no readable trusted compose file: {compose_path}"
        ) from exc
    port_lines = [
        match.group(1)
        for line in text.splitlines()
        if (
            match := re.fullmatch(
                r'\s*-\s*"127\.0\.0\.1:([0-9]{1,5}):8001"\s*',
                line,
            )
        )
    ]
    if len(port_lines) != 1:
        raise RuntimeError(
            f"Existing tenant compose has no single canonical host port: {compose_path}"
        )
    return validate_canonical_docker_compose_text(
        slug,
        int(port_lines[0]),
        text,
        allow_legacy=True,
    )


def canonical_managed_nginx_block_text(slug: str, host_port: int) -> str:
    snippet = (
        f"# nginx route for tenant {slug}\n"
        f"location ^~ /api/{slug}/ {{\n"
        f"    proxy_set_header X-Tenant-Slug {slug};\n"
        "\n"
        "    if ($request_method = OPTIONS) {\n"
        '        add_header Access-Control-Allow-Origin "https://dashboard.unboks.org" always;\n'
        '        add_header Access-Control-Allow-Methods "GET, POST, PUT, PATCH, DELETE, OPTIONS" always;\n'
        '        add_header Access-Control-Allow-Headers "Authorization, Content-Type, Accept, Origin, X-Tenant-Slug, Cache-Control, Pragma" always;\n'
        '        add_header Access-Control-Allow-Credentials "true" always;\n'
        "        add_header Access-Control-Max-Age 86400 always;\n"
        "        return 204;\n"
        "    }\n"
        "\n"
        f"    proxy_pass http://127.0.0.1:{host_port}/;\n"
        "    proxy_set_header Host $host;\n"
        "    proxy_set_header X-Real-IP $remote_addr;\n"
        "    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "    proxy_set_header X-Forwarded-Proto $scheme;\n"
        "    proxy_hide_header X-Unboks-Tenant;\n"
        f'    add_header X-Unboks-Tenant "{slug}" always;\n'
        '    add_header Access-Control-Expose-Headers "X-Unboks-Tenant" always;\n'
        "}\n"
    )
    return (
        f"    # BEGIN UNBOKS TENANT {slug}\n"
        + "\n".join(
            ("    " + line if line else "")
            for line in snippet.rstrip().splitlines()
        )
        + f"\n    # END UNBOKS TENANT {slug}\n"
    )


def validate_managed_nginx_block(slug: str, host_port: int, block: str) -> None:
    """Reject ambiguous or non-removable tenant routes before touching nginx."""
    lines = [line.strip() for line in block.splitlines() if line.strip()]
    begin = f"# BEGIN UNBOKS TENANT {slug}"
    end = f"# END UNBOKS TENANT {slug}"
    if (
        not lines
        or lines[0] != begin
        or lines[-1] != end
        or lines.count(begin) != 1
        or lines.count(end) != 1
    ):
        raise RuntimeError("nginx block is missing one ordered canonical marker pair")

    active = [line for line in lines[1:-1] if not line.startswith("#")]
    expected_location = f"location ^~ /api/{slug}/ {{"
    locations = [line for line in active if re.match(r"^location\s", line)]
    if locations != [expected_location]:
        raise RuntimeError("nginx block does not contain one canonical tenant location")

    expected_proxy = f"proxy_pass http://127.0.0.1:{host_port}/;"
    proxy_passes = [line for line in active if re.match(r"^proxy_pass\s", line)]
    if proxy_passes != [expected_proxy]:
        raise RuntimeError("nginx block does not contain one canonical tenant proxy target")

    expected_hide = "proxy_hide_header X-Unboks-Tenant;"
    hidden_headers = [
        line
        for line in active
        if re.match(r"^proxy_hide_header\s+X-Unboks-Tenant\b", line, re.I)
    ]
    if hidden_headers != [expected_hide]:
        raise RuntimeError("nginx block must hide the upstream tenant header exactly once")

    expected_tenant_header = f'add_header X-Unboks-Tenant "{slug}" always;'
    tenant_headers = [
        line
        for line in active
        if re.match(r"^add_header\s+X-Unboks-Tenant\b", line, re.I)
    ]
    if tenant_headers != [expected_tenant_header]:
        raise RuntimeError("nginx block must set one canonical tenant identity header")

    expected_expose = 'add_header Access-Control-Expose-Headers "X-Unboks-Tenant" always;'
    expose_headers = [
        line
        for line in active
        if re.match(r"^add_header\s+Access-Control-Expose-Headers\b", line, re.I)
    ]
    if expose_headers != [expected_expose]:
        raise RuntimeError("nginx block must expose the tenant identity header exactly once")

    canonical_active = [
        line.strip()
        for line in canonical_managed_nginx_block_text(slug, host_port).splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if active != canonical_active:
        raise RuntimeError("nginx block contains noncanonical tenant route directives")


def update_client_status(tenant_dir: Path, status: str) -> None:
    client_path = tenant_dir / "config" / "client.json"
    with exclusive_client_json_lock(client_path):
        data = read_client_json_no_follow(client_path)
        business = data.get("business")
        if isinstance(business, dict) and business:
            business["status"] = status
        data["status"] = status
        atomic_write(
            client_path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )


def validate_tenant_details(raw: object) -> dict[str, str]:
    """Accept only the non-secret business fields exposed by the Nr3 form."""
    if not isinstance(raw, dict) or set(raw) != set(TENANT_DETAIL_LIMITS):
        raise RuntimeError("Tenant details contain an unsupported or missing field.")
    clean: dict[str, str] = {}
    for field, limit in TENANT_DETAIL_LIMITS.items():
        value = raw.get(field)
        if not isinstance(value, str):
            raise RuntimeError(f"Tenant detail {field!r} must be text.")
        value = value.strip()
        if any(unicodedata.category(char).startswith("C") for char in value):
            raise RuntimeError(f"Tenant detail {field!r} contains a control character.")
        if len(value) > limit:
            raise RuntimeError(f"Tenant detail {field!r} is too long.")
        clean[field] = value
    if not clean["name"]:
        raise RuntimeError("Tenant business name is required.")
    return clean


def update_tenant_details(
    tenant_dir: Path,
    slug: str,
    raw_details: object,
) -> None:
    """Patch safe business metadata while preserving all runtime secrets."""
    details = validate_tenant_details(raw_details)
    client_path = tenant_dir / "config" / "client.json"
    with exclusive_client_json_lock(client_path):
        data = read_client_json_no_follow(client_path)
        if str(data.get("slug") or "").strip() != slug:
            raise RuntimeError("client.json slug does not match the tenant action.")

        data["name"] = details["name"]
        data["contact_person"] = details["contact_person"]
        data["email"] = details["email"]
        data["phone"] = details["phone"]
        data["whatsapp"] = details["whatsapp"]
        data["website"] = details["website"]
        data["address"] = details["address"]
        data["logo_url"] = details["logo_url"]
        business = data.get("business")
        if isinstance(business, dict) and business:
            business["slug"] = slug
            business["name"] = details["name"]
            business["contact_person"] = details["contact_person"]
            business["email"] = details["email"]
            business["phone"] = details["phone"]
            business["whatsapp"] = details["whatsapp"]
            business["website"] = details["website"]
            business["address"] = details["address"]
            business["logo_url"] = details["logo_url"]
        atomic_write(
            client_path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )


def update_dashboard_password(tenant_dir: Path, slug: str, new_password: str) -> None:
    env_path = tenant_dir / "config" / "platform.env"
    client_path = tenant_dir / "config" / "client.json"
    with exclusive_client_json_lock(client_path):
        data = read_client_json_no_follow(client_path)
        env_text = read_regular_text_no_follow(
            env_path,
            description="platform.env",
        )
        assert env_text is not None
        lines = env_text.splitlines()
        data["password"] = new_password
        data["dashboard_access_key"] = new_password
        data["password_updated_at"] = utc_now()
        business = data.get("business")
        if isinstance(business, dict) and business:
            business["password_updated_at"] = data["password_updated_at"]
        atomic_write(
            client_path,
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )

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
    atomic_write(env_path, "\n".join(out).rstrip() + "\n", mode=0o600)


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
    with exclusive_client_json_lock(client_path):
        data = read_client_json_no_follow(client_path)
        # client.json is tenant-writable and may contain permissive legacy IDs.
        # The trusted Nr3 action supplies the one provider-verified account that
        # may be authorized; never merge inherited entries into that decision.
        data["channel_account_allowlist"] = {
            "mode": "strict",
            "zernio_accounts": [account_id],
            "notes": str(note or "").strip()
            or "Strict account allowlist maintained by Nr3 WhatsApp connection state.",
        }
        atomic_write(
            client_path,
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            mode=0o600,
        )


def _quick_check_sqlite(path: Path) -> None:
    try:
        with sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
        ) as db:
            rows = [str(row[0]) for row in db.execute("PRAGMA quick_check")]
    except sqlite3.Error as exc:
        raise RuntimeError(f"SQLite backup is unreadable: {path}") from exc
    if rows != ["ok"]:
        raise RuntimeError(
            f"SQLite quick_check failed for {path}: {'; '.join(rows[:5])}"
        )


def _backup_sqlite_database(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    try:
        with sqlite3.connect(source_uri, uri=True) as source_db:
            with sqlite3.connect(target) as target_db:
                source_db.backup(target_db)
                target_db.commit()
                # A source using WAL can transfer that persistent journal-mode
                # flag. Convert the standalone snapshot to DELETE mode so its
                # correctness never depends on omitted -wal/-shm sidecars.
                target_db.execute("PRAGMA journal_mode=DELETE").fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not create consistent SQLite backup: {source}") from exc
    try:
        target.chmod(0o600)
    except OSError:
        pass
    _quick_check_sqlite(target)
    _fsync_regular_file_required(target)


def _copy_recoverable_tree(
    source_root: Path,
    target_root: Path,
) -> None:
    """Copy a tree privately, using SQLite's snapshot API for every *.db."""
    if source_root.is_symlink() or not source_root.is_dir():
        raise RuntimeError(f"Backup source is not a trusted directory: {source_root}")
    target_root.mkdir(parents=True, exist_ok=False)
    target_root.chmod(0o700)
    for source in sorted(
        source_root.rglob("*"),
        key=lambda path: path.relative_to(source_root).as_posix(),
    ):
        relative = source.relative_to(source_root)
        if source.is_symlink():
            raise RuntimeError(f"Backup source contains an unsafe symlink: {source}")
        target = target_root / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            target.chmod(0o700)
            continue
        if not source.is_file():
            raise RuntimeError(f"Backup source contains an unsupported path: {source}")
        lowered_name = source.name.lower()
        if lowered_name.endswith(("-wal", "-shm", "-journal")):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.suffix.lower() == ".db":
            _backup_sqlite_database(source, target)
        else:
            shutil.copyfile(source, target)
            target.chmod(0o600)
            _fsync_regular_file_required(target)


def _validate_recoverable_backup_content(backup_root: Path) -> None:
    for path in sorted(backup_root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError(f"Delete backup contains an unsafe symlink: {path}")
        if path.is_file() and path.name.lower().endswith(("-wal", "-shm")):
            raise RuntimeError(f"Delete backup contains a SQLite sidecar: {path}")
        if path.is_file() and path.suffix.lower() == ".db":
            _quick_check_sqlite(path)
        if path.is_file() and path.suffix.lower() == ".json":
            try:
                json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise RuntimeError(f"Critical JSON backup is unreadable: {path}") from exc


def _delete_backup_inventory(backup_root: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(
        backup_root.rglob("*"),
        key=lambda item: item.relative_to(backup_root).as_posix(),
    ):
        if path.name == "DELETE_MANIFEST.json" and path.parent == backup_root:
            continue
        if path.is_symlink():
            raise RuntimeError(f"Delete backup contains an unsafe symlink: {path}")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError(f"Delete backup contains an unsupported path: {path}")
        inventory.append({
            "path": path.relative_to(backup_root).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256_file(path),
        })
    return inventory


def _inventory_digest(inventory: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        inventory,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def backup_tenant_before_delete(
    slug: str,
    tenant_dir: Path,
    *,
    delete_operation_id: str,
    generation_fingerprint: str,
    backup_role: str = "prepared",
) -> Path:
    if backup_role not in {"prepared", "defensive"}:
        raise RuntimeError("Delete backup role is invalid.")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    name = f"{slug}-{stamp}-{secrets.token_hex(3)}"
    backup_dir = DELETED_TENANTS_ROOT / name
    staging_dir = DELETED_TENANTS_ROOT / f".{name}.tmp-{secrets.token_hex(3)}"
    DELETED_TENANTS_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        DELETED_TENANTS_ROOT.chmod(0o700)
    except OSError:
        pass
    staging_dir.mkdir(parents=True, exist_ok=False)
    staging_dir.chmod(0o700)
    try:
        if tenant_dir.is_dir() and not tenant_dir.is_symlink():
            _copy_recoverable_tree(tenant_dir, staging_dir / "client")
            tenant_folder_present = True
        elif tenant_dir.exists() or tenant_dir.is_symlink():
            raise RuntimeError(f"Tenant backup path is not trusted: {tenant_dir}")
        else:
            atomic_write(
                staging_dir / "client-missing.txt",
                f"Tenant folder was already missing at delete time: {tenant_dir}\n",
                mode=0o600,
            )
            tenant_folder_present = False
        _validate_recoverable_backup_content(staging_dir)
        inventory = _delete_backup_inventory(staging_dir)
        manifest = {
            "version": 2,
            "slug": slug,
            "delete_operation_id": delete_operation_id,
            "generation_fingerprint": generation_fingerprint,
            "backup_role": backup_role,
            "created_at": utc_now(),
            "tenant_dir": str(tenant_dir),
            "tenant_folder_present": tenant_folder_present,
            "backup_dir": str(backup_dir),
            # Nr3's global database/registry contains every customer. It must
            # be backed up by the control-plane disaster-recovery process, not
            # copied into a per-tenant retention artifact.
            "recovery_scope": "tenant_runtime_only",
            "control_panel_data_included": False,
            "inventory": inventory,
            "inventory_digest": _inventory_digest(inventory),
        }
        atomic_write(
            staging_dir / "DELETE_MANIFEST.json",
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            mode=0o600,
        )
        # Content hashes prove logical correctness only. Persist every file
        # above and every directory entry here before publishing the snapshot,
        # then require the parent rename barrier before deletion can continue.
        _fsync_directory_tree_required(staging_dir)
        os.replace(staging_dir, backup_dir)
        _fsync_directory_required(DELETED_TENANTS_ROOT)
    except Exception:
        if staging_dir.exists() and not staging_dir.is_symlink():
            shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    return backup_dir


def delete_backup_digest(backup_dir: Path) -> str:
    """Hash every path and byte in a prepared backup without following links."""
    digest = hashlib.sha256()
    try:
        paths = sorted(
            backup_dir.rglob("*"),
            key=lambda path: path.relative_to(backup_dir).as_posix(),
        )
    except OSError as exc:
        raise RuntimeError(f"Delete backup cannot be enumerated: {backup_dir}") from exc
    for path in paths:
        relative = path.relative_to(backup_dir).as_posix().encode("utf-8")
        if path.is_symlink():
            raise RuntimeError(f"Delete backup contains an unsafe symlink: {path}")
        kind = b"D" if path.is_dir() else b"F" if path.is_file() else b"?"
        if kind == b"?":
            raise RuntimeError(f"Delete backup contains an unsupported path: {path}")
        digest.update(kind)
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        if kind == b"F":
            size = path.stat().st_size
            digest.update(size.to_bytes(8, "big"))
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def verify_delete_backup(
    slug: str,
    tenant_dir: Path,
    backup_dir: Path,
    *,
    delete_operation_id: str,
    generation_fingerprint: str,
    expected_digest: str = "",
    expected_role: str = "prepared",
) -> str:
    """Verify the backup structure and its manifest before provider deletion."""
    manifest_path = backup_dir / "DELETE_MANIFEST.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Delete backup manifest is unreadable: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Delete backup manifest is malformed: {manifest_path}")
    expected = {
        "version": 2,
        "slug": slug,
        "delete_operation_id": delete_operation_id,
        "generation_fingerprint": generation_fingerprint,
        "tenant_dir": str(tenant_dir),
        "backup_dir": str(backup_dir),
        "backup_role": expected_role,
        "recovery_scope": "tenant_runtime_only",
        "control_panel_data_included": False,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Delete backup manifest does not match tenant {slug}")
    if (backup_dir / "icp-data").exists() or (backup_dir / "icp-data").is_symlink():
        raise RuntimeError(
            f"Per-tenant delete backup contains global control-panel data: {backup_dir}"
        )
    if manifest.get("tenant_folder_present") is True:
        if not (backup_dir / "client").is_dir():
            raise RuntimeError(f"Delete backup client tree is missing: {backup_dir}")
        verify_live_tenant_generation(
            slug,
            backup_dir / "client",
            generation_fingerprint,
        )
    elif manifest.get("tenant_folder_present") is False:
        if not (backup_dir / "client-missing.txt").is_file():
            raise RuntimeError(f"Delete backup missing-folder marker is absent: {backup_dir}")
    else:
        raise RuntimeError(f"Delete backup manifest has invalid tenant state: {manifest_path}")
    manifest_inventory = manifest.get("inventory")
    if not isinstance(manifest_inventory, list) or not all(
        isinstance(item, dict) for item in manifest_inventory
    ):
        raise RuntimeError(f"Delete backup inventory is malformed: {manifest_path}")
    actual_inventory = _delete_backup_inventory(backup_dir)
    if manifest_inventory != actual_inventory:
        raise RuntimeError(f"Delete backup inventory changed: {backup_dir}")
    if manifest.get("inventory_digest") != _inventory_digest(actual_inventory):
        raise RuntimeError(f"Delete backup inventory digest is invalid: {backup_dir}")
    _validate_recoverable_backup_content(backup_dir)
    actual_digest = delete_backup_digest(backup_dir)
    if expected_digest and actual_digest != expected_digest:
        raise RuntimeError(f"Prepared delete backup digest changed: {backup_dir}")
    return (
        f"backup manifest and tenant snapshot verified at {backup_dir} "
        f"({actual_digest})"
    )


def validate_delete_operation_id(job: dict[str, Any]) -> str:
    operation_id = str(job.get("delete_operation_id") or "").strip()
    if re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
        raise RuntimeError("Delete operation id is missing or invalid.")
    return operation_id


def validate_generation_fingerprint(job: dict[str, Any]) -> str:
    fingerprint = str(job.get("generation_fingerprint") or "").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
        raise RuntimeError("Tenant generation fingerprint is missing or invalid.")
    return fingerprint


def tenant_generation_fingerprint(slug: str, tenant_dir: Path) -> str:
    client_path = tenant_dir / "config" / "client.json"
    try:
        data = read_client_json_no_follow(client_path)
    except RuntimeError as exc:
        raise RuntimeError(
            f"Tenant generation cannot be verified from {client_path}."
        ) from exc
    business = data.get("business")
    source = business if isinstance(business, dict) and business else data
    configured_slug = str(source.get("slug") or data.get("slug") or slug).strip()
    if configured_slug != slug:
        raise RuntimeError(f"Tenant generation identity does not match {slug}.")
    marker: dict[str, str] = {"slug": slug}
    for key in ("tenant_generation_id", "creation_id", "created_at", "access_key"):
        value = data.get(key)
        if value in (None, ""):
            value = source.get(key)
        if isinstance(value, str) and value:
            marker[key] = value
    fingerprint_source: Any = marker if len(marker) > 1 else data
    canonical = json.dumps(
        fingerprint_source,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


def verify_live_tenant_generation(
    slug: str,
    tenant_dir: Path,
    expected_fingerprint: str,
) -> None:
    if tenant_generation_fingerprint(slug, tenant_dir) != expected_fingerprint:
        raise RuntimeError(
            f"Tenant {slug} generation changed after delete preparation began."
        )


def _delete_prepare_state_path(slug: str, job_id: str) -> Path:
    key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
    return DELETED_TENANTS_ROOT / f".nr3-delete-prepare-{slug}-{key}.json"


def _load_delete_prepare_state(
    path: Path,
    *,
    job_id: str,
    slug: str,
    operation_id: str,
    generation_fingerprint: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Delete preparation recovery state is not trusted: {path}")
    state = read_json_file(path)
    expected = {
        "version": 1,
        "job_id": job_id,
        "slug": slug,
        "delete_operation_id": operation_id,
        "generation_fingerprint": generation_fingerprint,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Delete preparation recovery identity changed: {path}")
    if state.get("original_container_state") not in {
        "absent",
        "running",
        "stopped",
    }:
        raise RuntimeError(f"Delete preparation container state is invalid: {path}")
    return state


def _restore_preparation_container_state(
    slug: str,
    tenant_dir: Path,
    generation_fingerprint: str,
    original_state: str,
    details: list[str],
) -> None:
    # Recovery is itself a runtime mutation when the old container must be
    # restarted. Never apply it to a replacement generation that reused slug.
    verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
    current = exact_container_runtime_state(
        slug,
        details,
        context="delete preparation restore",
    )
    if original_state == "running" and current != "running":
        verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
        started = run(["docker", "start", f"wtyj-{slug}"], check=False)
        details.append(f"delete preparation docker start returned {started.returncode}")
        current = exact_container_runtime_state(
            slug,
            details,
            context="delete preparation restart proof",
        )
    if current != original_state:
        raise RuntimeError(
            f"Exact container state was {original_state} before backup but is {current}."
        )


def create_verified_prepared_delete_backup(
    *,
    job_id: str,
    slug: str,
    tenant_dir: Path,
    operation_id: str,
    generation_fingerprint: str,
) -> tuple[Path, str, list[str]]:
    """Quiesce the tenant, create a recoverable snapshot, then restore state."""
    details: list[str] = []
    DELETED_TENANTS_ROOT.mkdir(parents=True, exist_ok=True)
    DELETED_TENANTS_ROOT.chmod(0o700)
    state_path = _delete_prepare_state_path(slug, job_id)
    state_pattern = re.compile(
        rf"^\.nr3-delete-prepare-{re.escape(slug)}-[0-9a-f]{{24}}\.json$"
    )
    conflicts = [
        path
        for path in DELETED_TENANTS_ROOT.iterdir()
        if state_pattern.fullmatch(path.name) and path != state_path
    ]
    if conflicts:
        if len(conflicts) != 1 or conflicts[0].is_symlink():
            raise HostActionFailure(
                f"Ambiguous recoverable delete preparation exists for tenant {slug}.",
                details=details,
            )
        prior_state = read_json_file(conflicts[0])
        prior_matches = all(
            prior_state.get(key) == value
            for key, value in {
                "version": 1,
                "slug": slug,
                "delete_operation_id": operation_id,
                "generation_fingerprint": generation_fingerprint,
            }.items()
        ) and prior_state.get("original_container_state") in {
            "absent",
            "running",
            "stopped",
        }
        if not prior_matches:
            raise HostActionFailure(
                f"Another delete preparation owns a different tenant generation for {slug}.",
                details=details,
            )
        try:
            _restore_preparation_container_state(
                slug,
                tenant_dir,
                generation_fingerprint,
                str(prior_state["original_container_state"]),
                details,
            )
        except Exception as exc:
            raise HostActionFailure(
                f"Prior delete preparation could not restore tenant {slug}: {exc}",
                details=details,
            ) from exc
        conflicts[0].unlink()
        _fsync_directory(DELETED_TENANTS_ROOT)
        details.append("recovered and retired the prior delete preparation state")

    if state_path.exists() or state_path.is_symlink():
        state = _load_delete_prepare_state(
            state_path,
            job_id=job_id,
            slug=slug,
            operation_id=operation_id,
            generation_fingerprint=generation_fingerprint,
        )
    else:
        verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
        original_state = exact_container_runtime_state(
            slug,
            details,
            context="delete preparation preflight",
        )
        state = {
            "version": 1,
            "job_id": job_id,
            "slug": slug,
            "delete_operation_id": operation_id,
            "generation_fingerprint": generation_fingerprint,
            "original_container_state": original_state,
            "backup_path": "",
            "backup_digest": "",
            "created_at": utc_now(),
        }
        atomic_write(
            state_path,
            json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            mode=0o600,
        )
        _fsync_directory(DELETED_TENANTS_ROOT)

    original_state = str(state["original_container_state"])
    backup_dir: Path | None = None
    backup_digest = ""
    primary_error: Exception | None = None
    state_restored = False
    try:
        # Check before even observing Docker so a stale retry issues zero host
        # runtime commands against a replacement tenant generation.
        verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
        current_state = exact_container_runtime_state(
            slug,
            details,
            context="delete preparation quiesce",
        )
        if current_state == "running":
            verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
            stopped = run(["docker", "stop", f"wtyj-{slug}"], check=False)
            details.append(f"delete preparation docker stop returned {stopped.returncode}")
            current_state = exact_container_runtime_state(
                slug,
                details,
                context="delete preparation stop proof",
            )
        if current_state not in {"absent", "stopped"}:
            raise RuntimeError(
                f"Tenant {slug} container could not be proven quiescent for backup."
            )

        recorded_path = str(state.get("backup_path") or "")
        recorded_digest = str(state.get("backup_digest") or "")
        if recorded_path and recorded_digest:
            backup_dir = validated_prepared_backup(
                slug,
                tenant_dir,
                recorded_path,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                expected_digest=recorded_digest,
            )
            backup_digest = recorded_digest
            details.append(f"recovered verified prepared backup at {backup_dir}")
        else:
            verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
            backup_dir = backup_tenant_before_delete(
                slug,
                tenant_dir,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                backup_role="prepared",
            )
            backup_digest = delete_backup_digest(backup_dir)
            verify_delete_backup(
                slug,
                tenant_dir,
                backup_dir,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                expected_digest=backup_digest,
                expected_role="prepared",
            )
            state["backup_path"] = str(backup_dir)
            state["backup_digest"] = backup_digest
            atomic_write(
                state_path,
                json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                mode=0o600,
            )
            details.append(f"recoverable prepared backup created at {backup_dir}")
    except Exception as exc:
        primary_error = exc
    finally:
        try:
            _restore_preparation_container_state(
                slug,
                tenant_dir,
                generation_fingerprint,
                original_state,
                details,
            )
            state_restored = True
            state_path.unlink(missing_ok=True)
            _fsync_directory(DELETED_TENANTS_ROOT)
        except Exception as restore_exc:
            if primary_error is None:
                primary_error = restore_exc
            else:
                details.append(
                    f"container state restoration also failed: {str(restore_exc)[:200]}"
                )

    if primary_error is not None:
        raise HostActionFailure(
            str(primary_error),
            details=details,
            safe_to_release=False,
        ) from primary_error
    if not state_restored or backup_dir is None or not backup_digest:
        raise HostActionFailure(
            "Delete preparation did not complete its recovery proof.",
            details=details,
        )
    return backup_dir, backup_digest, details


def _delete_final_state_path(slug: str, operation_id: str) -> Path:
    # operation_id is validated as exactly 32 lowercase hex characters before
    # this helper is called, so the durable filename cannot escape the root.
    return DELETED_TENANTS_ROOT / f".nr3-delete-final-{slug}-{operation_id}.json"


def _write_delete_final_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write(
        path,
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        mode=0o600,
    )
    _fsync_directory(DELETED_TENANTS_ROOT)


def _load_delete_final_state(
    path: Path,
    *,
    slug: str,
    operation_id: str,
    generation_fingerprint: str,
    prepared_backup: Path,
    prepared_digest: str,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Final delete recovery state is not trusted: {path}")
    state = read_json_file(path)
    expected = {
        "version": 1,
        "slug": slug,
        "delete_operation_id": operation_id,
        "generation_fingerprint": generation_fingerprint,
        "prepared_backup_path": str(prepared_backup),
        "prepared_backup_digest": prepared_digest,
    }
    if any(state.get(key) != value for key, value in expected.items()):
        raise RuntimeError(f"Final delete recovery identity changed: {path}")
    if state.get("original_container_state") not in {
        "absent",
        "running",
        "stopped",
    }:
        raise RuntimeError(f"Final delete container state is invalid: {path}")
    if not isinstance(state.get("teardown_proven"), bool):
        raise RuntimeError(f"Final delete teardown state is invalid: {path}")
    backup_path = str(state.get("defensive_backup_path") or "")
    backup_digest = str(state.get("defensive_backup_digest") or "")
    if bool(backup_path) != bool(backup_digest):
        raise RuntimeError(f"Final delete backup recovery state is incomplete: {path}")
    if backup_digest and re.fullmatch(r"sha256:[0-9a-f]{64}", backup_digest) is None:
        raise RuntimeError(f"Final delete backup digest is invalid: {path}")
    if state["teardown_proven"] and not backup_path:
        raise RuntimeError(f"Final delete teardown lacks a defensive backup: {path}")
    return state


def validated_defensive_backup(
    slug: str,
    tenant_dir: Path,
    raw_path: object,
    *,
    delete_operation_id: str,
    generation_fingerprint: str,
    expected_digest: str,
) -> Path:
    root = DELETED_TENANTS_ROOT.resolve()
    candidate = Path(str(raw_path or ""))
    if candidate.is_symlink():
        raise RuntimeError("Defensive delete backup path may not be a symlink.")
    defensive = candidate.resolve()
    if defensive.parent != root or not defensive.is_dir():
        raise RuntimeError("Defensive delete backup is outside the trusted backup root.")
    verify_delete_backup(
        slug,
        tenant_dir,
        defensive,
        delete_operation_id=delete_operation_id,
        generation_fingerprint=generation_fingerprint,
        expected_digest=expected_digest,
        expected_role="defensive",
    )
    return defensive


def create_verified_defensive_delete_backup(
    *,
    job_id: str,
    slug: str,
    tenant_dir: Path,
    operation_id: str,
    generation_fingerprint: str,
    prepared_backup: Path,
    prepared_digest: str,
) -> tuple[Path, str, Path, dict[str, Any], list[str]]:
    """Quiesce immediately before teardown and durably snapshot current data.

    The state is keyed by the durable delete operation rather than a worker job
    id. A crash or a control-panel retry can therefore reuse the exact current
    snapshot instead of taking a misleading post-teardown "missing" backup.
    """
    details: list[str] = []
    DELETED_TENANTS_ROOT.mkdir(parents=True, exist_ok=True)
    DELETED_TENANTS_ROOT.chmod(0o700)
    state_path = _delete_final_state_path(slug, operation_id)
    state: dict[str, Any] | None = None
    state_pattern = re.compile(
        rf"^\.nr3-delete-final-{re.escape(slug)}-[0-9a-f]{{32}}\.json$"
    )
    conflicts = [
        path
        for path in DELETED_TENANTS_ROOT.iterdir()
        if state_pattern.fullmatch(path.name) and path != state_path
    ]
    if conflicts:
        raise HostActionFailure(
            f"Another final-delete transaction owns tenant {slug}; "
            "manual recovery is required.",
            details=details,
        )

    if state_path.exists() or state_path.is_symlink():
        state = _load_delete_final_state(
            state_path,
            slug=slug,
            operation_id=operation_id,
            generation_fingerprint=generation_fingerprint,
            prepared_backup=prepared_backup,
            prepared_digest=prepared_digest,
        )
        # A crash before the defensive snapshot was published can leave the
        # tenant stopped. First restore the prior state and retire that partial
        # transaction, then start a fresh quiesce/snapshot attempt.
        if (
            not state.get("defensive_backup_path")
            and not state.get("teardown_proven")
        ):
            try:
                _restore_preparation_container_state(
                    slug,
                    tenant_dir,
                    generation_fingerprint,
                    str(state["original_container_state"]),
                    details,
                )
            except Exception as exc:
                raise HostActionFailure(
                    f"Interrupted final delete could not restore tenant {slug}: {exc}",
                    details=details,
                ) from exc
            state_path.unlink()
            _fsync_directory(DELETED_TENANTS_ROOT)
            details.append("recovered interrupted final-delete quiesce state")
            state = None

    if state is None:
        if tenant_dir.is_symlink() or not tenant_dir.is_dir():
            raise HostActionFailure(
                f"Tenant {slug} runtime is unavailable for its final defensive backup.",
                details=details,
            )
        verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
        original_state = exact_container_runtime_state(
            slug,
            details,
            context="final delete preflight",
        )
        state = {
            "version": 1,
            "job_id": job_id,
            "slug": slug,
            "delete_operation_id": operation_id,
            "generation_fingerprint": generation_fingerprint,
            "prepared_backup_path": str(prepared_backup),
            "prepared_backup_digest": prepared_digest,
            "original_container_state": original_state,
            "defensive_backup_path": "",
            "defensive_backup_digest": "",
            "teardown_proven": False,
            "created_at": utc_now(),
        }
        _write_delete_final_state(state_path, state)

    defensive_backup: Path | None = None
    defensive_digest = ""
    try:
        if state["teardown_proven"]:
            defensive_digest = str(state["defensive_backup_digest"])
            defensive_backup = validated_defensive_backup(
                slug,
                tenant_dir,
                state["defensive_backup_path"],
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                expected_digest=defensive_digest,
            )
            details.append(
                f"recovered final delete after teardown proof with backup {defensive_backup}"
            )
            return defensive_backup, defensive_digest, state_path, state, details

        # Existing recovery state is not ownership proof for the current slug.
        # Verify its live generation before any Docker inspection or mutation.
        verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
        current_state = exact_container_runtime_state(
            slug,
            details,
            context="final delete quiesce",
        )
        if current_state == "running":
            verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
            stopped = run(["docker", "stop", f"wtyj-{slug}"], check=False)
            details.append(f"final delete docker stop returned {stopped.returncode}")
            current_state = exact_container_runtime_state(
                slug,
                details,
                context="final delete stop proof",
            )
        if current_state not in {"absent", "stopped"}:
            raise RuntimeError(
                f"Tenant {slug} container could not be proven quiescent for final backup."
            )

        recorded_path = str(state.get("defensive_backup_path") or "")
        recorded_digest = str(state.get("defensive_backup_digest") or "")
        if recorded_path and recorded_digest:
            defensive_backup = validated_defensive_backup(
                slug,
                tenant_dir,
                recorded_path,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                expected_digest=recorded_digest,
            )
            defensive_digest = recorded_digest
            details.append(f"recovered verified defensive backup at {defensive_backup}")
        else:
            if tenant_dir.is_symlink() or not tenant_dir.is_dir():
                raise RuntimeError(
                    f"Tenant {slug} runtime disappeared before its defensive backup."
                )
            verify_live_tenant_generation(slug, tenant_dir, generation_fingerprint)
            defensive_backup = backup_tenant_before_delete(
                slug,
                tenant_dir,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                backup_role="defensive",
            )
            defensive_digest = delete_backup_digest(defensive_backup)
            verify_delete_backup(
                slug,
                tenant_dir,
                defensive_backup,
                delete_operation_id=operation_id,
                generation_fingerprint=generation_fingerprint,
                expected_digest=defensive_digest,
                expected_role="defensive",
            )
            state["defensive_backup_path"] = str(defensive_backup)
            state["defensive_backup_digest"] = defensive_digest
            _write_delete_final_state(state_path, state)
            details.append(f"recoverable defensive backup created at {defensive_backup}")
    except Exception as exc:
        try:
            _restore_preparation_container_state(
                slug,
                tenant_dir,
                generation_fingerprint,
                str(state["original_container_state"]),
                details,
            )
            state_path.unlink(missing_ok=True)
            _fsync_directory(DELETED_TENANTS_ROOT)
        except Exception as restore_exc:
            details.append(
                f"final backup container restoration also failed: "
                f"{str(restore_exc)[:200]}"
            )
        raise HostActionFailure(str(exc), details=details) from exc

    if defensive_backup is None or not defensive_digest:
        raise HostActionFailure(
            "Final delete did not complete its defensive backup proof.",
            details=details,
        )
    return defensive_backup, defensive_digest, state_path, state, details


def validated_prepared_backup(
    slug: str,
    tenant_dir: Path,
    raw_path: object,
    *,
    delete_operation_id: str,
    generation_fingerprint: str,
    expected_digest: str,
) -> Path:
    root = DELETED_TENANTS_ROOT.resolve()
    candidate = Path(str(raw_path or ""))
    if candidate.is_symlink():
        raise RuntimeError("Prepared delete backup path may not be a symlink.")
    prepared = candidate.resolve()
    if prepared.parent != root or not prepared.is_dir():
        raise RuntimeError("Prepared delete backup path is outside the trusted backup root.")
    verify_delete_backup(
        slug,
        tenant_dir,
        prepared,
        delete_operation_id=delete_operation_id,
        generation_fingerprint=generation_fingerprint,
        expected_digest=expected_digest,
    )
    return prepared


def validate_delete_confirmation(job: dict[str, Any], slug: str) -> None:
    if str(job.get("typed_slug") or "") != slug:
        raise RuntimeError("Typed slug confirmation does not match tenant slug.")
    if str(job.get("final_confirmation") or "") != "DELETE FOREVER":
        raise RuntimeError("Final delete confirmation text is invalid.")


def process_prepare_delete_tenant(
    job_id: str,
    job: dict[str, Any],
    slug: str,
) -> None:
    """Create a verified backup without mutating the live tenant runtime."""
    validate_delete_confirmation(job, slug)
    operation_id = validate_delete_operation_id(job)
    generation_fingerprint = validate_generation_fingerprint(job)
    tenant_dir = CLIENTS_ROOT / slug
    backup_dir, backup_digest, preparation_details = (
        create_verified_prepared_delete_backup(
            job_id=job_id,
            slug=slug,
            tenant_dir=tenant_dir,
            operation_id=operation_id,
            generation_fingerprint=generation_fingerprint,
        )
    )
    verification = verify_delete_backup(
        slug,
        tenant_dir,
        backup_dir,
        delete_operation_id=operation_id,
        generation_fingerprint=generation_fingerprint,
        expected_digest=backup_digest,
    )
    write_result(job_id, {
        "status": "succeeded",
        "job_type": "tenant_action",
        "action": "prepare_delete_tenant",
        "slug": slug,
        "job_payload_digest": job_payload_digest(job),
        "creation_id": str(job.get("creation_id") or ""),
        "requested_job_id": job_id,
        "delete_operation_id": operation_id,
        "generation_fingerprint": generation_fingerprint,
        "message": f"Tenant {slug} backup is ready for final deletion.",
        "details": [
            *preparation_details,
            f"backup created at {backup_dir}",
            verification,
            "live tenant runtime was not changed",
        ],
        "backup_path": str(backup_dir),
        "backup_digest": backup_digest,
        "prepared_backup_path": str(backup_dir),
        "prepared_backup_digest": backup_digest,
        "safe_to_release": False,
    })


def process_delete_tenant(job_id: str, job: dict[str, Any], slug: str) -> None:
    validate_delete_confirmation(job, slug)
    operation_id = validate_delete_operation_id(job)
    generation_fingerprint = validate_generation_fingerprint(job)
    prepared_digest = str(job.get("prepared_backup_digest") or "").strip()
    if re.fullmatch(r"sha256:[0-9a-f]{64}", prepared_digest) is None:
        raise RuntimeError("Prepared delete backup digest is missing or invalid.")
    tenant_dir = CLIENTS_ROOT / slug
    details: list[str] = []
    prepared_backup = validated_prepared_backup(
        slug,
        tenant_dir,
        job.get("prepared_backup_path"),
        delete_operation_id=operation_id,
        generation_fingerprint=generation_fingerprint,
        expected_digest=prepared_digest,
    )
    details.append(f"prepared backup verified at {prepared_backup}")
    (
        defensive_backup,
        defensive_digest,
        final_state_path,
        final_state,
        defensive_details,
    ) = create_verified_defensive_delete_backup(
        job_id=job_id,
        slug=slug,
        tenant_dir=tenant_dir,
        operation_id=operation_id,
        generation_fingerprint=generation_fingerprint,
        prepared_backup=prepared_backup,
        prepared_digest=prepared_digest,
    )
    details.extend(defensive_details)

    # The old container may already have crossed the absence boundary while
    # files/routes still need retry cleanup. Never interpret a newly-created
    # tenant tree as residue from this delete operation.
    if tenant_dir.exists() or tenant_dir.is_symlink():
        if tenant_dir.is_symlink() or not tenant_dir.is_dir():
            raise HostActionFailure(
                f"Tenant runtime path is not trusted: {tenant_dir}",
                details=details,
            )
        try:
            verify_live_tenant_generation(
                slug,
                tenant_dir,
                generation_fingerprint,
            )
        except Exception as exc:
            raise HostActionFailure(str(exc), details=details) from exc

    try:
        if (tenant_dir / "docker-compose.yml").exists():
            try:
                trusted_compose_for_existing_tenant(slug, tenant_dir)
            except Exception as exc:
                details.append(
                    "skipped untrusted docker compose configuration during delete: "
                    f"{str(exc)[:200]}"
                )
            else:
                down = run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        "docker-compose.yml",
                        "down",
                        "-v",
                        "--remove-orphans",
                    ],
                    cwd=tenant_dir,
                    check=False,
                )
                details.append(f"docker compose down returned {down.returncode}")
        rm = run(["docker", "rm", "-f", f"wtyj-{slug}"], check=False)
        if rm.returncode == 0:
            details.append(f"removed container wtyj-{slug}")
        else:
            details.append(f"docker rm returned {rm.returncode}")

        if not exact_container_is_absent(slug, details, context="delete teardown"):
            raise RuntimeError(
                f"Exact container wtyj-{slug} absence could not be proven; "
                "tenant files and nginx route were retained."
            )
        # Persist the destructive boundary before removing any files. A retry
        # must never try to restart a container that has been proven removed.
        final_state["teardown_proven"] = True
        _write_delete_final_state(final_state_path, final_state)

        if tenant_dir.is_dir() and not tenant_dir.is_symlink():
            shutil.rmtree(tenant_dir)
            details.append(f"deleted tenant folder {tenant_dir}")
        elif tenant_dir.exists() or tenant_dir.is_symlink():
            raise RuntimeError(f"Tenant runtime path is not trusted: {tenant_dir}")
        else:
            details.append(f"tenant folder was already missing: {tenant_dir}")
        # The absence result can release this slug for future reuse. Persist
        # the parent directory entry before reporting that irreversible fact.
        _fsync_directory_required(CLIENTS_ROOT)
        details.append(remove_nginx_block(slug))

        if not tenant_artifacts_are_absent(
            slug,
            tenant_dir,
            details,
            context="delete proof",
        ):
            raise RuntimeError(
                f"Tenant {slug} artifact absence could not be proven after deletion."
            )
        if not remove_tenant_bridge_token(slug, details):
            raise RuntimeError(f"Tenant {slug} bridge token could not be removed.")
    except Exception as exc:
        if not final_state.get("teardown_proven"):
            try:
                _restore_preparation_container_state(
                    slug,
                    tenant_dir,
                    generation_fingerprint,
                    str(final_state["original_container_state"]),
                    details,
                )
                final_state_path.unlink(missing_ok=True)
                _fsync_directory(DELETED_TENANTS_ROOT)
                details.append("restored the pre-final-delete container state")
            except Exception as restore_exc:
                details.append(
                    "pre-final-delete container restoration also failed: "
                    f"{str(restore_exc)[:200]}"
                )
        raise HostActionFailure(str(exc), details=details) from exc

    write_result(job_id, {
        "status": "succeeded",
        "job_type": "tenant_action",
        "action": "delete_tenant",
        "slug": slug,
        "job_payload_digest": job_payload_digest(job),
        "creation_id": str(job.get("creation_id") or ""),
        "requested_job_id": job_id,
        "delete_operation_id": operation_id,
        "generation_fingerprint": generation_fingerprint,
        "message": f"Tenant {slug} was permanently deleted on the VPS.",
        "details": details,
        # The canonical backup is the recoverable snapshot taken after the
        # provider-side delay and immediately before host teardown. The
        # separately named prepared proof remains the authorization binding.
        "backup_path": str(defensive_backup),
        "backup_digest": defensive_digest,
        "prepared_backup_path": str(prepared_backup),
        "prepared_backup_digest": prepared_digest,
        "defensive_backup_path": str(defensive_backup),
        "defensive_backup_digest": defensive_digest,
        "safe_to_release": True,
    })
    try:
        final_state_path.unlink(missing_ok=True)
        _fsync_directory(DELETED_TENANTS_ROOT)
    except OSError:
        # The terminal result is the durable completion proof. Leaving a
        # correlated recovery marker is harmless and safer than rewriting a
        # successful result as a failure after all artifacts are gone.
        pass


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
    target_bridge_token: str,
    target_host_port: int,
    verified_zernio_account_id: str = "",
    target_creation_id: str = "",
) -> None:
    previous_client = read_json_file(previous_dir / "config" / "client.json")

    client_path = target_dir / "config" / "client.json"
    client = read_json_file(client_path)
    client = clear_provider_json_values(client)
    account_id = str(verified_zernio_account_id or "").strip()
    if account_id and (
        len(account_id) > 512
        or any(unicodedata.category(char).startswith("C") for char in account_id)
    ):
        raise RuntimeError("Verified Zernio account id is invalid.")
    # The old runtime client.json is tenant-writable and cannot be an authority
    # for provider identity. Rebuild the strict allowlist only from the account
    # that Nr3 verified in its own connection database and placed in this job.
    if preserve_provider_connection and account_id:
        client["channel_account_allowlist"] = {
            "mode": "strict",
            "zernio_accounts": [account_id],
            "notes": "Rebuilt from the Nr3-verified target connection during restore.",
        }
    client["slug"] = target
    client["host_port"] = target_host_port
    generation_keys = (
        "tenant_generation_id",
        "creation_id",
        "created_at",
        "access_key",
    )
    if target_creation_id:
        # A clone is a new lifecycle generation. Never let it inherit the
        # donor's immutable marker (which could authorize a delayed old job).
        for key in generation_keys:
            client.pop(key, None)
        client["creation_id"] = target_creation_id
    else:
        # A same-target restore keeps its pre-restore generation identity. The
        # uploaded archive is not authoritative for lifecycle ownership.
        for key in generation_keys:
            if previous_client.get(key) not in (None, ""):
                client[key] = previous_client[key]
            else:
                client.pop(key, None)
    if previous_client.get("port") is not None:
        client["port"] = previous_client["port"]
    else:
        client.pop("port", None)
    business = client.get("business")
    if isinstance(business, dict):
        business["slug"] = target
    client_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(
        client_path,
        json.dumps(client, indent=2, ensure_ascii=False) + "\n",
        mode=0o600,
    )

    if len(target_bridge_token) < 32:
        raise RuntimeError("Target tenant bridge token is missing or too short.")
    env_path = target_dir / "config" / "platform.env"
    env_text = read_regular_text_no_follow(
        env_path,
        description="restored platform.env",
        allow_missing=True,
    )
    lines = env_text.splitlines() if env_text is not None else []
    seen_id = False
    seen_slug = False
    seen_bridge_token = False
    seen_required_runtime_env: set[str] = set()
    out: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip()
        if key == "TENANT_ID":
            out.append(f"TENANT_ID={target}")
            seen_id = True
        elif key == "TENANT_SLUG":
            out.append(f"TENANT_SLUG={target}")
            seen_slug = True
        elif key == "NR3_INTERNAL_API_TOKEN":
            if not seen_bridge_token:
                out.append(f"NR3_INTERNAL_API_TOKEN={target_bridge_token}")
                seen_bridge_token = True
        elif key in REQUIRED_RUNTIME_ENV:
            if key not in seen_required_runtime_env:
                out.append(f"{key}={REQUIRED_RUNTIME_ENV[key]}")
                seen_required_runtime_env.add(key)
        elif key in PROVIDER_ENV_KEYS_TO_CLEAR:
            continue
        elif line.startswith("# platform.env for tenant "):
            out.append(f"# platform.env for tenant {target}")
        else:
            out.append(line)
    if not seen_id:
        out.append(f"TENANT_ID={target}")
    if not seen_slug:
        out.append(f"TENANT_SLUG={target}")
    if not seen_bridge_token:
        out.append(f"NR3_INTERNAL_API_TOKEN={target_bridge_token}")
    for key, value in REQUIRED_RUNTIME_ENV.items():
        if key not in seen_required_runtime_env:
            out.append(f"{key}={value}")
    atomic_write(env_path, "\n".join(out).rstrip() + "\n", mode=0o600)


def _fsync_directory(path: Path) -> None:
    """Best-effort persistence barrier after a directory rename."""
    try:
        _fsync_directory_required(path)
    except OSError:
        return


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_restore_package_path(raw: object) -> Path:
    candidate = Path(str(raw or ""))
    if candidate.is_symlink():
        raise RuntimeError("Backup package path may not be a symlink.")
    resolved = candidate.resolve()
    allowed_root = IMPORT_PAYLOAD_DIR.resolve()
    if allowed_root not in resolved.parents or not resolved.is_file():
        raise RuntimeError(
            "Backup package path is outside the approved import payload directory."
        )
    return resolved


def _restore_transaction_path(slug: str, job_id: str) -> Path:
    job_key = hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:24]
    return CLIENTS_ROOT / f".nr3-restore-{slug}-{job_key}"


def _restore_manifest_path(state_dir: Path) -> Path:
    return state_dir / "RESTORE_MANIFEST.json"


def _write_restore_manifest(state_dir: Path, manifest: dict[str, Any]) -> None:
    atomic_write(
        _restore_manifest_path(state_dir),
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        mode=0o600,
    )


def _read_restore_owner(target_dir: Path) -> dict[str, Any]:
    return read_json_file(target_dir / RESTORE_OWNER_MARKER)


def _restore_owner_matches(
    target_dir: Path,
    *,
    job_id: str,
    slug: str,
    package_sha256: str,
) -> bool:
    owner = _read_restore_owner(target_dir)
    return all(
        str(owner.get(key) or "") == expected
        for key, expected in {
            "job_id": job_id,
            "slug": slug,
            "package_sha256": package_sha256,
        }.items()
    )


def _new_restore_state(
    state_dir: Path,
    manifest: dict[str, Any],
) -> None:
    """Publish a restore manifest and its directory as one sibling rename."""
    state_dir.parent.mkdir(parents=True, exist_ok=True)
    staging_state = Path(
        tempfile.mkdtemp(
            prefix=f".{state_dir.name}.creating-",
            dir=state_dir.parent,
        )
    )
    try:
        staging_state.chmod(0o700)
        _write_restore_manifest(staging_state, manifest)
        os.replace(staging_state, state_dir)
        _fsync_directory(state_dir.parent)
    except Exception:
        shutil.rmtree(staging_state, ignore_errors=True)
        raise


def _load_restore_state(
    state_dir: Path,
    expected: dict[str, Any],
) -> dict[str, Any]:
    if state_dir.is_symlink() or not state_dir.is_dir():
        raise RuntimeError(f"Restore transaction state is not trusted: {state_dir}")
    manifest = read_json_file(_restore_manifest_path(state_dir))
    if not manifest:
        raise RuntimeError(f"Restore transaction manifest is unreadable: {state_dir}")
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise RuntimeError(
                f"Restore transaction identity changed for tenant {expected['slug']}."
            )
    if manifest.get("phase") not in {"prepared", "swapped", "healthy"}:
        raise RuntimeError(f"Restore transaction phase is invalid: {state_dir}")
    if not isinstance(manifest.get("token_ready"), bool):
        raise RuntimeError(f"Restore transaction token state is invalid: {state_dir}")
    if not isinstance(manifest.get("had_existing_target"), bool):
        raise RuntimeError(f"Restore transaction target state is invalid: {state_dir}")
    return manifest


def _copy_archive_runtime_without_compose(source_root: Path, staging: Path) -> None:
    source_root = source_root.resolve()

    def ignore_archive_compose(directory: str, names: list[str]) -> list[str]:
        if Path(directory).resolve() != source_root:
            return []
        return [name for name in names if name in COMPOSE_FILENAMES]

    shutil.copytree(source_root, staging, ignore=ignore_archive_compose)


def _start_restored_runtime(
    slug: str,
    tenant_dir: Path,
    host_port: int,
    details: list[str],
) -> None:
    inspect = run(["docker", "network", "inspect", "unboks-control"], check=False)
    if inspect.returncode != 0:
        run(["docker", "network", "create", "unboks-control"])
        details.append("created docker network unboks-control")
    run(
        [
            "docker",
            "compose",
            "-f",
            "docker-compose.yml",
            "up",
            "-d",
            "--force-recreate",
        ],
        cwd=tenant_dir,
    )
    details.append(f"docker compose up -d --force-recreate completed for {slug}")
    details.append(wait_for_health(host_port))


def _rollback_restore_failure(
    *,
    job_id: str,
    slug: str,
    tenant_dir: Path,
    state_dir: Path,
    manifest: dict[str, Any] | None,
    package_sha256: str,
    host_port: int,
    details: list[str],
) -> bool:
    """Recover the old runtime, or prove a new clone was fully removed."""
    if manifest is None:
        return False
    had_existing = bool(manifest.get("had_existing_target"))
    previous_dir = state_dir / "previous"
    owner_matches = _restore_owner_matches(
        tenant_dir,
        job_id=job_id,
        slug=slug,
        package_sha256=package_sha256,
    )

    if not had_existing:
        if tenant_dir.exists() and not owner_matches:
            details.append(
                "restore rollback retained an unowned tenant path; absence is unproven"
            )
            return False
        safe = rollback_failed_provision(slug, tenant_dir, details)
        if safe:
            shutil.rmtree(state_dir, ignore_errors=True)
            _fsync_directory(CLIENTS_ROOT)
        return safe

    # A failure before the swap did not alter the live target. Discard only
    # this job's staging transaction; the original runtime stays in place.
    if not previous_dir.exists() and not owner_matches:
        shutil.rmtree(state_dir, ignore_errors=True)
        _fsync_directory(CLIENTS_ROOT)
        details.append("restore failed before swapping the existing runtime")
        return False

    if not previous_dir.is_dir() or previous_dir.is_symlink():
        details.append("durable previous runtime is unavailable; rollback retained state")
        return False

    if owner_matches:
        try:
            down = run(
                [
                    "docker",
                    "compose",
                    "-f",
                    "docker-compose.yml",
                    "down",
                    "--remove-orphans",
                ],
                cwd=tenant_dir,
                check=False,
            )
            details.append(f"restore rollback compose down returned {down.returncode}")
        except Exception as exc:
            details.append(f"restore rollback compose down failed: {str(exc)[:200]}")
        try:
            removed = run(["docker", "rm", "-f", f"wtyj-{slug}"], check=False)
            details.append(f"restore rollback docker rm returned {removed.returncode}")
        except Exception as exc:
            details.append(f"restore rollback docker rm failed: {str(exc)[:200]}")
        if not exact_container_is_absent(
            slug,
            details,
            context="restore rollback teardown",
        ):
            return False
        shutil.rmtree(tenant_dir)
    elif tenant_dir.exists():
        details.append("restore rollback found an unowned current runtime; retained it")
        return False

    os.replace(previous_dir, tenant_dir)
    _fsync_directory(CLIENTS_ROOT)
    try:
        trusted = trusted_compose_for_existing_tenant(slug, tenant_dir)
        validate_canonical_docker_compose_text(slug, host_port, trusted)
        _start_restored_runtime(slug, tenant_dir, host_port, details)
    except Exception as exc:
        details.append(f"previous runtime restart failed: {str(exc)[:200]}")
        return False
    shutil.rmtree(state_dir, ignore_errors=True)
    _fsync_directory(CLIENTS_ROOT)
    details.append("durable previous runtime restored and health-checked")
    # Existing tenant identity remains reserved even after a successful rollback.
    return False


def process_restore_tenant_runtime(job_id: str, job: dict[str, Any], slug: str) -> None:
    host_port = validate_host_port(job.get("host_port"))
    package_path = _validated_restore_package_path(job.get("backup_package_path"))
    package_sha256 = _sha256_file(package_path)
    tenant_dir = CLIENTS_ROOT / slug
    state_dir = _restore_transaction_path(slug, job_id)
    creation_id = str(job.get("creation_id") or "").strip()
    generation_fingerprint = str(job.get("generation_fingerprint") or "").strip()
    if not creation_id:
        generation_fingerprint = validate_generation_fingerprint(job)
    preserve_provider_connection = bool(job.get("preserve_provider_connection", True))
    verified_account_id = str(job.get("zernio_account_id") or "").strip()
    expected_state = {
        "version": 1,
        "job_id": job_id,
        "slug": slug,
        "package_path": str(package_path.resolve()),
        "package_sha256": package_sha256,
        "host_port": host_port,
        "creation_id": creation_id,
        "generation_fingerprint": generation_fingerprint,
        "preserve_provider_connection": preserve_provider_connection,
        "verified_zernio_account_id": verified_account_id,
    }
    details: list[str] = []
    manifest: dict[str, Any] | None = None
    source_root: Path | None = None

    try:
        # A crash after the health proof may leave only the owner marker. It is
        # safe to repeat the canonical start/health check and finish the result.
        if not state_dir.exists() and _restore_owner_matches(
            tenant_dir,
            job_id=job_id,
            slug=slug,
            package_sha256=package_sha256,
        ):
            owner = _read_restore_owner(tenant_dir)
            manifest = {
                **expected_state,
                "had_existing_target": owner.get("had_existing_target") is True,
                "phase": "healthy",
                "token_ready": True,
            }
            trusted = trusted_compose_for_existing_tenant(slug, tenant_dir)
            validate_canonical_docker_compose_text(slug, host_port, trusted)
            if owner.get("had_existing_target") is False:
                insert_nginx_block(
                    slug,
                    canonical_managed_nginx_block_text(slug, host_port),
                )
                run(["systemctl", "reload", "nginx"])
            _start_restored_runtime(slug, tenant_dir, host_port, details)
            (tenant_dir / RESTORE_OWNER_MARKER).unlink()
        else:
            finalized_pattern = re.compile(
                rf"^\.nr3-restore-{re.escape(slug)}-[0-9a-f]{{24}}$"
            )
            conflicting_states = [
                path
                for path in CLIENTS_ROOT.iterdir()
                if finalized_pattern.fullmatch(path.name) and path != state_dir
            ] if CLIENTS_ROOT.is_dir() else []
            if conflicting_states:
                raise RuntimeError(
                    f"Another durable restore transaction exists for tenant {slug}."
                )

            if state_dir.exists():
                manifest = _load_restore_state(state_dir, expected_state)
            else:
                had_existing = tenant_dir.exists() or tenant_dir.is_symlink()
                if creation_id and had_existing:
                    raise RuntimeError(
                        f"Clone restore target {slug} is already occupied; nothing was changed."
                    )
                if not creation_id and not had_existing:
                    raise RuntimeError(
                        f"Existing restore target {slug} is absent; nothing was changed."
                    )
                if had_existing:
                    if tenant_dir.is_symlink() or not tenant_dir.is_dir():
                        raise RuntimeError(
                            f"Existing tenant runtime path is not trusted: {tenant_dir}"
                        )
                    verify_live_tenant_generation(
                        slug,
                        tenant_dir,
                        generation_fingerprint,
                    )
                    trusted = trusted_compose_for_existing_tenant(slug, tenant_dir)
                    validate_canonical_docker_compose_text(slug, host_port, trusted)
                elif not tenant_artifacts_are_absent(
                    slug,
                    tenant_dir,
                    details,
                    context="restore preflight",
                ):
                    raise RuntimeError(
                        f"Pre-existing tenant artifacts block restore for {slug}; "
                        "nothing was changed."
                    )
                manifest = {
                    **expected_state,
                    "had_existing_target": had_existing,
                    "phase": "prepared",
                    "token_ready": had_existing,
                    "created_at": utc_now(),
                }
                _new_restore_state(state_dir, manifest)

            if manifest["had_existing_target"]:
                target_bridge_token = read_or_create_tenant_bridge_token(slug)
            elif manifest["token_ready"]:
                target_bridge_token = read_or_create_tenant_bridge_token(slug)
            else:
                target_bridge_token = rotate_tenant_bridge_token(slug)
                manifest["token_ready"] = True
                _write_restore_manifest(state_dir, manifest)

            previous_dir = state_dir / "previous"
            owner_matches = _restore_owner_matches(
                tenant_dir,
                job_id=job_id,
                slug=slug,
                package_sha256=package_sha256,
            )
            if not owner_matches:
                source_root = extract_client_tree_from_backup(package_path)
                staging = state_dir / "staging"
                if staging.exists() or staging.is_symlink():
                    if staging.is_symlink() or not staging.is_dir():
                        raise RuntimeError("Restore staging path is not trusted.")
                    shutil.rmtree(staging)
                _copy_archive_runtime_without_compose(source_root, staging)
                trusted_compose = canonical_docker_compose_text(slug, host_port)
                atomic_write(
                    staging / "docker-compose.yml",
                    trusted_compose,
                    mode=0o600,
                )
                baseline = (
                    previous_dir
                    if previous_dir.is_dir()
                    else tenant_dir
                    if manifest["had_existing_target"]
                    else state_dir / "no-previous-runtime"
                )
                rewrite_restored_runtime_identity(
                    slug,
                    source_root,
                    staging,
                    baseline,
                    preserve_provider_connection=preserve_provider_connection,
                    target_bridge_token=target_bridge_token,
                    target_host_port=host_port,
                    verified_zernio_account_id=verified_account_id,
                    target_creation_id=creation_id,
                )
                validate_canonical_docker_compose_text(
                    slug,
                    host_port,
                    (staging / "docker-compose.yml").read_text(encoding="utf-8"),
                )
                atomic_write(
                    staging / RESTORE_OWNER_MARKER,
                    json.dumps(
                        {
                            "job_id": job_id,
                            "slug": slug,
                            "package_sha256": package_sha256,
                            "had_existing_target": manifest["had_existing_target"],
                        },
                        indent=2,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n",
                    mode=0o600,
                )

                if manifest["had_existing_target"] and not previous_dir.exists():
                    if not tenant_dir.is_dir() or tenant_dir.is_symlink():
                        raise RuntimeError("Existing runtime disappeared before restore swap.")
                    os.replace(tenant_dir, previous_dir)
                    _fsync_directory(CLIENTS_ROOT)
                if tenant_dir.exists() or tenant_dir.is_symlink():
                    raise RuntimeError("Restore target is occupied before staged swap.")
                os.replace(staging, tenant_dir)
                _fsync_directory(CLIENTS_ROOT)
                manifest["phase"] = "swapped"
                _write_restore_manifest(state_dir, manifest)

            if manifest["had_existing_target"] is False:
                insert_nginx_block(
                    slug,
                    canonical_managed_nginx_block_text(slug, host_port),
                )
                run(["systemctl", "reload", "nginx"])
                details.append("canonical nginx tenant route installed")

            _start_restored_runtime(slug, tenant_dir, host_port, details)
            manifest["phase"] = "healthy"
            _write_restore_manifest(state_dir, manifest)
            # The prior runtime is retained until the replacement health proof
            # succeeds. Only then may the durable rollback directory disappear.
            shutil.rmtree(state_dir)
            _fsync_directory(CLIENTS_ROOT)
            (tenant_dir / RESTORE_OWNER_MARKER).unlink()

        write_result(job_id, {
            "status": "succeeded",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": slug,
            "job_payload_digest": job_payload_digest(job),
            "creation_id": creation_id,
            "generation_fingerprint": generation_fingerprint,
            "message": f"Tenant {slug} runtime was restored from backup and recreated.",
            "details": [
                *details,
                f"runtime restored to {tenant_dir}",
                "provider allowlist rebuilt from verified Nr3 state: "
                f"{bool(preserve_provider_connection and verified_account_id)}",
            ],
            "dashboard_url": str(
                job.get("dashboard_url")
                or f"https://dashboard.unboks.org/{slug}"
            ),
            "health_url": f"http://127.0.0.1:{host_port}/health",
        })
    except Exception as exc:
        try:
            safe_to_release = _rollback_restore_failure(
                job_id=job_id,
                slug=slug,
                tenant_dir=tenant_dir,
                state_dir=state_dir,
                manifest=manifest,
                package_sha256=package_sha256,
                host_port=host_port,
                details=details,
            )
        except Exception as rollback_exc:
            details.append(
                f"restore rollback itself failed: {str(rollback_exc)[:200]}"
            )
            safe_to_release = False
        raise HostActionFailure(
            str(exc),
            details=details,
            safe_to_release=safe_to_release,
        ) from exc
    finally:
        if source_root is not None:
            shutil.rmtree(source_root, ignore_errors=True)


def process_tenant_action(job_id: str, job: dict[str, Any]) -> None:
    action = str(job.get("action") or "")
    slug = validate_slug(job.get("slug"))
    if slug in RESERVED_SLUGS:
        raise RuntimeError(f"Tenant {slug!r} is reserved and cannot be changed by host action.")
    if action == "prepare_delete_tenant":
        process_prepare_delete_tenant(job_id, job, slug)
        return
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
        "update_tenant_details",
    }:
        raise RuntimeError(f"Unsupported tenant action: {action!r}")

    tenant_dir = CLIENTS_ROOT / slug
    if not tenant_dir.is_dir():
        raise RuntimeError(f"Tenant directory not found: {tenant_dir}")
    details: list[str] = []
    if action in {
        "suspend_tenant",
        "unpause_tenant",
        "restart_tenant",
        "reset_dashboard_password",
        "repair_whatsapp_allowlist",
        "update_tenant_details",
    }:
        expected_generation = validate_generation_fingerprint(job)
        verify_live_tenant_generation(slug, tenant_dir, expected_generation)
        details.append(
            f"verified current tenant generation {expected_generation}"
        )
    if action == "update_tenant_details":
        update_tenant_details(tenant_dir, slug, job.get("tenant_details"))
        details.append("client.json safe tenant business details updated")
        message = f"Tenant details updated for {slug}."
    elif action == "reset_dashboard_password":
        raw_password = str(job.get("new_password") or "")
        if any(unicodedata.category(char).startswith("C") for char in raw_password):
            raise RuntimeError("New dashboard password contains a control character.")
        new_password = raw_password.strip()
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
        "job_payload_digest": job_payload_digest(job),
        "creation_id": str(job.get("creation_id") or ""),
        "generation_fingerprint": str(job.get("generation_fingerprint") or ""),
        "message": message,
        "details": details,
        "dashboard_url": dashboard_url,
    })


def matching_terminal_result_exists(processing_path: Path) -> bool:
    """Recognize only a terminal result owned by this exact queued operation."""
    job = read_json_file(processing_path)
    job_id = processing_path.stem
    if not job or str(job.get("job_id") or "") != job_id:
        return False
    result = read_json_file(RESULT_DIR / f"{job_id}.json")
    if not result or result.get("status") not in {"succeeded", "failed"}:
        return False
    if str(result.get("job_payload_digest") or "") != job_payload_digest(job):
        return False
    for field in ("job_id", "job_type", "slug"):
        if str(result.get(field) or "") != str(job.get(field) or ""):
            return False
    job_type = str(job.get("job_type") or "")
    if job_type == "tenant_provision":
        if not str(job.get("creation_id") or ""):
            return False
        if str(result.get("creation_id") or "") != str(job.get("creation_id") or ""):
            return False
        if str(result.get("signup_request_id") or "") != str(
            job.get("signup_request_id") or ""
        ):
            return False
    elif job_type == "tenant_action":
        if str(result.get("action") or "") != str(job.get("action") or ""):
            return False
        for field in (
            "creation_id",
            "delete_operation_id",
            "generation_fingerprint",
        ):
            if str(job.get(field) or "") and str(result.get(field) or "") != str(
                job.get(field) or ""
            ):
                return False
        for field in ("prepared_backup_path", "prepared_backup_digest"):
            if str(job.get(field) or "") and str(result.get(field) or "") != str(
                job.get(field) or ""
            ):
                return False
    else:
        return False
    return True


def process_job(job_path: Path) -> None:
    if job_path.suffix == ".processing":
        processing_path = job_path
    elif job_path.suffix == ".json":
        processing_path = job_path.with_suffix(".processing")
        try:
            os.replace(job_path, processing_path)
        except FileNotFoundError:
            return
    else:
        return

    # A worker may have crashed after its atomic result write but before queue
    # cleanup. Only an exactly correlated terminal result permits discarding the
    # orphan; any missing, malformed, or mismatched result is reprocessed.
    if matching_terminal_result_exists(processing_path):
        try:
            processing_path.unlink(missing_ok=True)
        except OSError:
            pass
        return

    job_id = processing_path.stem
    details: list[str] = []
    slug = ""
    job_type = ""
    action = ""
    creation_id = ""
    signup_request_id = ""
    delete_operation_id = ""
    generation_fingerprint = ""
    prepared_backup_path = ""
    prepared_backup_digest = ""
    tenant_dir: Path | None = None
    rollback_on_failure = False
    job: dict[str, Any] = {}
    try:
        job = json.loads(processing_path.read_text(encoding="utf-8"))
        if not isinstance(job, dict):
            raise RuntimeError("Provisioning job payload must be a JSON object")
        payload_job_id = str(job.get("job_id") or "").strip()
        if not payload_job_id or payload_job_id != job_id:
            raise RuntimeError(
                "Provisioning job id does not match its queue filename"
            )
        job_type = str(job.get("job_type") or "").strip()
        if job_type == "tenant_action":
            slug = validate_slug(job.get("slug"))
            action = str(job.get("action") or "").strip()
            creation_id = str(job.get("creation_id") or "").strip()
            delete_operation_id = str(job.get("delete_operation_id") or "").strip()
            generation_fingerprint = str(
                job.get("generation_fingerprint") or ""
            ).strip()
            prepared_backup_path = str(
                job.get("prepared_backup_path") or ""
            ).strip()
            prepared_backup_digest = str(
                job.get("prepared_backup_digest") or ""
            ).strip()
            process_tenant_action(job_id, job)
            try:
                processing_path.unlink(missing_ok=True)
            except OSError:
                pass
            return
        if job_type != "tenant_provision":
            raise RuntimeError("Unsupported or missing provisioning job type")
        slug = validate_slug(job.get("slug"))
        creation_id = str(job.get("creation_id") or "").strip()
        signup_request_id = str(job.get("signup_request_id") or "").strip()
        if not creation_id:
            raise RuntimeError("Provisioning job is missing its creation owner id")
        if slug in RESERVED_SLUGS:
            raise RuntimeError(f"Tenant {slug!r} is reserved and cannot be provisioned.")
        host_port = validate_host_port(job.get("host_port"))
        client_data = job.get("client_data")
        if not isinstance(client_data, dict):
            raise RuntimeError("client_data must be a JSON object")
        if client_data.get("slug") != slug:
            raise RuntimeError("client_data.slug does not match job slug")
        # Never trust a provider allowlist supplied by a provisioning caller.
        # Nr3 may add exactly the verified account only after the provider
        # callback and explicit phone selection complete.
        client_data = dict(client_data)
        client_data["creation_id"] = creation_id
        client_data["channel_account_allowlist"] = dict(
            INITIAL_CHANNEL_ACCOUNT_ALLOWLIST
        )
        password = str(client_data.get("password") or "")
        if len(password) < 8:
            raise RuntimeError("client_data.password is missing or too short")
        if any(unicodedata.category(char).startswith("C") for char in password):
            raise RuntimeError("client_data.password contains a control character")
        docker_compose_text = validate_canonical_docker_compose_text(
            slug,
            host_port,
            str(job.get("docker_compose_text") or ""),
        )
        nginx_block = str(job.get("managed_nginx_block_text") or "")
        validate_managed_nginx_block(slug, host_port, nginx_block)
        nginx_block = canonical_managed_nginx_block_text(slug, host_port)

        tenant_dir = CLIENTS_ROOT / slug
        if not tenant_artifacts_are_absent(
            slug,
            tenant_dir,
            details,
            context="provision preflight",
        ):
            raise RuntimeError(
                f"Pre-existing tenant artifacts block provisioning for {slug}; "
                "nothing was changed."
            )

        # Claim the tenant path atomically before arming rollback. A collision
        # between preflight and mkdir belongs to someone else and must not be
        # removed by this job's failure handler.
        tenant_dir.mkdir(parents=True, exist_ok=False)
        rollback_on_failure = True
        token = rotate_tenant_bridge_token(slug)

        (tenant_dir / "config").mkdir()
        (tenant_dir / "data").mkdir()
        (tenant_dir / "logs").mkdir()
        atomic_write(
            tenant_dir / "config" / "client.json",
            json.dumps(client_data, indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )
        atomic_write(
            tenant_dir / "config" / "platform.env",
            platform_env_text(slug, password, str(client_data.get("created_at") or utc_now()), token),
            mode=0o600,
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
            "job_payload_digest": job_payload_digest(job),
            "creation_id": creation_id,
            "signup_request_id": signup_request_id,
            "message": f"Tenant {slug} was provisioned on the VPS.",
            "details": details,
            "dashboard_url": dashboard_url,
            "health_url": f"http://127.0.0.1:{host_port}/health",
        })
        try:
            processing_path.unlink(missing_ok=True)
        except OSError:
            pass
    except Exception as exc:
        safe_to_release = False
        if isinstance(exc, HostActionFailure):
            details.extend(exc.details)
            safe_to_release = exc.safe_to_release
        elif rollback_on_failure and slug and tenant_dir is not None:
            try:
                safe_to_release = rollback_failed_provision(
                    slug,
                    tenant_dir,
                    details,
                )
            except Exception as rollback_exc:
                details.append(f"rollback failed: {str(rollback_exc)[:200]}")
        elif job_type == "tenant_provision" and slug:
            proof_dir = tenant_dir if tenant_dir is not None else CLIENTS_ROOT / slug
            safe_to_release = tenant_artifacts_are_absent(
                slug,
                proof_dir,
                details,
                context="failure proof",
            )
        failure_payload = {
            "status": "failed",
            "job_type": (
                job_type
                if job_type in {"tenant_provision", "tenant_action"}
                else "unknown"
            ),
            "slug": slug,
            "creation_id": creation_id,
            "signup_request_id": signup_request_id,
            "delete_operation_id": delete_operation_id,
            "generation_fingerprint": generation_fingerprint,
            "requested_job_id": job_id if job_type == "tenant_action" else "",
            "prepared_backup_path": prepared_backup_path,
            "prepared_backup_digest": prepared_backup_digest,
            "message": str(exc),
            "details": details,
            "safe_to_release": safe_to_release,
        }
        if job:
            failure_payload["job_payload_digest"] = job_payload_digest(job)
        if job_type == "tenant_action":
            failure_payload["action"] = action
        write_result(job_id, failure_payload)
        # Publish the terminal result before moving the claimed job. If the
        # process dies in this window, restart recovery can correlate and clean
        # the remaining .processing file instead of losing the operation.
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        failed_copy = FAILED_DIR / processing_path.name
        try:
            os.replace(processing_path, failed_copy)
        except OSError:
            pass


def run_forever() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    with worker_execution_lock():
        print(f"Nr3 provision worker watching {QUEUE_DIR}", flush=True)
        while True:
            for job_path in sorted(QUEUE_DIR.glob("*.processing")):
                process_job(job_path)
            for job_path in sorted(QUEUE_DIR.glob("*.json")):
                process_job(job_path)
            time.sleep(POLL_SECONDS)


def run_once() -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    with worker_execution_lock():
        for job_path in sorted(QUEUE_DIR.glob("*.processing")):
            process_job(job_path)
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
