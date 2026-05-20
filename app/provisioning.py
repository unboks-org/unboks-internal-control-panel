"""Automatic tenant provisioning bridge.

The FastAPI app should not directly own Docker/nginx/systemctl access.
Instead it writes a strict JSON job into the shared data volume. A root
host-side systemd worker consumes that job and performs the privileged
VPS operations.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AutoProvisionResult:
    status: str
    message: str
    job_id: str | None = None
    details: tuple[str, ...] = field(default_factory=tuple)
    dashboard_url: str = ""
    health_url: str = ""


def _enabled() -> bool:
    return os.getenv("NR3_AUTO_PROVISION", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default).strip() or default)


def _timeout_seconds() -> float:
    raw = os.getenv("NR3_PROVISION_TIMEOUT_SECONDS", "45").strip()
    try:
        value = float(raw)
    except ValueError:
        return 45.0
    return max(0.0, min(value, 180.0))


def auto_provision_tenant(
    *,
    slug: str,
    host_port: int,
    client_data: dict[str, Any],
    docker_compose_text: str,
    managed_nginx_block_text: str,
    dashboard_url: str,
) -> AutoProvisionResult:
    """Queue and optionally wait for privileged VPS provisioning.

    Disabled by default for local development/tests. On the VPS the
    systemd worker should be running and the queue/result directories
    must live inside the shared ./data volume.
    """
    if not _enabled():
        return AutoProvisionResult(
            status="disabled",
            message="Automatic VPS provisioning is disabled; use the manual fallback script.",
            dashboard_url=dashboard_url,
        )

    jobs_dir = _path_env("NR3_PROVISION_QUEUE_DIR", "data/provisioning/jobs")
    results_dir = _path_env("NR3_PROVISION_RESULT_DIR", "data/provisioning/results")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    job_id = f"{stamp}-{slug}-{secrets.token_hex(4)}"
    result_path = results_dir / f"{job_id}.json"
    job_path = jobs_dir / f"{job_id}.json"
    tmp_path = jobs_dir / f".{job_id}.tmp"

    payload = {
        "job_id": job_id,
        "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "slug": slug,
        "host_port": host_port,
        "client_data": client_data,
        "docker_compose_text": docker_compose_text,
        "managed_nginx_block_text": managed_nginx_block_text,
        "dashboard_url": dashboard_url,
    }
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, job_path)

    timeout = _timeout_seconds()
    if timeout <= 0:
        return AutoProvisionResult(
            status="queued",
            message="Provisioning job queued; worker result was not awaited.",
            job_id=job_id,
            dashboard_url=dashboard_url,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return AutoProvisionResult(
                    status="failed",
                    message="Provisioning worker wrote an unreadable result.",
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            status = str(result.get("status") or "failed")
            details_raw = result.get("details")
            details = tuple(str(item) for item in details_raw) if isinstance(details_raw, list) else tuple()
            return AutoProvisionResult(
                status=status,
                message=str(result.get("message") or "Provisioning finished."),
                job_id=job_id,
                details=details,
                dashboard_url=str(result.get("dashboard_url") or dashboard_url),
                health_url=str(result.get("health_url") or ""),
            )
        time.sleep(1.0)

    return AutoProvisionResult(
        status="queued",
        message="Provisioning job queued, but the worker did not finish before the UI timeout.",
        job_id=job_id,
        dashboard_url=dashboard_url,
    )


def queue_tenant_host_action(
    *,
    slug: str,
    action: str,
    dashboard_url: str = "",
) -> AutoProvisionResult:
    """Queue a privileged host action such as suspending a tenant.

    The web app still performs immediate bridge-state changes itself;
    this queues the Docker/client.json host operation for the root
    worker.
    """
    if not _enabled():
        return AutoProvisionResult(
            status="disabled",
            message="Host action worker is disabled.",
            dashboard_url=dashboard_url,
        )
    if action not in {"suspend_tenant"}:
        return AutoProvisionResult(
            status="failed",
            message=f"Unsupported host action: {action}",
            dashboard_url=dashboard_url,
        )

    jobs_dir = _path_env("NR3_PROVISION_QUEUE_DIR", "data/provisioning/jobs")
    results_dir = _path_env("NR3_PROVISION_RESULT_DIR", "data/provisioning/results")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    job_id = f"{stamp}-{slug}-{action}-{secrets.token_hex(4)}"
    job_path = jobs_dir / f"{job_id}.json"
    tmp_path = jobs_dir / f".{job_id}.tmp"
    result_path = results_dir / f"{job_id}.json"

    payload = {
        "job_id": job_id,
        "job_type": "tenant_action",
        "action": action,
        "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "slug": slug,
        "dashboard_url": dashboard_url,
    }
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, job_path)

    timeout = _timeout_seconds()
    if timeout <= 0:
        return AutoProvisionResult(
            status="queued",
            message="Host action queued; worker result was not awaited.",
            job_id=job_id,
            dashboard_url=dashboard_url,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result_path.exists():
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return AutoProvisionResult(
                    status="failed",
                    message="Host action worker wrote an unreadable result.",
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            details_raw = result.get("details")
            details = tuple(str(item) for item in details_raw) if isinstance(details_raw, list) else tuple()
            return AutoProvisionResult(
                status=str(result.get("status") or "failed"),
                message=str(result.get("message") or "Host action finished."),
                job_id=job_id,
                details=details,
                dashboard_url=str(result.get("dashboard_url") or dashboard_url),
                health_url=str(result.get("health_url") or ""),
            )
        time.sleep(1.0)

    return AutoProvisionResult(
        status="queued",
        message="Host action queued, but the worker did not finish before the UI timeout.",
        job_id=job_id,
        dashboard_url=dashboard_url,
    )
