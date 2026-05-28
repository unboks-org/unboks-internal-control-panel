"""Tenant export/import package builder for Nr3.

This module intentionally treats provider credentials as non-portable.
Exports include safe metadata and explicitly mark secrets/channels as
requiring reconnect instead of serializing raw tokens or passwords.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import audit_log, channel_connections, channel_state, icp_overrides
from app.config import get_settings
from app.tenants import (
    get_tenant,
    get_tenant_client_data,
    register_tenant,
    tenant_account_details,
    validate_slug,
)


EXPORT_VERSION = "nr3-tenant-backup-v1"
REQUIRED_FILES = {
    "manifest.json",
    "tenant.json",
    "prompts.json",
    "channels.json",
    "settings.json",
    "learning.json",
    "README_RESTORE.txt",
    "checksums.json",
}
SECRET_MARKER = "[excluded: secret or credential]"
SECRET_KEY_PARTS = (
    "password",
    "secret",
    "token",
    "credential",
    "api_key",
    "apikey",
    "access_key",
    "private_key",
    "auth_url",
    "state_token",
    "smtp",
)


@dataclass(frozen=True)
class ExportResult:
    job_id: str
    tenant_id: str
    zip_path: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    errors: list[str]
    warnings: list[str]
    summary: dict[str, Any]
    package_path: Path | None = None


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    mode: str
    target_tenant_id: str
    rollback_path: Path | None
    summary: dict[str, Any]


def export_summary(tenant_id: str) -> dict[str, Any]:
    tenant = _require_tenant(tenant_id)
    client_data = get_tenant_client_data(tenant.id)
    icp_state = _tenant_json_state(_icp_state_path(), tenant.id)
    channel_connection = channel_connections.get_tenant_channel_connection(tenant.id)
    return {
        "tenant_id": tenant.id,
        "tenant_name": tenant.name,
        "export_version": EXPORT_VERSION,
        "sections": [
            "account",
            "prompts",
            "sot",
            "channels",
            "settings",
            "learning",
            "technical_metadata",
        ],
        "optional_sections": [
            "uploaded_files",
            "conversation_escalation_history",
            "logs",
            "inactive_archived_data",
        ],
        "has_client_json": bool(client_data),
        "has_icp_prompt_state": bool(icp_state),
        "whatsapp_status": channel_connection.status if channel_connection else "not_connected",
        "secrets_policy": "Raw provider tokens, passwords, API keys, and auth URLs are excluded/redacted.",
    }


def create_tenant_export(
    tenant_id: str,
    *,
    include_history: bool = False,
    include_files: bool = False,
    include_logs: bool = False,
    include_archived: bool = False,
    actor: str = "internal_admin",
    rollback: bool = False,
) -> ExportResult:
    tenant = _require_tenant(tenant_id)
    audit_log.record_event(
        action="tenant_export_started",
        tenant_id=tenant.id,
        safe_summary="Tenant export started.",
        metadata={"rollback": rollback},
        actor=actor,
    )
    try:
        package = _build_package(
            tenant.id,
            include_history=include_history,
            include_files=include_files,
            include_logs=include_logs,
            include_archived=include_archived,
        )
        target_dir = _rollback_dir() if rollback else _export_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        job_id = f"texp_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_urlsafe(6)}"
        zip_path = target_dir / f"{tenant.id}-{job_id}.zip"
        _write_zip(zip_path, package)
        _record_job(
            job_id,
            {
                "id": job_id,
                "type": "export",
                "tenant_id": tenant.id,
                "status": "completed",
                "zip_path": str(zip_path),
                "created_at": _now(),
                "rollback": rollback,
            },
        )
        audit_log.record_event(
            action="tenant_export_completed",
            tenant_id=tenant.id,
            safe_summary="Tenant export ZIP created.",
            metadata={
                "job_id": job_id,
                "sections": package["manifest.json"]["included_sections"],
                "excluded": package["manifest.json"]["excluded_sections"],
                "rollback": rollback,
            },
            actor=actor,
        )
        return ExportResult(
            job_id=job_id,
            tenant_id=tenant.id,
            zip_path=zip_path,
            manifest=package["manifest.json"],
        )
    except Exception as exc:
        audit_log.record_event(
            action="tenant_export_failed",
            tenant_id=tenant.id,
            result="failed",
            safe_summary="Tenant export failed.",
            metadata={"error": str(exc)[:500], "rollback": rollback},
            actor=actor,
        )
        raise


def validate_import_package(package_path: Path) -> ValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, Any] = {}
    if not package_path.exists():
        return ValidationResult(False, ["Upload was not saved."], warnings, summary)
    try:
        with zipfile.ZipFile(package_path) as zf:
            names = set(zf.namelist())
            missing = sorted(REQUIRED_FILES - names)
            if missing:
                errors.append("Missing required file(s): " + ", ".join(missing))
            manifest = _read_json_from_zip(zf, "manifest.json", errors)
            checksums = _read_json_from_zip(zf, "checksums.json", errors)
            tenant_data = _read_json_from_zip(zf, "tenant.json", errors)
            if isinstance(manifest, dict):
                if manifest.get("export_version") != EXPORT_VERSION:
                    errors.append("Unsupported export version.")
                tenant_slug = str(manifest.get("tenant_slug") or "").strip()
                try:
                    validate_slug(tenant_slug)
                except Exception:
                    errors.append("Manifest tenant slug is invalid.")
                summary = {
                    "tenant_slug": tenant_slug,
                    "tenant_name": manifest.get("tenant_name") or tenant_slug,
                    "exported_at": manifest.get("exported_at"),
                    "export_version": manifest.get("export_version"),
                    "included_sections": manifest.get("included_sections", []),
                    "excluded_sections": manifest.get("excluded_sections", []),
                    "secrets_handling": manifest.get("secrets_handling", ""),
                    "warnings": manifest.get("warnings", []),
                }
            if isinstance(checksums, dict):
                expected = checksums.get("files")
                if isinstance(expected, dict):
                    for name, expected_hash in expected.items():
                        if name == "checksums.json":
                            continue
                        if name not in names:
                            errors.append(f"Checksum target missing: {name}")
                            continue
                        actual = hashlib.sha256(zf.read(name)).hexdigest()
                        if actual != expected_hash:
                            errors.append(f"Checksum mismatch: {name}")
                else:
                    errors.append("checksums.json has no files map.")
            if isinstance(tenant_data, dict):
                packaged_slug = str(tenant_data.get("tenant", {}).get("slug") or "").strip()
                manifest_slug = str(summary.get("tenant_slug") or "").strip()
                if packaged_slug and manifest_slug and packaged_slug != manifest_slug:
                    errors.append("tenant.json slug does not match manifest.")
            if summary.get("excluded_sections"):
                warnings.append("Some secrets/provider data were excluded and channels may need reconnecting.")
    except zipfile.BadZipFile:
        errors.append("Uploaded file is not a valid ZIP package.")
    except OSError as exc:
        errors.append(f"Could not read import package: {exc}")
    ok = not errors
    return ValidationResult(ok, errors, warnings, summary, package_path)


def import_tenant_backup(
    package_path: Path,
    *,
    mode: str,
    target_tenant_id: str,
    confirmation: str,
    new_slug: str = "",
    new_name: str = "",
    actor: str = "internal_admin",
) -> ImportResult:
    validation = validate_import_package(package_path)
    if not validation.ok:
        raise ValueError("; ".join(validation.errors))
    source_slug = str(validation.summary.get("tenant_slug") or "")
    if mode not in {"validate", "restore_existing", "clone_new"}:
        raise ValueError("Unsupported import mode.")
    if mode == "validate":
        audit_log.record_event(
            action="tenant_import_validation_started",
            tenant_id=target_tenant_id or source_slug,
            safe_summary="Tenant import package validated only.",
            metadata=validation.summary,
            actor=actor,
        )
        return ImportResult(True, mode, target_tenant_id or source_slug, None, validation.summary)
    if mode == "restore_existing":
        target = validate_slug(target_tenant_id)
        if confirmation != target:
            raise ValueError("Typed confirmation must match the target tenant slug.")
        _require_tenant(target)
    else:
        target = validate_slug(new_slug)
        if get_tenant(target) is not None:
            raise ValueError("Target clone slug already exists.")
        if confirmation != target:
            raise ValueError("Typed confirmation must match the new tenant slug.")

    audit_log.record_event(
        action="tenant_import_started",
        tenant_id=target,
        safe_summary=f"Tenant import started in mode {mode}.",
        metadata={"source_slug": source_slug},
        actor=actor,
    )
    rollback: ExportResult | None = None
    if mode == "restore_existing":
        rollback = create_tenant_export(target, actor=actor, rollback=True)
        audit_log.record_event(
            action="tenant_import_rollback_created",
            tenant_id=target,
            safe_summary="Rollback export created before import.",
            metadata={"rollback_path": str(rollback.zip_path)},
            actor=actor,
        )
    try:
        package = _read_package(package_path)
        _apply_package(
            package,
            target_slug=target,
            target_name=new_name.strip() or None,
            mode=mode,
        )
        summary = dict(validation.summary)
        summary["target_tenant_id"] = target
        summary["channels_requiring_reconnect"] = _channels_requiring_reconnect(package)
        _record_job(
            f"timp_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}_{secrets.token_urlsafe(6)}",
            {
                "type": "import",
                "tenant_id": target,
                "status": "completed",
                "mode": mode,
                "rollback_path": str(rollback.zip_path) if rollback else None,
                "created_at": _now(),
            },
        )
        audit_log.record_event(
            action="tenant_import_completed",
            tenant_id=target,
            safe_summary="Tenant import completed. Provider channels may require reconnect.",
            metadata=summary,
            actor=actor,
        )
        return ImportResult(True, mode, target, rollback.zip_path if rollback else None, summary)
    except Exception as exc:
        audit_log.record_event(
            action="tenant_import_failed",
            tenant_id=target,
            result="failed",
            safe_summary="Tenant import failed; rollback package is available if one was created.",
            metadata={
                "error": str(exc)[:500],
                "rollback_path": str(rollback.zip_path) if rollback else None,
            },
            actor=actor,
        )
        raise


def job_status(job_id: str) -> dict[str, Any] | None:
    jobs = _load_jobs()
    job = jobs.get(job_id)
    return job if isinstance(job, dict) else None


def export_path_for_job(tenant_id: str, job_id: str) -> Path | None:
    job = job_status(job_id)
    if not job or job.get("tenant_id") != tenant_id:
        return None
    path = Path(str(job.get("zip_path") or ""))
    if not path.exists():
        return None
    try:
        path.relative_to(_data_dir().resolve())
    except ValueError:
        return None
    return path


def save_upload_to_temp(filename: str, content: bytes) -> Path:
    temp_dir = _import_upload_dir()
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if filename.lower().endswith(".zip") else ".upload"
    path = temp_dir / f"import-{secrets.token_urlsafe(12)}{suffix}"
    path.write_bytes(content)
    return path


def _build_package(
    tenant_id: str,
    *,
    include_history: bool,
    include_files: bool,
    include_logs: bool,
    include_archived: bool,
) -> dict[str, Any]:
    tenant = _require_tenant(tenant_id)
    client_data_raw = get_tenant_client_data(tenant.id)
    tenant_data, tenant_excluded = _redact(client_data_raw)
    account = tenant_account_details(tenant.id)
    prompts = _prompt_data(tenant.id)
    channels = _channel_data(tenant.id)
    settings = _settings_data(tenant.id, include_archived=include_archived)
    learning = _learning_data(tenant.id)
    logs = _logs_data(tenant.id) if include_logs else {"included": False, "events": []}
    knowledge_files = _knowledge_files(tenant.id, include_files=include_files)
    included = [
        "account",
        "prompts",
        "sot",
        "channels",
        "settings",
        "learning",
        "technical_metadata",
    ]
    if include_logs:
        included.append("logs")
    if include_files:
        included.append("uploaded_files")
    excluded = [
        "raw_provider_tokens",
        "raw_passwords",
        "raw_api_keys",
        "platform.env_values",
    ]
    if not include_history:
        excluded.append("conversation_escalation_history")
    if not include_files:
        excluded.append("uploaded_file_binaries")
    manifest = {
        "tenant_slug": tenant.id,
        "tenant_name": tenant.name,
        "exported_at": _now(),
        "export_version": EXPORT_VERSION,
        "app_version": _git_commit(),
        "included_sections": included,
        "excluded_sections": excluded,
        "secrets_handling": "Secrets are excluded or redacted. Channels may require reconnect after restore.",
        "source_environment": get_settings().env,
        "partial": bool(excluded),
        "warnings": [
            "Provider channels are metadata-only unless a secure reconnect flow is run.",
            "platform.env values are not exported.",
        ],
    }
    package: dict[str, Any] = {
        "manifest.json": manifest,
        "tenant.json": {
            "tenant": {
                "slug": tenant.id,
                "name": tenant.name,
                "status": tenant.status,
                "account": account,
                "client_json": tenant_data,
                "excluded_client_fields": tenant_excluded,
            }
        },
        "prompts.json": prompts,
        "channels.json": channels,
        "settings.json": settings,
        "learning.json": learning,
        "logs.json": logs,
        "README_RESTORE.txt": _restore_readme(tenant.id),
    }
    if include_files:
        for rel, file_path in knowledge_files.items():
            package[f"uploads/{rel}"] = file_path
    checksums = _checksums_for_package(package)
    package["checksums.json"] = {"algorithm": "sha256", "files": checksums}
    return package


def _prompt_data(tenant_id: str) -> dict[str, Any]:
    state = _tenant_json_state(_icp_state_path(), tenant_id)
    envelope = icp_overrides.effective_state_envelope(tenant_id)
    return {
        "source": "nr3_icp_overrides",
        "tenant_id": tenant_id,
        "ai_agent_settings": envelope.get("ai_agent_settings", {}),
        "sot_entries": envelope.get("sot_entries", []),
        "feature_toggles": envelope.get("feature_toggles", {}),
        "raw_tenant_state": _redact(state)[0],
        "not_indexed_yet": [
            "Nr2 runtime prompt/settings database",
            "Live Marina base prompt builder",
            "Channel-specific runtime prompt fragments",
        ],
    }


def _channel_data(tenant_id: str) -> dict[str, Any]:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    latest = channel_connections.get_latest_connection_request_for_tenant(tenant_id)
    conn_data = None
    if connection:
        conn_data = {
            "channel": connection.channel,
            "provider": connection.provider,
            "status": connection.status,
            "zernio_profile_id": connection.zernio_profile_id,
            "zernio_account_id": connection.zernio_account_id,
            "phone_number_id": connection.phone_number_id,
            "display_phone_number": connection.display_phone_number,
            "waba_id": connection.waba_id,
            "metadata": _redact(_json_loads(connection.metadata_json))[0],
            "connected_at": connection.connected_at,
            "requires_reconnect": connection.status == "connected",
        }
    latest_data = None
    if latest:
        latest_data = {
            "channel": latest.channel,
            "provider": latest.provider,
            "status": latest.status,
            "zernio_profile_id": latest.zernio_profile_id,
            "zernio_account_id": latest.zernio_account_id,
            "selected_phone_number_id": latest.selected_phone_number_id,
            "display_phone_number": latest.display_phone_number,
            "error_summary": latest.error_summary,
            "created_at": latest.created_at,
            "updated_at": latest.updated_at,
        }
    return {
        "visibility": channel_state.read_channels(tenant_id),
        "whatsapp_connection": conn_data,
        "latest_whatsapp_request": latest_data,
        "secrets_exported": False,
    }


def _settings_data(tenant_id: str, *, include_archived: bool) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "include_archived_requested": include_archived,
        "registry": _registry_entry(tenant_id),
        "platform_env": _platform_env_metadata(tenant_id),
    }


def _learning_data(tenant_id: str) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "included": False,
        "reason": "Nr2 learning entries are not indexed by Nr3 export yet.",
        "items": [],
    }


def _logs_data(tenant_id: str) -> dict[str, Any]:
    events = []
    for event in audit_log.list_events(limit=250):
        if event.tenant_id == tenant_id:
            events.append({
                "id": event.id,
                "actor": event.actor,
                "tenant_id": event.tenant_id,
                "action": event.action,
                "result": event.result,
                "safe_summary": event.safe_summary,
                "metadata": _redact(_json_loads(event.metadata_json))[0],
                "created_at": event.created_at,
            })
    return {"included": True, "events": events}


def _knowledge_files(tenant_id: str, *, include_files: bool) -> dict[str, Path]:
    if not include_files:
        return {}
    root = _tenant_root(tenant_id)
    if root is None:
        return {}
    candidates = [root / "data", root / "uploads", root / "knowledge"]
    files: dict[str, Path] = {}
    for base in candidates:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.stat().st_size <= 50 * 1024 * 1024:
                rel = f"{base.name}/{path.relative_to(base)}"
                files[rel] = path
    return files


def _apply_package(
    package: dict[str, Any],
    *,
    target_slug: str,
    target_name: str | None,
    mode: str,
) -> None:
    tenant_json = package.get("tenant.json")
    prompts = package.get("prompts.json")
    channels = package.get("channels.json")
    if not isinstance(tenant_json, dict):
        raise ValueError("tenant.json is invalid.")
    source_tenant = tenant_json.get("tenant")
    if not isinstance(source_tenant, dict):
        raise ValueError("tenant.json has no tenant object.")
    client_json = source_tenant.get("client_json")
    if not isinstance(client_json, dict):
        client_json = {}
    existing_client = get_tenant_client_data(target_slug) if mode == "restore_existing" else {}
    clean_client = _prepare_client_json_for_import(client_json, existing_client)
    clean_client["slug"] = target_slug
    clean_client["name"] = target_name or clean_client.get("name") or source_tenant.get("name") or target_slug
    clean_client["status"] = clean_client.get("status") if clean_client.get("status") in {"active", "inactive"} else "inactive"
    _write_client_json(target_slug, clean_client, create=(mode == "clone_new"))
    register_tenant({
        "slug": target_slug,
        "name": str(clean_client.get("name") or target_slug),
        "status": str(clean_client.get("status") or "inactive"),
    })
    if isinstance(prompts, dict):
        _apply_icp_prompt_state(target_slug, prompts)
    if isinstance(channels, dict):
        visibility = channels.get("visibility")
        if isinstance(visibility, dict):
            valid_channel_keys = {key for _, key in channel_state.CHANNEL_KEYS}
            for key, value in visibility.items():
                if key in valid_channel_keys:
                    channel_state.set_channel(target_slug, key, bool(value))
        connection = channels.get("whatsapp_connection")
        if isinstance(connection, dict) and connection.get("status"):
            # Safe metadata only. Force reconnect instead of pretending provider
            # secrets/account access moved with the package.
            channel_connections.upsert_tenant_channel_connection(
                tenant_id=target_slug,
                channel=str(connection.get("channel") or "whatsapp"),
                provider=str(connection.get("provider") or "zernio"),
                status="pending" if connection.get("status") == "connected" else str(connection.get("status")),
                zernio_profile_id=connection.get("zernio_profile_id"),
                zernio_account_id=connection.get("zernio_account_id"),
                phone_number_id=connection.get("phone_number_id"),
                display_phone_number=connection.get("display_phone_number"),
                waba_id=connection.get("waba_id"),
                metadata={
                    "restored_from_backup": True,
                    "requires_reconnect": True,
                },
                last_error="Restored from backup; reconnect provider channel.",
            )


def _apply_icp_prompt_state(target_slug: str, prompts: dict[str, Any]) -> None:
    raw_state = prompts.get("raw_tenant_state")
    if not isinstance(raw_state, dict):
        return
    path = Path(_icp_state_path())
    data = _json_file(path, default={"tenants": {}})
    tenants = data.setdefault("tenants", {})
    if not isinstance(tenants, dict):
        tenants = {}
        data["tenants"] = tenants
    tenants[target_slug] = raw_state
    _atomic_json(path, data)


def _write_client_json(target_slug: str, client_json: dict[str, Any], *, create: bool) -> None:
    root = _client_root()
    if not root:
        raise ValueError("NR3_TENANTS_CLIENT_DIR is unavailable.")
    tenant_root = root / target_slug
    if create and tenant_root.exists():
        raise ValueError("Target tenant folder already exists.")
    (tenant_root / "config").mkdir(parents=True, exist_ok=True)
    (tenant_root / "data").mkdir(parents=True, exist_ok=True)
    path = tenant_root / "config" / "client.json"
    _atomic_json(path, client_json)


def _prepare_client_json_for_import(
    imported: dict[str, Any],
    existing: dict[str, Any],
) -> dict[str, Any]:
    """Use imported safe fields while preserving existing secrets.

    Export packages contain ``SECRET_MARKER`` instead of raw credentials. For
    restore-existing we keep the current runtime value. For clones/new tenants
    there is no safe value to write, so the redacted key is omitted.
    """

    def walk(new_value: Any, old_value: Any) -> Any:
        if new_value == SECRET_MARKER:
            return old_value if old_value not in (None, "") else None
        if isinstance(new_value, dict):
            old_map = old_value if isinstance(old_value, dict) else {}
            out: dict[str, Any] = {}
            for key, item in new_value.items():
                if item == SECRET_MARKER and key not in old_map:
                    continue
                resolved = walk(item, old_map.get(key))
                if resolved is not None:
                    out[key] = resolved
            return out
        if isinstance(new_value, list):
            return [walk(item, None) for item in new_value]
        return new_value

    prepared = walk(imported, existing)
    return prepared if isinstance(prepared, dict) else {}


def _write_zip(path: Path, package: dict[str, Any]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, value in package.items():
            if isinstance(value, Path):
                zf.write(value, arcname=name)
            elif isinstance(value, str):
                zf.writestr(name, value)
            else:
                zf.writestr(
                    name,
                    json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                )


def _read_package(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with zipfile.ZipFile(path) as zf:
        for name in REQUIRED_FILES - {"README_RESTORE.txt"}:
            result[name] = json.loads(zf.read(name).decode("utf-8"))
        result["README_RESTORE.txt"] = zf.read("README_RESTORE.txt").decode("utf-8")
    return result


def _read_json_from_zip(zf: zipfile.ZipFile, name: str, errors: list[str]) -> Any:
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except KeyError:
        return None
    except (json.JSONDecodeError, UnicodeDecodeError):
        errors.append(f"{name} is not valid JSON.")
        return None


def _checksums_for_package(package: dict[str, Any]) -> dict[str, str]:
    checksums: dict[str, str] = {}
    for name, value in package.items():
        if name == "checksums.json":
            continue
        if isinstance(value, Path):
            payload = value.read_bytes()
        elif isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        checksums[name] = hashlib.sha256(payload).hexdigest()
    return checksums


def _redact(value: Any, *, path: str = "") -> tuple[Any, list[str]]:
    excluded: list[str] = []
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_s = str(key)
            child_path = f"{path}.{key_s}" if path else key_s
            if _is_secret_key(key_s):
                clean[key_s] = SECRET_MARKER
                excluded.append(child_path)
                continue
            clean_value, child_excluded = _redact(item, path=child_path)
            clean[key_s] = clean_value
            excluded.extend(child_excluded)
        return clean, excluded
    if isinstance(value, list):
        out = []
        for idx, item in enumerate(value):
            clean_value, child_excluded = _redact(item, path=f"{path}[{idx}]")
            out.append(clean_value)
            excluded.extend(child_excluded)
        return out, excluded
    return value, excluded


def _is_secret_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SECRET_KEY_PARTS)


def _platform_env_metadata(tenant_id: str) -> dict[str, Any]:
    root = _tenant_root(tenant_id)
    if root is None:
        return {"exists": False, "values_exported": False, "keys": []}
    path = root / "config" / "platform.env"
    if not path.exists():
        return {"exists": False, "values_exported": False, "keys": []}
    keys = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            keys.append(stripped.split("=", 1)[0])
    except OSError:
        pass
    return {
        "exists": True,
        "values_exported": False,
        "keys": sorted(set(keys)),
        "requires_manual_recreate": True,
    }


def _registry_entry(tenant_id: str) -> dict[str, Any]:
    data = _json_file(Path(os.getenv("NR3_TENANT_REGISTRY_PATH", "data/tenant_registry.json")), default={})
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    entry = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    return _redact(entry if isinstance(entry, dict) else {})[0]


def _tenant_json_state(path: str, tenant_id: str) -> dict[str, Any]:
    data = _json_file(Path(path), default={})
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    return state if isinstance(state, dict) else {}


def _channels_requiring_reconnect(package: dict[str, Any]) -> list[str]:
    channels = package.get("channels.json")
    if not isinstance(channels, dict):
        return []
    connection = channels.get("whatsapp_connection")
    if isinstance(connection, dict) and connection.get("status") == "connected":
        return ["whatsapp"]
    return []


def _restore_readme(tenant_id: str) -> str:
    return (
        f"Unboks Nr3 tenant backup for {tenant_id}\n\n"
        "Restore only through Nr3 Tenant Backup & Restore.\n"
        "Raw provider tokens, passwords, API keys, and platform.env values are not included.\n"
        "Connected channels may need reconnecting after restore or clone.\n"
        "Use Validate only before applying an import.\n"
    )


def _require_tenant(tenant_id: str):
    safe = validate_slug(tenant_id)
    tenant = get_tenant(safe)
    if tenant is None:
        raise ValueError("Tenant not found.")
    return tenant


def _json_loads(text: str | None) -> Any:
    if not text:
        return {}
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}


def _json_file(path: Path, *, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return default


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _record_job(job_id: str, payload: dict[str, Any]) -> None:
    jobs = _load_jobs()
    jobs[job_id] = payload
    _atomic_json(_jobs_path(), jobs)


def _load_jobs() -> dict[str, Any]:
    data = _json_file(_jobs_path(), default={})
    return data if isinstance(data, dict) else {}


def _data_dir() -> Path:
    return Path(os.getenv("NR3_DATA_DIR", "data")).resolve()


def _export_dir() -> Path:
    return _data_dir() / "tenant_exports"


def _rollback_dir() -> Path:
    return _data_dir() / "tenant_import_rollbacks"


def _import_upload_dir() -> Path:
    return _data_dir() / "tenant_import_uploads"


def _jobs_path() -> Path:
    return _data_dir() / "tenant_backup_jobs.json"


def _icp_state_path() -> str:
    return os.getenv("NR3_ICP_STATE_PATH", "data/icp_overrides.json").strip()


def _client_root() -> Path | None:
    root = os.getenv("NR3_TENANTS_CLIENT_DIR", "/root/clients").strip()
    if not root:
        return None
    return Path(root)


def _tenant_root(tenant_id: str) -> Path | None:
    root = _client_root()
    if root is None:
        return None
    path = root / tenant_id
    return path if path.exists() else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _git_commit() -> str:
    head = Path(".git/HEAD")
    try:
        ref = head.read_text(encoding="utf-8").strip()
        if ref.startswith("ref: "):
            ref_path = Path(".git") / ref.split(" ", 1)[1]
            return ref_path.read_text(encoding="utf-8").strip()[:12]
        return ref[:12]
    except OSError:
        return "unknown"


def cleanup_temp_upload(path: Path | None) -> None:
    if path is None:
        return
    try:
        temp_root = _import_upload_dir().resolve()
        resolved = path.resolve()
        resolved.relative_to(temp_root)
    except (OSError, ValueError):
        return
    try:
        resolved.unlink()
    except OSError:
        pass


def copy_upload_to_path(src: Path) -> Path:
    dest = _import_upload_dir() / f"import-{secrets.token_urlsafe(12)}.zip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    return dest
