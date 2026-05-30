"""Tenant configuration backup/export/import helpers for Nr3.

Exports are authenticated ZIP packages. Secrets are never exported in plain
text; runtime credentials are replaced with explicit "excluded" markers.
Import restores Nr3-controlled state and safe account metadata only. External
provider channels are marked as requiring reconnect.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
import zipfile
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import audit_log, channel_connections, channel_state, icp_overrides, tenant_notes
from app.tenants import (
    get_tenant,
    get_tenant_client_data,
    register_tenant,
    tenant_account_details,
    update_tenant_account_details,
    validate_slug,
)


EXPORT_VERSION = "1.0"
SECRET_HINTS = ("password", "secret", "token", "access_key", "api_key", "private_key")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _exports_dir() -> Path:
    root = Path(os.getenv("NR3_TENANT_EXPORTS_DIR", "data/tenant_exports"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _rollback_dir() -> Path:
    root = Path(os.getenv("NR3_TENANT_IMPORT_ROLLBACK_DIR", "data/tenant_import_rollbacks"))
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_json(value: Any) -> Any:
    if is_dataclass(value):
        return _safe_json(asdict(value))
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            lowered = key_text.lower()
            if any(hint in lowered for hint in SECRET_HINTS):
                out[key_text] = {"excluded": True, "reason": "secret_not_exported"}
            else:
                out[key_text] = _safe_json(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    return value


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_zip_json(zf: zipfile.ZipFile, name: str, value: Any, checksums: dict[str, str]) -> None:
    data = _json_bytes(value)
    zf.writestr(name, data)
    checksums[name] = _checksum(data)


def _connection_snapshot(tenant_id: str) -> dict[str, Any]:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    latest = channel_connections.get_latest_connection_request_for_tenant(tenant_id)
    connection_data = _safe_json(connection) if connection else None
    latest_data = _safe_json(latest) if latest else None
    if isinstance(latest_data, dict):
        latest_data.pop("auth_url", None)
        latest_data.pop("state_token_hash", None)
    return {
        "whatsapp": connection_data,
        "latest_request": latest_data,
        "secrets": "provider tokens and live authorization links are excluded; reconnect may be required after restore",
    }


def build_export_package(
    tenant_id: str,
    *,
    include_history: bool = False,
    include_files: bool = False,
    include_logs: bool = False,
    include_inactive: bool = False,
) -> Path:
    tenant = get_tenant(tenant_id)
    if tenant is None:
        raise ValueError("tenant not found")
    safe_slug = validate_slug(tenant_id)
    timestamp = _now()
    package_id = f"{safe_slug}-{timestamp.replace(':', '').replace('+', 'Z')}-{secrets.token_hex(4)}"
    path = _exports_dir() / f"{package_id}.zip"
    checksums: dict[str, str] = {}

    client_data = get_tenant_client_data(safe_slug)
    account = tenant_account_details(safe_slug)
    ai_settings = icp_overrides.ai_agent_settings_for_tenant(safe_slug)
    sot_entries = icp_overrides.sot_entries_for_tenant(safe_slug)
    notes = [asdict(note) for note in tenant_notes.list_notes(safe_slug)]
    channels = channel_state.read_channels(safe_slug)
    connections = _connection_snapshot(safe_slug)

    manifest = {
        "export_version": EXPORT_VERSION,
        "tenant_slug": safe_slug,
        "tenant_name": tenant.name,
        "export_timestamp": timestamp,
        "source_environment": os.getenv("NR3_ENV", "unknown"),
        "included_sections": [
            "tenant",
            "prompts",
            "channels",
            "learning",
            "settings",
        ],
        "optional_sections": {
            "history": bool(include_history),
            "files": bool(include_files),
            "logs": bool(include_logs),
            "inactive_archived": bool(include_inactive),
        },
        "excluded_sections": [
            "raw passwords",
            "provider tokens",
            "live authorization links",
            "payment secrets",
        ],
        "secrets_handling": "raw secrets are replaced with excluded markers; channels may require reconnect after restore",
        "partial": True,
        "partial_reason": "Nr3 exports safe tenant configuration and metadata. External provider secrets are intentionally excluded.",
    }

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_zip_json(zf, "manifest.json", manifest, checksums)
        _write_zip_json(
            zf,
            "tenant.json",
            {
                "tenant": asdict(tenant),
                "account": account,
                "client_json_sanitized": _safe_json(client_data),
            },
            checksums,
        )
        _write_zip_json(
            zf,
            "prompts.json",
            {
                "ai_agent_settings": ai_settings,
                "sot_entries": sot_entries,
                "source": "nr3_icp_overrides",
            },
            checksums,
        )
        _write_zip_json(
            zf,
            "channels.json",
            {
                "visibility": channels,
                "connections": connections,
                "requires_reconnect_after_import": True,
            },
            checksums,
        )
        _write_zip_json(zf, "learning.json", {"tenant_notes": notes}, checksums)
        _write_zip_json(
            zf,
            "settings.json",
            {"account": account, "status": tenant.status},
            checksums,
        )
        if include_logs:
            events = [
                _safe_json(event)
                for event in audit_log.list_events(limit=200)
                if event.tenant_id == safe_slug
            ]
            _write_zip_json(zf, "logs.json", {"audit_events": events}, checksums)
        if include_files:
            zf.writestr(
                "uploads/README.txt",
                "Uploaded runtime files are not copied in this safe v1 export unless a future storage manifest indexes them.\n",
            )
        readme = (
            "Tenant Configuration Backup & Restore\n\n"
            "This ZIP was generated by Nr3.\n"
            "This is a safe v1 configuration backup, not a full disaster-recovery export.\n"
            "Included: tenant/account settings, prompt/SOT data, channel metadata, notes, optional audit logs, and checksums.\n"
            "Excluded or metadata-only: raw secrets, provider tokens, live authorization links, full uploaded file contents, and full conversation history.\n"
            "After restore, external channels may need reconnecting.\n"
            "Use Nr3 Import with Validate only first, then Restore existing or Restore as new tenant.\n"
        ).encode("utf-8")
        zf.writestr("README_RESTORE.txt", readme)
        checksums["README_RESTORE.txt"] = _checksum(readme)
        _write_zip_json(zf, "checksums.json", checksums, checksums)

    audit_log.record_event(
        action="tenant_export_completed",
        tenant_id=safe_slug,
        safe_summary=f"Tenant export package created: {path.name}",
        metadata={"package": path.name, "partial": True},
    )
    return path


def _read_zip_json(zf: zipfile.ZipFile, name: str) -> Any:
    try:
        return json.loads(zf.read(name).decode("utf-8"))
    except KeyError:
        raise ValueError(f"missing required file: {name}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(f"invalid JSON file: {name}")


def validate_import_package(package_path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(package_path):
        raise ValueError("upload is not a ZIP backup package")
    with zipfile.ZipFile(package_path) as zf:
        manifest = _read_zip_json(zf, "manifest.json")
        tenant = _read_zip_json(zf, "tenant.json")
        _read_zip_json(zf, "prompts.json")
        _read_zip_json(zf, "channels.json")
        _read_zip_json(zf, "settings.json")
        checksums = _read_zip_json(zf, "checksums.json")
        if not isinstance(checksums, dict):
            raise ValueError("checksums.json must be an object")
        for name, expected in checksums.items():
            if name == "checksums.json":
                continue
            actual = _checksum(zf.read(name))
            if actual != expected:
                raise ValueError(f"checksum mismatch: {name}")
    slug = str(manifest.get("tenant_slug") or "").strip()
    validate_slug(slug)
    return {
        "tenant_slug": slug,
        "tenant_name": manifest.get("tenant_name") or tenant.get("tenant", {}).get("name") or slug,
        "export_timestamp": manifest.get("export_timestamp"),
        "export_version": manifest.get("export_version"),
        "included_sections": manifest.get("included_sections") or [],
        "excluded_sections": manifest.get("excluded_sections") or [],
        "secrets_handling": manifest.get("secrets_handling"),
        "partial": bool(manifest.get("partial")),
        "warnings": [
            "Provider tokens and raw passwords are excluded.",
            "External channels are marked as requiring reconnect.",
        ],
    }


def _save_upload(upload_file, suffix: str = ".zip") -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="tenant-import-"))
    path = tmp_dir / f"upload{suffix}"
    with path.open("wb") as f:
        shutil.copyfileobj(upload_file, f)
    return path


def validate_uploaded_package(upload_file) -> dict[str, Any]:
    path = _save_upload(upload_file)
    return validate_import_package(path)


def import_uploaded_package(
    upload_file,
    *,
    target_tenant: str,
    mode: str,
    new_slug: str = "",
    confirmation: str = "",
) -> dict[str, Any]:
    package_path = _save_upload(upload_file)
    summary = validate_import_package(package_path)
    source_slug = summary["tenant_slug"]
    target = validate_slug(new_slug or target_tenant)
    if mode not in {"validate", "restore", "clone"}:
        raise ValueError("unsupported import mode")
    if mode == "validate":
        return {"status": "validated", "summary": summary}
    if mode == "restore" and confirmation != target_tenant:
        raise ValueError("type the current tenant slug to confirm restore")
    if mode == "clone":
        if not new_slug:
            raise ValueError("new slug is required for clone import")
        if get_tenant(target) is not None:
            raise ValueError("target slug already exists")
        if confirmation != "IMPORT CLONE":
            raise ValueError("type IMPORT CLONE to confirm clone")

    if mode == "restore":
        rollback = build_export_package(target_tenant)
        icp_overrides.forget_tenant(target)
        channel_state.forget_tenant(target)
        tenant_notes.forget_tenant(target)
    else:
        rollback_root = _rollback_dir()
        marker = rollback_root / f"{target}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-clone-marker.json"
        marker.write_text(
            json.dumps(
                {"target_tenant": target, "source_tenant": source_slug, "mode": mode, "created_at": _now()},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        rollback = marker

    with zipfile.ZipFile(package_path) as zf:
        tenant_payload = _read_zip_json(zf, "tenant.json")
        prompts = _read_zip_json(zf, "prompts.json")
        channels = _read_zip_json(zf, "channels.json")
        learning = _read_zip_json(zf, "learning.json")

    account = tenant_payload.get("account") if isinstance(tenant_payload, dict) else {}
    if not isinstance(account, dict):
        account = {}
    update_tenant_account_details(
        target,
        name=str(account.get("name") or summary["tenant_name"] or target),
        contact_person=str(account.get("contact_person") or ""),
        email=str(account.get("email") or ""),
        phone=str(account.get("phone") or ""),
        website=str(account.get("website") or ""),
        address=str(account.get("address") or ""),
        logo_url=str(account.get("logo_url") or ""),
    )
    register_tenant({"slug": target, "name": account.get("name") or summary["tenant_name"] or target, "status": "active"})

    ai = prompts.get("ai_agent_settings") if isinstance(prompts, dict) else {}
    if isinstance(ai, dict):
        tone = ai.get("tone") if isinstance(ai.get("tone"), dict) else None
        if tone:
            icp_overrides.set_ai_tone(target, tone.get("tone") or "", notes=tone.get("notes") or "")
        agent_name = ai.get("agent_name") if isinstance(ai.get("agent_name"), dict) else None
        if agent_name:
            icp_overrides.set_agent_name_override(target, agent_name.get("name") or "")
        rules = ai.get("escalation_rules") if isinstance(ai.get("escalation_rules"), dict) else None
        if rules:
            soft = rules.get("soft_escalation") or {}
            hard = rules.get("hard_escalation") or {}
            icp_overrides.set_escalation_rules(
                target,
                soft_when=soft.get("when") or "",
                hard_when=hard.get("when") or "",
            )
    for entry in (prompts.get("sot_entries") if isinstance(prompts, dict) else []) or []:
        if isinstance(entry, dict):
            icp_overrides.add_sot_entry(
                target,
                title=entry.get("title") or "",
                content=entry.get("content") or "",
                category=entry.get("category") or "general",
            )
    visibility = channels.get("visibility") if isinstance(channels, dict) else {}
    if isinstance(visibility, dict):
        for key, value in visibility.items():
            channel_state.set_channel(target, str(key), bool(value))
    for note in (learning.get("tenant_notes") if isinstance(learning, dict) else []) or []:
        if isinstance(note, dict) and note.get("body"):
            added = tenant_notes.add_note(
                target,
                str(note.get("body")),
                priority=str(note.get("priority") or "normal"),
                follow_up_date=note.get("follow_up_date"),
            )
            if bool(note.get("pinned")):
                tenant_notes.toggle_pin(target, added.id)

    audit_log.record_event(
        action="tenant_import_completed",
        tenant_id=target,
        safe_summary=f"Tenant import {mode} completed from {source_slug}; rollback {rollback.name}",
        metadata={"source_slug": source_slug, "mode": mode, "rollback": rollback.name},
    )
    return {
        "status": "imported",
        "mode": mode,
        "target_tenant": target,
        "source_tenant": source_slug,
        "rollback_package": str(rollback),
        "channels_require_reconnect": True,
    }
