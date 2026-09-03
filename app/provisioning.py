"""Automatic tenant provisioning bridge.

The FastAPI app should not directly own Docker/nginx/systemctl access.
Instead it writes a strict JSON job into the shared data volume. A root
host-side systemd worker consumes that job and performs the privileged
VPS operations.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.file_lock import exclusive_file_lock


_HELD_TENANT_LIFECYCLE_LOCKS: ContextVar[frozenset[str]] = ContextVar(
    "held_tenant_lifecycle_locks",
    default=frozenset(),
)

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


@dataclass(frozen=True)
class AutoProvisionResult:
    status: str
    message: str
    job_id: str | None = None
    details: tuple[str, ...] = field(default_factory=tuple)
    dashboard_url: str = ""
    health_url: str = ""
    backup_path: str = ""
    backup_digest: str = ""
    prepared_backup_path: str = ""
    prepared_backup_digest: str = ""
    operation_id: str = ""
    generation_fingerprint: str = ""
    # A failed worker result may release the slug/port reservation only when
    # the host has positively proved that no tenant directory, container, or
    # nginx route survived rollback. Missing/legacy values fail closed.
    safe_to_release: bool = False


def auto_provision_enabled() -> bool:
    """Return whether the privileged host-worker bridge is explicitly on."""
    return os.getenv("NR3_AUTO_PROVISION", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _enabled() -> bool:
    return auto_provision_enabled()


def _path_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default).strip() or default)


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write provisioning state that can contain credentials."""
    content = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
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


@contextmanager
def tenant_creation_lock(slug: str):
    """Hold the cross-process lifecycle lock for one validated tenant slug.

    The lock is re-entrant within the current request/task context.  This lets
    a full lifecycle transaction call smaller guarded tenant mutators without
    deadlocking on a second ``flock`` descriptor, while other threads and
    processes remain serialized by the on-disk lock.
    """
    if re.fullmatch(r"[a-z][a-z0-9_-]{1,49}", slug or "") is None:
        raise ValueError("Invalid tenant slug for creation lock.")
    configured = os.getenv("NR3_TENANT_CREATE_LOCK_DIR", "").strip()
    lock_dir = (
        Path(configured)
        if configured
        else _provision_claims_path().parent / "create-locks"
    )
    lock_path = str((lock_dir / f"{slug}.lock").resolve())
    held = _HELD_TENANT_LIFECYCLE_LOCKS.get()
    if lock_path in held:
        yield
        return
    token = _HELD_TENANT_LIFECYCLE_LOCKS.set(held | {lock_path})
    try:
        with exclusive_file_lock(Path(lock_path)):
            yield
    finally:
        _HELD_TENANT_LIFECYCLE_LOCKS.reset(token)


def _provision_claims_path() -> Path:
    configured = os.getenv("NR3_PROVISION_CLAIMS_PATH", "").strip()
    if configured:
        return Path(configured)
    port_registry = os.getenv("NR3_PORT_REGISTRY_PATH", "").strip()
    if port_registry:
        return Path(port_registry).with_name("tenant_provision_claims.json")
    return Path("data/provisioning/tenant_claims.json")


def _load_provision_claims(path: Path) -> dict[str, dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(
            f"Tenant provision claims are unreadable: {path}"
        ) from exc
    claims = raw.get("claims") if isinstance(raw, dict) else None
    if not isinstance(claims, dict):
        raise RuntimeError(f"Tenant provision claims are malformed: {path}")
    return {
        str(slug): dict(claim)
        for slug, claim in claims.items()
        if isinstance(slug, str) and isinstance(claim, dict)
    }


def tenant_provision_claim(slug: str) -> dict[str, str] | None:
    path = _provision_claims_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        claim = _load_provision_claims(path).get(slug)
    return dict(claim) if claim is not None else None


def create_tenant_provision_claim(slug: str, creation_id: str) -> bool:
    path = _provision_claims_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        claims = _load_provision_claims(path)
        if slug in claims:
            return False
        claims[slug] = {
            "creation_id": creation_id,
            "job_id": "",
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        _write_private_json(path, {"claims": claims})
    return True


def update_tenant_provision_claim_job(
    slug: str, creation_id: str, job_id: str | None
) -> bool:
    path = _provision_claims_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        claims = _load_provision_claims(path)
        claim = claims.get(slug)
        if not isinstance(claim, dict) or claim.get("creation_id") != creation_id:
            return False
        claim["job_id"] = str(job_id or "")
        _write_private_json(path, {"claims": claims})
    return True


def clear_tenant_provision_claim(slug: str, creation_id: str) -> bool:
    path = _provision_claims_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        claims = _load_provision_claims(path)
        claim = claims.get(slug)
        if not isinstance(claim, dict) or claim.get("creation_id") != creation_id:
            return False
        claims.pop(slug, None)
        _write_private_json(path, {"claims": claims})
    return True


def _timeout_seconds() -> float:
    raw = os.getenv("NR3_PROVISION_TIMEOUT_SECONDS", "45").strip()
    try:
        value = float(raw)
    except ValueError:
        return 45.0
    return max(0.0, min(value, 180.0))


def _job_slug(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("slug") or "").strip()


def _job_action(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("action") or "").strip()


def _job_id(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return path.stem
    if not isinstance(data, dict):
        return path.stem
    return str(data.get("job_id") or path.stem)


def _active_job_for_slug(jobs_dir: Path, slug: str, *, action: str = "") -> tuple[str, str] | None:
    for pattern in ("*.json", "*.processing"):
        for path in sorted(jobs_dir.glob(pattern)):
            if path.name.startswith("."):
                continue
            if _job_slug(path) != slug:
                continue
            if action and _job_action(path) != action:
                continue
            return _job_id(path), path.name
    return None


def _provision_lock_path(jobs_dir: Path, slug: str) -> Path:
    return jobs_dir.parent / "locks" / f"{slug}.lock"


def _valid_job_id(job_id: str) -> bool:
    return re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{7,179}", job_id or "") is not None


def read_host_action_result(
    *,
    job_id: str,
    slug: str,
    action: str,
    delete_operation_id: str = "",
) -> AutoProvisionResult | None:
    """Read one exact correlated worker result without consuming it.

    A missing result means the operation is still pending.  Any malformed or
    mismatched result is terminally untrusted and therefore fails closed.
    """
    if not _valid_job_id(job_id):
        return AutoProvisionResult(
            status="failed",
            message="Host action job id is invalid and was not trusted.",
            job_id=job_id or None,
        )
    results_dir = _path_env("NR3_PROVISION_RESULT_DIR", "data/provisioning/results")
    result_path = results_dir / f"{job_id}.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return AutoProvisionResult(
            status="failed",
            message="Host action worker wrote an unreadable result.",
            job_id=job_id,
        )
    if not isinstance(result, dict):
        return AutoProvisionResult(
            status="failed",
            message="Host action worker wrote a malformed result.",
            job_id=job_id,
        )
    status = str(result.get("status") or "")
    identity_matches = (
        status in {"succeeded", "failed"}
        and result.get("job_type") == "tenant_action"
        and str(result.get("job_id") or "") == job_id
        and str(result.get("slug") or "") == slug
        and str(result.get("action") or "") == action
    )
    if delete_operation_id:
        identity_matches = identity_matches and (
            str(result.get("delete_operation_id") or "") == delete_operation_id
        )
    if not identity_matches:
        return AutoProvisionResult(
            status="failed",
            message=(
                "Host action result did not match the queued tenant operation "
                "and was not trusted."
            ),
            job_id=job_id,
        )
    details_raw = result.get("details")
    details = (
        tuple(str(item) for item in details_raw)
        if isinstance(details_raw, list)
        else tuple()
    )
    return AutoProvisionResult(
        status=status,
        message=str(result.get("message") or "Host action finished."),
        job_id=job_id,
        details=details,
        dashboard_url=str(result.get("dashboard_url") or ""),
        health_url=str(result.get("health_url") or ""),
        backup_path=str(result.get("backup_path") or ""),
        backup_digest=str(result.get("backup_digest") or ""),
        prepared_backup_path=str(result.get("prepared_backup_path") or ""),
        prepared_backup_digest=str(result.get("prepared_backup_digest") or ""),
        operation_id=str(result.get("delete_operation_id") or ""),
        generation_fingerprint=str(result.get("generation_fingerprint") or ""),
        safe_to_release=result.get("safe_to_release") is True,
    )


def host_action_job_is_active(*, job_id: str, slug: str, action: str) -> bool:
    """Return whether the exact worker job is queued or being processed."""
    if not _valid_job_id(job_id):
        return False
    jobs_dir = _path_env("NR3_PROVISION_QUEUE_DIR", "data/provisioning/jobs")
    for suffix in (".json", ".processing"):
        path = jobs_dir / f"{job_id}{suffix}"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            return True
        # A file at this exact unguessable id is active even if corrupt or
        # mismatched; treating it as absent could publish a colliding job.
        return True
    return False


def auto_provision_tenant(
    *,
    slug: str,
    host_port: int,
    client_data: dict[str, Any],
    docker_compose_text: str,
    managed_nginx_block_text: str,
    dashboard_url: str,
    creation_id: str = "",
    signup_request_id: str = "",
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

    with exclusive_file_lock(_provision_lock_path(jobs_dir, slug)):
        active = _active_job_for_slug(jobs_dir, slug)
        if active is not None:
            existing_job_id, filename = active
            active_path = jobs_dir / filename
            try:
                active_payload = json.loads(active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                active_payload = {}
            if not (
                isinstance(active_payload, dict)
                and active_payload.get("job_type") == "tenant_provision"
                and str(active_payload.get("creation_id") or "") == creation_id
                and creation_id
            ):
                return AutoProvisionResult(
                    status="failed",
                    message=(
                        f"A different or unreadable job already owns tenant {slug} "
                        f"({filename}); no second job was queued."
                    ),
                    job_id=existing_job_id,
                    dashboard_url=dashboard_url,
                )
            return AutoProvisionResult(
                status="queued",
                message=f"Provisioning is already active for tenant {slug} ({filename}).",
                job_id=existing_job_id,
                dashboard_url=dashboard_url,
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        job_id = f"{stamp}-{slug}-{secrets.token_hex(4)}"
        result_path = results_dir / f"{job_id}.json"
        job_path = jobs_dir / f"{job_id}.json"
        tmp_path = jobs_dir / f".{job_id}.tmp"

        payload = {
            "job_id": job_id,
            "job_type": "tenant_provision",
            "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "slug": slug,
            "host_port": host_port,
            "client_data": client_data,
            "docker_compose_text": docker_compose_text,
            "managed_nginx_block_text": managed_nginx_block_text,
            "dashboard_url": dashboard_url,
            "creation_id": creation_id,
        }
        if signup_request_id:
            payload["signup_request_id"] = signup_request_id
        _write_private_json(tmp_path, payload)
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
                    status="queued",
                    message=(
                        "Provisioning result was unreadable; tenant ownership is "
                        "retained for safe reconciliation."
                    ),
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            if not isinstance(result, dict):
                return AutoProvisionResult(
                    status="queued",
                    message=(
                        "Provisioning result was malformed; tenant ownership is "
                        "retained for safe reconciliation."
                    ),
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            status = str(result.get("status") or "")
            if not (
                status in {"succeeded", "failed"}
                and result.get("job_type") == "tenant_provision"
                and str(result.get("job_id") or "") == job_id
                and str(result.get("slug") or "") == slug
                and str(result.get("creation_id") or "") == creation_id
                and creation_id
            ):
                return AutoProvisionResult(
                    status="queued",
                    message=(
                        "Provisioning result did not match the active tenant "
                        "creation; ownership is retained for safe reconciliation."
                    ),
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            details_raw = result.get("details")
            details = tuple(str(item) for item in details_raw) if isinstance(details_raw, list) else tuple()
            return AutoProvisionResult(
                status=status,
                message=str(result.get("message") or "Provisioning finished."),
                job_id=job_id,
                details=details,
                dashboard_url=str(result.get("dashboard_url") or dashboard_url),
                health_url=str(result.get("health_url") or ""),
                safe_to_release=result.get("safe_to_release") is True,
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
    typed_slug: str = "",
    final_confirmation: str = "",
    new_password: str = "",
    backup_package_path: str = "",
    preserve_provider_connection: bool = True,
    zernio_account_id: str = "",
    allowlist_note: str = "",
    requested_job_id: str = "",
    delete_operation_id: str = "",
    generation_fingerprint: str = "",
    prepared_backup_path: str = "",
    prepared_backup_digest: str = "",
    host_port: int = 0,
    creation_id: str = "",
    tenant_details: dict[str, str] | None = None,
    before_queue: Callable[[], list[str] | tuple[str, ...] | None] | None = None,
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
    if action not in {
        "suspend_tenant",
        "unpause_tenant",
        "delete_tenant",
        "prepare_delete_tenant",
        "reset_dashboard_password",
        "restart_tenant",
        "restore_tenant_runtime",
        "repair_whatsapp_allowlist",
        "update_tenant_details",
    }:
        return AutoProvisionResult(
            status="failed",
            message=f"Unsupported host action: {action}",
            dashboard_url=dashboard_url,
        )

    jobs_dir = _path_env("NR3_PROVISION_QUEUE_DIR", "data/provisioning/jobs")
    results_dir = _path_env("NR3_PROVISION_RESULT_DIR", "data/provisioning/results")
    jobs_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)
    if requested_job_id and not _valid_job_id(requested_job_id):
        return AutoProvisionResult(
            status="failed",
            message="Requested host action job id is invalid.",
            job_id=requested_job_id,
            dashboard_url=dashboard_url,
        )
    if action in {"prepare_delete_tenant", "delete_tenant"}:
        if not delete_operation_id or not generation_fingerprint:
            return AutoProvisionResult(
                status="failed",
                message=(
                    "Tenant delete requires a durable operation id and generation proof."
                ),
                job_id=requested_job_id or None,
                dashboard_url=dashboard_url,
            )
        if action == "delete_tenant" and (
            not prepared_backup_path or not prepared_backup_digest
        ):
            return AutoProvisionResult(
                status="failed",
                message="Final tenant delete requires the verified prepared backup proof.",
                job_id=requested_job_id or None,
                dashboard_url=dashboard_url,
            )
    if action == "restore_tenant_runtime":
        if (
            isinstance(host_port, bool)
            or not isinstance(host_port, int)
            or not 1024 <= host_port <= 65535
        ):
            return AutoProvisionResult(
                status="failed",
                message="Runtime restore requires the target tenant's reserved host port.",
                job_id=requested_job_id or None,
                dashboard_url=dashboard_url,
            )
    if action == "update_tenant_details":
        if (
            not isinstance(tenant_details, dict)
            or set(tenant_details) != set(TENANT_DETAIL_LIMITS)
            or not str(tenant_details.get("name") or "").strip()
            or any(not isinstance(value, str) for value in tenant_details.values())
        ):
            return AutoProvisionResult(
                status="failed",
                message="Tenant details host action requires the exact safe field set.",
                job_id=requested_job_id or None,
                dashboard_url=dashboard_url,
            )
        normalized_details: dict[str, str] = {}
        for field, limit in TENANT_DETAIL_LIMITS.items():
            value = tenant_details[field].strip()
            if (
                len(value) > limit
                or any(
                    unicodedata.category(char).startswith("C")
                    for char in value
                )
            ):
                return AutoProvisionResult(
                    status="failed",
                    message=f"Tenant detail {field!r} is invalid.",
                    job_id=requested_job_id or None,
                    dashboard_url=dashboard_url,
                )
            normalized_details[field] = value
        tenant_details = normalized_details
    # Serialize every lifecycle mutation for one tenant. Per-action locks would
    # let (for example) a restart and an allowlist repair both pass the active
    # job check and race on the same runtime files.
    with tenant_creation_lock(slug), exclusive_file_lock(
        _provision_lock_path(jobs_dir, slug)
    ):
        try:
            from app.delete_operations import load_delete_operation

            delete_operation = load_delete_operation(slug)
        except Exception as exc:
            return AutoProvisionResult(
                status="failed",
                message=f"Tenant lifecycle ledger is unavailable: {exc}",
                job_id=requested_job_id or None,
                dashboard_url=dashboard_url,
            )
        if delete_operation is not None and delete_operation.get("phase") != "deleted":
            owns_delete = (
                action in {"prepare_delete_tenant", "delete_tenant"}
                and delete_operation.get("operation_id") == delete_operation_id
                and delete_operation.get("generation_fingerprint")
                == generation_fingerprint
            )
            if not owns_delete:
                return AutoProvisionResult(
                    status="failed",
                    message=(
                        f"Tenant {slug} has an active delete transaction; "
                        f"{action} was not queued."
                    ),
                    job_id=requested_job_id or None,
                    dashboard_url=dashboard_url,
                )
        generation_guarded_action = action in {
            "suspend_tenant",
            "unpause_tenant",
            "restart_tenant",
            "reset_dashboard_password",
            "repair_whatsapp_allowlist",
            "update_tenant_details",
        } or (action == "restore_tenant_runtime" and not creation_id)
        if generation_guarded_action:
            try:
                from app.delete_operations import read_tenant_generation

                _, current_generation_fingerprint = read_tenant_generation(slug)
            except Exception as exc:
                return AutoProvisionResult(
                    status="failed",
                    message=(
                        "Current tenant generation could not be proved; host "
                        f"action was not queued: {exc}"
                    ),
                    job_id=requested_job_id or None,
                    dashboard_url=dashboard_url,
                )
            if (
                generation_fingerprint
                and generation_fingerprint != current_generation_fingerprint
            ):
                return AutoProvisionResult(
                    status="failed",
                    message="Tenant generation changed before host action queueing.",
                    job_id=requested_job_id or None,
                    dashboard_url=dashboard_url,
                )
            generation_fingerprint = current_generation_fingerprint
        claim = tenant_provision_claim(slug)
        matching_restore_claim = bool(
            claim
            and action in {"restore_tenant_runtime", "restart_tenant"}
            and creation_id
            and claim.get("creation_id") == creation_id
        )
        if claim is not None and not matching_restore_claim:
            return AutoProvisionResult(
                status="failed",
                message=(
                    f"Tenant {slug} has an active creation reservation; "
                    f"{action} was not queued."
                ),
                job_id=str(claim.get("job_id") or "") or None,
                dashboard_url=dashboard_url,
            )
        active = _active_job_for_slug(jobs_dir, slug)
        if active is not None:
            existing_job_id, filename = active
            active_path = jobs_dir / filename
            try:
                active_payload = json.loads(active_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
                active_payload = {}
            expected_fields: dict[str, Any] = {
                "job_type": "tenant_action",
                "action": action,
                "slug": slug,
            }
            if generation_fingerprint:
                expected_fields["generation_fingerprint"] = generation_fingerprint
            if action in {"delete_tenant", "prepare_delete_tenant"}:
                expected_fields.update({
                    "typed_slug": typed_slug,
                    "final_confirmation": final_confirmation,
                    "delete_operation_id": delete_operation_id,
                    "generation_fingerprint": generation_fingerprint,
                })
                if action == "delete_tenant":
                    expected_fields.update({
                        "prepared_backup_path": prepared_backup_path,
                        "prepared_backup_digest": prepared_backup_digest,
                    })
            elif action == "reset_dashboard_password":
                expected_fields["new_password"] = new_password
            elif action == "restore_tenant_runtime":
                expected_fields.update({
                    "backup_package_path": backup_package_path,
                    "preserve_provider_connection": bool(
                        preserve_provider_connection
                    ),
                    "host_port": host_port,
                    "creation_id": creation_id,
                    "zernio_account_id": zernio_account_id,
                })
            elif action == "repair_whatsapp_allowlist":
                expected_fields.update({
                    "zernio_account_id": zernio_account_id,
                    "allowlist_note": allowlist_note,
                })
            elif action == "update_tenant_details":
                expected_fields["tenant_details"] = tenant_details
            same_request = isinstance(active_payload, dict) and all(
                active_payload.get(key) == value
                for key, value in expected_fields.items()
            )
            if not same_request:
                return AutoProvisionResult(
                    status="failed",
                    message=(
                        f"A different host job already owns tenant {slug} "
                        f"({filename}); {action} was not queued."
                    ),
                    job_id=existing_job_id,
                    dashboard_url=dashboard_url,
                )
            return AutoProvisionResult(
                status="queued",
                message=(
                    f"The same host action is already active for tenant {slug} "
                    f"({filename})."
                ),
                job_id=existing_job_id,
                dashboard_url=dashboard_url,
            )
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        job_id = requested_job_id or f"{stamp}-{slug}-{action}-{secrets.token_hex(4)}"
        job_path = jobs_dir / f"{job_id}.json"
        tmp_path = jobs_dir / f".{job_id}.tmp"
        result_path = results_dir / f"{job_id}.json"

        if requested_job_id:
            existing_result = read_host_action_result(
                job_id=job_id,
                slug=slug,
                action=action,
                delete_operation_id=delete_operation_id,
            )
            if existing_result is not None:
                return existing_result

        payload = {
            "job_id": job_id,
            "job_type": "tenant_action",
            "action": action,
            "requested_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "slug": slug,
            "dashboard_url": dashboard_url,
        }
        if typed_slug:
            payload["typed_slug"] = typed_slug
        if final_confirmation:
            payload["final_confirmation"] = final_confirmation
        if new_password:
            payload["new_password"] = new_password
        if backup_package_path:
            payload["backup_package_path"] = backup_package_path
        if zernio_account_id:
            payload["zernio_account_id"] = zernio_account_id
        if allowlist_note:
            payload["allowlist_note"] = allowlist_note
        if delete_operation_id:
            payload["delete_operation_id"] = delete_operation_id
        if generation_fingerprint:
            payload["generation_fingerprint"] = generation_fingerprint
        if prepared_backup_path:
            payload["prepared_backup_path"] = prepared_backup_path
        if prepared_backup_digest:
            payload["prepared_backup_digest"] = prepared_backup_digest
        if host_port:
            payload["host_port"] = host_port
        if creation_id:
            payload["creation_id"] = creation_id
        if tenant_details is not None:
            payload["tenant_details"] = dict(tenant_details)
        payload["preserve_provider_connection"] = bool(preserve_provider_connection)
        _write_private_json(tmp_path, payload)
        pre_queue_details: tuple[str, ...] = tuple()
        try:
            if before_queue is not None:
                callback_details = before_queue()
                if callback_details:
                    pre_queue_details = tuple(str(item) for item in callback_details)
            # Publish only after the protected pre-queue operation succeeds.
            # The worker ignores dot-prefixed temporary files.
            os.replace(tmp_path, job_path)
            jobs_fd = os.open(jobs_dir, os.O_RDONLY)
            try:
                os.fsync(jobs_fd)
            finally:
                os.close(jobs_fd)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    timeout = _timeout_seconds()
    if timeout <= 0:
        return AutoProvisionResult(
            status="queued",
            message="Host action queued; worker result was not awaited.",
            job_id=job_id,
            details=pre_queue_details,
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
            if not isinstance(result, dict):
                return AutoProvisionResult(
                    status="failed",
                    message="Host action worker wrote a malformed result.",
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            status = str(result.get("status") or "")
            if not (
                status in {"succeeded", "failed"}
                and result.get("job_type") == "tenant_action"
                and str(result.get("job_id") or "") == job_id
                and str(result.get("slug") or "") == slug
                and str(result.get("action") or "") == action
                and (
                    not delete_operation_id
                    or str(result.get("delete_operation_id") or "")
                    == delete_operation_id
                )
                and (
                    not generation_fingerprint
                    or str(result.get("generation_fingerprint") or "")
                    == generation_fingerprint
                )
            ):
                return AutoProvisionResult(
                    status="failed",
                    message=(
                        "Host action result did not match the queued tenant "
                        "operation and was not trusted."
                    ),
                    job_id=job_id,
                    dashboard_url=dashboard_url,
                )
            details_raw = result.get("details")
            worker_details = (
                tuple(str(item) for item in details_raw)
                if isinstance(details_raw, list)
                else tuple()
            )
            return AutoProvisionResult(
                status=status,
                message=str(result.get("message") or "Host action finished."),
                job_id=job_id,
                details=pre_queue_details + worker_details,
                dashboard_url=str(result.get("dashboard_url") or dashboard_url),
                health_url=str(result.get("health_url") or ""),
                backup_path=str(result.get("backup_path") or ""),
                backup_digest=str(result.get("backup_digest") or ""),
                prepared_backup_path=str(result.get("prepared_backup_path") or ""),
                prepared_backup_digest=str(result.get("prepared_backup_digest") or ""),
                operation_id=str(result.get("delete_operation_id") or ""),
                generation_fingerprint=str(
                    result.get("generation_fingerprint") or ""
                ),
                safe_to_release=result.get("safe_to_release") is True,
            )
        time.sleep(1.0)

    return AutoProvisionResult(
        status="queued",
        message="Host action queued, but the worker did not finish before the UI timeout.",
        job_id=job_id,
        details=pre_queue_details,
        dashboard_url=dashboard_url,
    )


def reconcile_host_action_results() -> int:
    """Reconcile terminal host results that finished after an HTTP timeout."""
    results_dir = _path_env("NR3_PROVISION_RESULT_DIR", "data/provisioning/results")
    marker_dir = _path_env("NR3_PROVISION_RECONCILED_DIR", "data/provisioning/reconciled")
    if not results_dir.exists():
        return 0
    marker_dir.mkdir(parents=True, exist_ok=True)
    reconciled = 0
    for result_path in sorted(results_dir.glob("*.json")):
        marker_path = marker_dir / f"{result_path.stem}.done"
        if marker_path.exists():
            continue
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        if (
            result.get("job_type") == "tenant_provision"
            and result.get("status") in {"succeeded", "failed"}
        ):
            slug = str(result.get("slug") or "").strip()
            creation_id = str(result.get("creation_id") or "").strip()
            job_id = str(result.get("job_id") or "").strip()
            signup_request_id = str(result.get("signup_request_id") or "").strip()
            if not job_id or job_id != result_path.stem:
                continue
            try:
                if slug and creation_id:
                    with tenant_creation_lock(slug):
                        claim = tenant_provision_claim(slug)
                        claim_job_id = str((claim or {}).get("job_id") or "")
                        if (
                            claim
                            and claim.get("creation_id") == creation_id
                            and not claim_job_id
                        ):
                            # A fast worker can finish before the request thread
                            # binds the freshly-created job id. The unguessable
                            # creation id safely owns that one-time binding.
                            update_tenant_provision_claim_job(
                                slug, creation_id, job_id
                            )
                            claim_job_id = job_id
                        owns_current_creation = bool(
                            claim
                            and claim.get("creation_id") == creation_id
                            and claim_job_id == job_id
                        )
                        if owns_current_creation and signup_request_id:
                            from app.config import get_settings
                            from app.public_signup_requests import (
                                reconcile_signup_provisioning_result,
                            )

                            if not reconcile_signup_provisioning_result(
                                signup_request_id,
                                slug=slug,
                                creation_id=creation_id,
                                job_id=job_id,
                                status=str(result.get("status")),
                                message=str(result.get("message") or ""),
                                settings=get_settings(),
                            ):
                                continue
                        if owns_current_creation:
                            if result.get("status") == "failed":
                                if result.get("safe_to_release") is True:
                                    if clear_tenant_provision_claim(slug, creation_id):
                                        from app.tenants import forget_tenant_state

                                        forget_tenant_state(slug)
                            else:
                                from app.delete_operations import activate_tenant_generation

                                activate_tenant_generation(
                                    slug=slug,
                                    generation_id=creation_id,
                                )
                                clear_tenant_provision_claim(slug, creation_id)
                marker_path.write_text(utc_marker(), encoding="utf-8")
                reconciled += 1
            except Exception:
                continue
            continue
        if (
            result.get("job_type") == "tenant_action"
            and result.get("action") in {"restore_tenant_runtime", "restart_tenant"}
            and result.get("status") in {"succeeded", "failed"}
        ):
            slug = str(result.get("slug") or "").strip()
            creation_id = str(result.get("creation_id") or "").strip()
            job_id = str(result.get("job_id") or "").strip()
            if not slug or not creation_id or not job_id or job_id != result_path.stem:
                continue
            try:
                with tenant_creation_lock(slug):
                    claim = tenant_provision_claim(slug)
                    claim_job_id = str((claim or {}).get("job_id") or "")
                    if (
                        claim
                        and claim.get("creation_id") == creation_id
                        and not claim_job_id
                    ):
                        update_tenant_provision_claim_job(
                            slug, creation_id, job_id
                        )
                        claim_job_id = job_id
                    owns_current_creation = bool(
                        claim
                        and claim.get("creation_id") == creation_id
                        and claim_job_id == job_id
                    )
                    if owns_current_creation and result.get("status") == "succeeded":
                        from app.delete_operations import activate_tenant_generation

                        activate_tenant_generation(
                            slug=slug,
                            generation_id=creation_id,
                        )
                        clear_tenant_provision_claim(slug, creation_id)
                    elif (
                        owns_current_creation
                        and result.get("status") == "failed"
                        and result.get("safe_to_release") is True
                    ):
                        if clear_tenant_provision_claim(slug, creation_id):
                            from app.tenants import forget_tenant_state

                            forget_tenant_state(slug)
                marker_path.write_text(utc_marker(), encoding="utf-8")
                reconciled += 1
            except Exception:
                continue
            continue
        if result.get("status") != "succeeded":
            continue
        if result.get("job_type") != "tenant_action":
            continue
        if result.get("action") != "delete_tenant":
            continue
        slug = str(result.get("slug") or "").strip()
        job_id = str(result.get("job_id") or "").strip()
        operation_id = str(result.get("delete_operation_id") or "").strip()
        generation_fingerprint = str(
            result.get("generation_fingerprint") or ""
        ).strip()
        if (
            not slug
            or not job_id
            or job_id != result_path.stem
            or not operation_id
            or result.get("safe_to_release") is not True
        ):
            continue
        try:
            from app import audit_log
            from app.delete_operations import (
                load_delete_operation,
                update_delete_operation,
            )
            from app.tenants import forget_tenant_state_strict

            operation = load_delete_operation(slug)
            operation_matches = bool(
                operation
                and operation.get("phase") == "delete_queued"
                and operation.get("operation_id") == operation_id
                and operation.get("delete_job_id") == job_id
                and operation.get("generation_fingerprint")
                == generation_fingerprint
                and operation.get("prepare_backup_path")
                == str(result.get("prepared_backup_path") or "")
                and operation.get("prepare_backup_digest")
                == str(result.get("prepared_backup_digest") or "")
                and bool(str(result.get("backup_path") or ""))
                and bool(str(result.get("backup_digest") or ""))
            )
            if not operation_matches or host_action_job_is_active(
                job_id=job_id,
                slug=slug,
                action="delete_tenant",
            ):
                continue
            with tenant_creation_lock(slug):
                operation = load_delete_operation(slug)
                operation_matches = bool(
                    operation
                    and operation.get("phase") == "delete_queued"
                    and operation.get("operation_id") == operation_id
                    and operation.get("delete_job_id") == job_id
                    and operation.get("generation_fingerprint")
                    == generation_fingerprint
                    and operation.get("prepare_backup_path")
                    == str(result.get("prepared_backup_path") or "")
                    and operation.get("prepare_backup_digest")
                    == str(result.get("prepared_backup_digest") or "")
                )
                client_root_raw = os.getenv("NR3_TENANTS_CLIENT_DIR", "").strip()
                client_root = Path(client_root_raw) if client_root_raw else None
                runtime_absent = bool(
                    client_root
                    and client_root.is_dir()
                    and not os.path.lexists(client_root / slug)
                )
                no_new_creation = tenant_provision_claim(slug) is None
                safe_to_forget = (
                    operation_matches and runtime_absent and no_new_creation
                )
                if safe_to_forget:
                    forget_tenant_state_strict(slug)
                    runtime_absent = bool(
                        client_root
                        and client_root.is_dir()
                        and not os.path.lexists(client_root / slug)
                    )
                    if not runtime_absent:
                        continue
                    update_delete_operation(
                        slug=slug,
                        operation_id=operation_id,
                        expected_phases={"delete_queued"},
                        phase="deleted",
                        delete_details=(
                            list(result.get("details"))
                            if isinstance(result.get("details"), list)
                            else []
                        ),
                        delete_backup_path=str(result.get("backup_path") or ""),
                        delete_backup_digest=str(result.get("backup_digest") or ""),
                        last_error="",
                    )
            if safe_to_forget:
                audit_log.record_event(
                    tenant_id=slug,
                    action="tenant.delete_reconciled",
                    result="ok",
                    safe_summary="Async host delete result reconciled into Nr3 state.",
                    metadata={"job_id": result_path.stem},
                )
                marker_path.write_text(utc_marker(), encoding="utf-8")
                reconciled += 1
            else:
                # A stale result must never erase a recreated tenant. Absence
                # from the read-only host runtime mount is the final proof that
                # the worker deleted this generation.
                audit_log.record_event(
                    tenant_id=slug,
                    action="tenant.delete_reconcile_skipped",
                    result="blocked",
                    safe_summary=(
                        "Async delete cleanup skipped because runtime absence "
                        "could not be proven."
                    ),
                    metadata={"job_id": result_path.stem},
                )
        except Exception as exc:
            try:
                from app import audit_log

                audit_log.record_event(
                    tenant_id=slug,
                    action="tenant.delete_local_reconcile_failed",
                    result="failed",
                    safe_summary=(
                        "Async host deletion succeeded, but strict local-state "
                        "reconciliation failed and will be retried."
                    ),
                    metadata={
                        "job_id": result_path.stem,
                        "error_type": type(exc).__name__,
                    },
                )
            except Exception:
                pass
            continue
    return reconciled


def utc_marker() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat() + "\n"
