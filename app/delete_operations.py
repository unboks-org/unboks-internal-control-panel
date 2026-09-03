"""Durable, resumable tenant-deletion transactions.

Deleting a tenant crosses three failure domains: the control-panel database,
the external channel provider, and the privileged host worker.  A request may
time out or the process may stop between any two of those steps, so the delete
intent and every completed phase must be persisted before the next side effect.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.file_lock import exclusive_file_lock


_SLUG_RE = re.compile(r"[a-z][a-z0-9_-]{1,49}")
_OPERATION_ID_RE = re.compile(r"[0-9a-f]{32}")
_GENERATION_ID_RE = re.compile(r"[A-Za-z0-9._~-]{8,180}")
_PHASES = {
    "preparing",
    "backup_queued",
    "backup_failed",
    "prepared",
    "provider_cleanup",
    "provider_cleanup_failed",
    "provider_cleaned",
    "delete_dispatching",
    "delete_queued",
    "delete_failed",
    "deleted",
}
_GENERATION_STATUSES = {"creating", "active", "retired"}


class DeleteOperationError(RuntimeError):
    """The durable delete ledger is unavailable or malformed."""


class DeleteOperationConflict(DeleteOperationError):
    """A different tenant generation or transition owns this delete."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _validate_slug(slug: str) -> str:
    value = str(slug or "").strip()
    if _SLUG_RE.fullmatch(value) is None:
        raise DeleteOperationError("Invalid tenant slug for delete operation.")
    return value


def _operations_dir() -> Path:
    configured = os.getenv("NR3_DELETE_OPERATIONS_DIR", "").strip()
    if configured:
        return Path(configured)
    queue_dir = os.getenv("NR3_PROVISION_QUEUE_DIR", "").strip()
    if queue_dir:
        return Path(queue_dir).parent / "delete-operations"
    return Path("data/provisioning/delete-operations")


def _operation_path(slug: str) -> Path:
    return _operations_dir() / f"{_validate_slug(slug)}.json"


def _lock_path(slug: str) -> Path:
    path = _operation_path(slug)
    return path.with_suffix(path.suffix + ".lock")


def _generation_path(slug: str) -> Path:
    return _operations_dir() / "generations" / f"{_validate_slug(slug)}.json"


def _history_path(slug: str, operation_id: str) -> Path:
    return _operations_dir() / "history" / f"{_validate_slug(slug)}-{operation_id}.json"


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
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


def _clean_id_list(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _job_id(slug: str, operation_id: str, action: str, attempt: int) -> str:
    suffix = "prepare" if action == "prepare_delete_tenant" else "delete"
    return f"delete-{slug}-{operation_id[:16]}-{suffix}-{attempt}"


def _validate_operation(raw: Any, *, slug: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeleteOperationError(f"Delete operation for {slug} is malformed.")
    operation = dict(raw)
    if operation.get("version") != 1 or operation.get("slug") != slug:
        raise DeleteOperationError(f"Delete operation for {slug} has invalid identity.")
    operation_id = str(operation.get("operation_id") or "")
    if _OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise DeleteOperationError(f"Delete operation for {slug} has invalid id.")
    if operation.get("phase") not in _PHASES:
        raise DeleteOperationError(f"Delete operation for {slug} has invalid phase.")
    fingerprint = str(operation.get("generation_fingerprint") or "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
        raise DeleteOperationError(
            f"Delete operation for {slug} has invalid generation proof."
        )
    tenant_generation_id = str(operation.get("tenant_generation_id") or "").strip()
    if _GENERATION_ID_RE.fullmatch(tenant_generation_id) is None:
        raise DeleteOperationError(
            f"Delete operation for {slug} has invalid tenant generation id."
        )
    for key in ("account_ids", "profile_ids", "prepare_details", "provider_details", "delete_details"):
        value = operation.get(key, [])
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise DeleteOperationError(
                f"Delete operation for {slug} has invalid {key}."
            )
        operation[key] = list(value)
    for key in ("prepare_attempt", "delete_attempt"):
        value = operation.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise DeleteOperationError(
                f"Delete operation for {slug} has invalid {key}."
            )
    for key in (
        "prepare_backup_path",
        "prepare_backup_digest",
        "delete_backup_path",
        "delete_backup_digest",
    ):
        if not isinstance(operation.get(key, ""), str):
            raise DeleteOperationError(
                f"Delete operation for {slug} has invalid {key}."
            )
    return operation


def _read_unlocked(path: Path, slug: str) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise DeleteOperationError(
            f"Delete operation ledger is unreadable for {slug}."
        ) from exc
    return _validate_operation(raw, slug=slug)


def _validate_generation_binding(raw: Any, *, slug: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise DeleteOperationError(
            f"Tenant generation binding for {slug} is malformed."
        )
    binding = dict(raw)
    generation_id = str(binding.get("generation_id") or "").strip()
    if (
        binding.get("version") != 1
        or binding.get("slug") != slug
        or _GENERATION_ID_RE.fullmatch(generation_id) is None
        or binding.get("status") not in _GENERATION_STATUSES
    ):
        raise DeleteOperationError(
            f"Tenant generation binding for {slug} has invalid identity."
        )
    return binding


def _read_generation_unlocked(slug: str) -> dict[str, Any] | None:
    path = _generation_path(slug)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise DeleteOperationError(
            f"Tenant generation binding is unreadable for {slug}."
        ) from exc
    return _validate_generation_binding(raw, slug=slug)


def _archive_completed_operation_unlocked(
    *,
    slug: str,
    operation: dict[str, Any],
) -> None:
    """Persist completed history idempotently before rotating a tombstone."""
    archive_path = _history_path(slug, str(operation["operation_id"]))
    existing = _read_unlocked(archive_path, slug)
    if existing is not None:
        if existing != operation:
            raise DeleteOperationConflict(
                "Completed delete history conflicts with the current tombstone; "
                "generation rollover was blocked."
            )
        return
    _write_private_json(archive_path, operation)


def bind_tenant_generation_for_creation(
    *,
    slug: str,
    generation_id: str,
    status: str = "creating",
) -> dict[str, Any]:
    """Bind an explicitly-created generation and rotate a completed tombstone.

    The caller must hold :func:`app.provisioning.tenant_creation_lock` for the
    slug. The completed delete record is archived before the new generation is
    opened. Writing the binding before unlinking the tombstone makes every
    possible crash point fail closed: while the tombstone remains, mutations
    are still rejected.
    """
    safe_slug = _validate_slug(slug)
    clean_generation_id = str(generation_id or "").strip()
    if _GENERATION_ID_RE.fullmatch(clean_generation_id) is None:
        raise DeleteOperationError("Invalid tenant generation id.")
    if status not in {"creating", "active"}:
        raise DeleteOperationError("Invalid new tenant generation status.")
    operation_path = _operation_path(safe_slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        operation = _read_unlocked(operation_path, safe_slug)
        if operation is not None and operation["phase"] != "deleted":
            raise DeleteOperationConflict(
                "An active delete transaction owns this tenant slug."
            )
        existing = _read_generation_unlocked(safe_slug)
        if (
            existing is not None
            and existing["status"] != "retired"
            and existing["generation_id"] != clean_generation_id
            and operation is None
        ):
            raise DeleteOperationConflict(
                "A different active tenant generation owns this slug."
            )
        if operation is not None:
            _archive_completed_operation_unlocked(
                slug=safe_slug,
                operation=operation,
            )
        now = _now()
        binding = {
            "version": 1,
            "slug": safe_slug,
            "generation_id": clean_generation_id,
            "status": status,
            "bound_at": now,
            "updated_at": now,
        }
        _write_private_json(_generation_path(safe_slug), binding)
        if operation is not None:
            operation_path.unlink()
        return binding


def activate_tenant_generation(*, slug: str, generation_id: str) -> bool:
    """Mark the exact current generation active; stale completions are ignored."""
    safe_slug = _validate_slug(slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        if _read_unlocked(_operation_path(safe_slug), safe_slug) is not None:
            return False
        binding = _read_generation_unlocked(safe_slug)
        if binding is None or binding["generation_id"] != generation_id:
            return False
        binding["status"] = "active"
        binding["updated_at"] = _now()
        _write_private_json(_generation_path(safe_slug), binding)
        return True


def retire_tenant_generation(*, slug: str) -> None:
    """Close a failed/removed generation so an absent slug cannot be mutated."""
    safe_slug = _validate_slug(slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        binding = _read_generation_unlocked(safe_slug)
        if binding is None:
            return
        binding["status"] = "retired"
        binding["updated_at"] = _now()
        _write_private_json(_generation_path(safe_slug), binding)


def require_tenant_mutation_generation(
    slug: str,
    *,
    expected_generation_id: str | None = None,
) -> str:
    """Return the current generation id or fail closed for stale mutations.

    Callers hold the per-slug lifecycle lock. Legacy tenants are assigned a
    stable binding on their first protected mutation. A delete record in any
    phase, including the completed tombstone, takes precedence over a binding.
    """
    safe_slug = _validate_slug(slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        operation = _read_unlocked(_operation_path(safe_slug), safe_slug)
        if operation is not None:
            phase = "completed" if operation["phase"] == "deleted" else "in progress"
            raise DeleteOperationConflict(
                f"Tenant deletion is {phase}; tenant mutation was blocked."
            )
        binding = _read_generation_unlocked(safe_slug)
        if binding is None:
            # Backward-compatible migration for tenants that predate generation
            # bindings. Once written, callbacks and later recreations are bound
            # to an explicit durable identity.
            now = _now()
            binding = {
                "version": 1,
                "slug": safe_slug,
                "generation_id": f"legacy-{hashlib.sha256(safe_slug.encode()).hexdigest()}",
                "status": "active",
                "bound_at": now,
                "updated_at": now,
            }
            _write_private_json(_generation_path(safe_slug), binding)
        if binding["status"] == "retired":
            raise DeleteOperationConflict(
                "Tenant generation is retired; tenant mutation was blocked."
            )
        expected = str(expected_generation_id or "").strip()
        if expected and expected != binding["generation_id"]:
            raise DeleteOperationConflict(
                "Tenant generation changed; stale mutation was blocked."
            )
        return str(binding["generation_id"])


def load_delete_operation(slug: str) -> dict[str, Any] | None:
    safe_slug = _validate_slug(slug)
    path = _operation_path(safe_slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        operation = _read_unlocked(path, safe_slug)
    return dict(operation) if operation is not None else None


def read_tenant_generation(slug: str) -> tuple[dict[str, Any], str]:
    """Read exact runtime config and derive a non-secret generation proof.

    Missing, unreadable, malformed, or identity-mismatched client.json files
    fail closed.  Immutable creation fields are preferred.  Legacy tenants
    without them use the canonical complete config, which can cause a safe
    retry conflict after any edit but can never authorize a stale deletion.
    """
    safe_slug = _validate_slug(slug)
    root_value = os.getenv("NR3_TENANTS_CLIENT_DIR", "").strip()
    if not root_value:
        raise DeleteOperationError(
            "Tenant runtime mount is not configured; deletion cannot be proven safe."
        )
    root = Path(root_value)
    client_path = root / safe_slug / "config" / "client.json"
    try:
        raw = client_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except FileNotFoundError as exc:
        raise DeleteOperationError(
            f"Tenant client.json is missing for {safe_slug}; deletion was blocked."
        ) from exc
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise DeleteOperationError(
            f"Tenant client.json is unreadable for {safe_slug}; deletion was blocked."
        ) from exc
    if not isinstance(data, dict):
        raise DeleteOperationError(
            f"Tenant client.json is not an object for {safe_slug}; deletion was blocked."
        )
    business = data.get("business")
    source = business if isinstance(business, dict) and business else data
    configured_slug = str(source.get("slug") or data.get("slug") or safe_slug).strip()
    if configured_slug != safe_slug:
        raise DeleteOperationError(
            f"Tenant client.json identity does not match {safe_slug}; deletion was blocked."
        )

    marker: dict[str, str] = {"slug": safe_slug}
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
    fingerprint = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    return data, fingerprint


def start_delete_operation(
    *,
    slug: str,
    tenant_generation_id: str,
    generation_fingerprint: str,
    account_ids: Iterable[Any],
    profile_ids: Iterable[Any],
) -> dict[str, Any]:
    """Create the durable delete intent, or resume the exact same generation."""
    safe_slug = _validate_slug(slug)
    clean_generation_id = str(tenant_generation_id or "").strip()
    if _GENERATION_ID_RE.fullmatch(clean_generation_id) is None:
        raise DeleteOperationError("Invalid tenant generation id for deletion.")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", generation_fingerprint) is None:
        raise DeleteOperationError("Invalid tenant generation fingerprint.")
    path = _operation_path(safe_slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        existing = _read_unlocked(path, safe_slug)
        if existing is not None:
            generation_changed = (
                existing["tenant_generation_id"] != clean_generation_id
                or existing["generation_fingerprint"] != generation_fingerprint
            )
            if (
                existing["phase"] == "deleted"
                and generation_changed
            ):
                _archive_completed_operation_unlocked(
                    slug=safe_slug,
                    operation=existing,
                )
                path.unlink()
                existing = None
            elif generation_changed:
                raise DeleteOperationConflict(
                    "A delete transaction exists for a different tenant generation. "
                    "No provider or runtime mutation was attempted."
                )
            elif existing["phase"] == "deleted":
                raise DeleteOperationConflict(
                    "This tenant generation was already deleted."
                )
            else:
                return existing

        operation_id = secrets.token_hex(16)
        now = _now()
        operation = {
            "version": 1,
            "operation_id": operation_id,
            "slug": safe_slug,
            "tenant_generation_id": clean_generation_id,
            "generation_fingerprint": generation_fingerprint,
            "phase": "preparing",
            "prepare_attempt": 1,
            "prepare_job_id": _job_id(
                safe_slug, operation_id, "prepare_delete_tenant", 1
            ),
            "prepare_backup_path": "",
            "prepare_backup_digest": "",
            "prepare_details": [],
            "provider_details": [],
            "delete_attempt": 1,
            "delete_job_id": _job_id(safe_slug, operation_id, "delete_tenant", 1),
            "delete_backup_path": "",
            "delete_backup_digest": "",
            "delete_details": [],
            "account_ids": _clean_id_list(account_ids),
            "profile_ids": _clean_id_list(profile_ids),
            "last_error": "",
            "created_at": now,
            "updated_at": now,
        }
        _write_private_json(path, operation)
        return operation


def update_delete_operation(
    *,
    slug: str,
    operation_id: str,
    expected_phases: Iterable[str],
    phase: str,
    **changes: Any,
) -> dict[str, Any]:
    """CAS one durable phase transition and return the updated record."""
    safe_slug = _validate_slug(slug)
    if phase not in _PHASES:
        raise DeleteOperationError(f"Invalid delete phase: {phase}")
    allowed = set(expected_phases)
    if not allowed or not allowed.issubset(_PHASES):
        raise DeleteOperationError("Invalid expected delete phases.")
    path = _operation_path(safe_slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        operation = _read_unlocked(path, safe_slug)
        if operation is None or operation.get("operation_id") != operation_id:
            raise DeleteOperationConflict(
                f"Delete operation ownership changed for {safe_slug}."
            )
        if operation["phase"] not in allowed:
            raise DeleteOperationConflict(
                f"Delete operation for {safe_slug} is already in phase "
                f"{operation['phase']}."
            )
        protected = {
            "version",
            "operation_id",
            "slug",
            "tenant_generation_id",
            "generation_fingerprint",
            "created_at",
        }
        if protected.intersection(changes):
            raise DeleteOperationError("Immutable delete identity cannot be changed.")
        operation.update(changes)
        operation["phase"] = phase
        operation["updated_at"] = _now()
        operation = _validate_operation(operation, slug=safe_slug)
        _write_private_json(path, operation)
        return operation


def retry_delete_phase(
    *,
    slug: str,
    operation_id: str,
    action: str,
) -> dict[str, Any]:
    """Issue a new deterministic worker job after a terminal failed attempt."""
    safe_slug = _validate_slug(slug)
    if action == "prepare_delete_tenant":
        failed_phase = "backup_failed"
        next_phase = "preparing"
        attempt_key = "prepare_attempt"
        job_key = "prepare_job_id"
    elif action == "delete_tenant":
        failed_phase = "delete_failed"
        next_phase = "delete_dispatching"
        attempt_key = "delete_attempt"
        job_key = "delete_job_id"
    else:
        raise DeleteOperationError(f"Unsupported delete retry action: {action}")

    path = _operation_path(safe_slug)
    with exclusive_file_lock(_lock_path(safe_slug)):
        operation = _read_unlocked(path, safe_slug)
        if operation is None or operation.get("operation_id") != operation_id:
            raise DeleteOperationConflict(
                f"Delete operation ownership changed for {safe_slug}."
            )
        if operation["phase"] != failed_phase:
            return operation
        attempt = int(operation[attempt_key]) + 1
        operation[attempt_key] = attempt
        operation[job_key] = _job_id(safe_slug, operation_id, action, attempt)
        operation["phase"] = next_phase
        operation["last_error"] = ""
        operation["updated_at"] = _now()
        _write_private_json(path, operation)
        return _validate_operation(operation, slug=safe_slug)


def delete_operation_blocks_lifecycle(slug: str) -> bool:
    """Return True while a durable delete intent owns this tenant generation."""
    operation = load_delete_operation(slug)
    return operation is not None and operation.get("phase") != "deleted"
