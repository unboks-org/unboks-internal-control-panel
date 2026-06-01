"""Tenant backup/export/import helpers for Nr3.

Exports are authenticated single-file packages. They include the selected
tenant runtime folder plus Nr3-owned state so an import can replace the target
tenant with a working clone while preserving target-only runtime identity such
as slug, compose service name, and host port.
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


EXPORT_VERSION = "2.0"
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


def _import_payload_dir() -> Path:
    root = Path(os.getenv("NR3_TENANT_IMPORT_PAYLOAD_DIR", "data/tenant_import_payloads"))
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


def _tenant_root(slug: str) -> Path:
    client_dir = os.getenv("NR3_TENANTS_CLIENT_DIR", "/root/clients").strip()
    return Path(client_dir) / validate_slug(slug)


def _write_zip_file(zf: zipfile.ZipFile, name: str, path: Path, checksums: dict[str, str]) -> None:
    data = path.read_bytes()
    zf.writestr(name, data)
    checksums[name] = _checksum(data)


def _write_client_tree(zf: zipfile.ZipFile, slug: str, checksums: dict[str, str]) -> bool:
    root = _tenant_root(slug)
    if not root.is_dir():
        return False
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.endswith(".lock"):
            continue
        _write_zip_file(zf, f"client_tree/{rel}", path, checksums)
    return True


def _connection_snapshot(tenant_id: str) -> dict[str, Any]:
    connection = channel_connections.get_tenant_channel_connection(tenant_id)
    latest = channel_connections.get_latest_connection_request_for_tenant(tenant_id)
    connection_data = asdict(connection) if connection else None
    latest_data = _safe_json(latest) if latest else None
    if isinstance(latest_data, dict):
        latest_data.pop("auth_url", None)
        latest_data.pop("state_token_hash", None)
    return {
        "whatsapp": connection_data,
        "latest_request": latest_data,
        "secrets": "tenant runtime files are included; live authorization links are excluded",
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
    path = _exports_dir() / f"{package_id}.unboksbackup"
    checksums: dict[str, str] = {}

    client_data = get_tenant_client_data(safe_slug)
    account = tenant_account_details(safe_slug)
    ai_settings = icp_overrides.ai_agent_settings_for_tenant(safe_slug)
    sot_entries = icp_overrides.sot_entries_for_tenant(safe_slug)
    notes = [asdict(note) for note in tenant_notes.list_notes(safe_slug)]
    channels = channel_state.read_channels(safe_slug)
    connections = _connection_snapshot(safe_slug)
    has_client_tree = _tenant_root(safe_slug).is_dir()

    manifest = {
        "export_version": EXPORT_VERSION,
        "tenant_slug": safe_slug,
        "tenant_name": tenant.name,
        "export_timestamp": timestamp,
        "source_environment": os.getenv("NR3_ENV", "unknown"),
        "included_sections": [
            "tenant",
            "tenant_runtime_folder",
            "prompts",
            "channels",
            "learning",
            "settings",
            "runtime_credentials",
        ],
        "optional_sections": {
            "history": bool(include_history),
            "files": bool(include_files),
            "logs": bool(include_logs),
            "inactive_archived": bool(include_inactive),
        },
        "excluded_sections": [
            "live authorization links",
            "payment secrets not stored in the tenant runtime folder",
        ],
        "secrets_handling": "tenant runtime credentials from the tenant folder are included; protect this file like production secrets",
        "partial": False,
        "complete_clone": True,
        "client_tree_included": has_client_tree,
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
                "client_json_raw": client_data,
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
                "requires_reconnect_after_import": False,
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
                "Runtime uploads are included under client_tree/ when they live in the tenant folder.\n",
            )
        if has_client_tree:
            _write_client_tree(zf, safe_slug, checksums)
        readme = (
            "Tenant Full Backup & Restore\n\n"
            "This ZIP was generated by Nr3.\n"
            "It is delivered as one .unboksbackup file.\n"
            "Included: tenant runtime folder, account settings, prompt/SOT data, channel metadata, notes, optional audit logs, uploaded files stored in the tenant folder, runtime databases, and checksums.\n"
            "Import replaces the selected tenant data and rewrites only target-specific identity fields such as slug, container name, and host port.\n"
            "Use Nr3 Import to restore this one backup file into the selected tenant.\n"
        ).encode("utf-8")
        zf.writestr("README_RESTORE.txt", readme)
        checksums["README_RESTORE.txt"] = _checksum(readme)
        _write_zip_json(zf, "checksums.json", checksums, checksums)

    audit_log.record_event(
        action="tenant_export_completed",
        tenant_id=safe_slug,
        safe_summary=f"Tenant export package created: {path.name}",
        metadata={"package": path.name, "partial": False, "client_tree": has_client_tree},
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
        raise ValueError("upload is not an Unboks backup file")
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
        "complete_clone": bool(manifest.get("complete_clone")),
        "client_tree_included": bool(manifest.get("client_tree_included")),
        "warnings": [
            "This backup may contain production credentials. Store it securely.",
            "Import rewrites target slug/container identity to avoid runtime collisions.",
        ],
    }


def _save_upload(upload_file, suffix: str = ".unboksbackup") -> Path:
    tmp_dir = Path(tempfile.mkdtemp(prefix="tenant-import-"))
    path = tmp_dir / f"upload{suffix}"
    with path.open("wb") as f:
        shutil.copyfileobj(upload_file, f)
    return path


def _persist_import_payload(package_path: Path, target: str) -> Path:
    payload_dir = _import_payload_dir()
    name = f"{validate_slug(target)}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(4)}.unboksbackup"
    target_path = payload_dir / name
    shutil.copy2(package_path, target_path)
    try:
        target_path.chmod(0o600)
    except OSError:
        pass
    return target_path


def _can_restore_runtime_in_process(target: str) -> bool:
    mode = os.getenv("NR3_TENANT_RUNTIME_RESTORE_MODE", "").strip().lower()
    if mode == "host":
        return False
    if mode == "direct":
        return True
    root = _tenant_root(target)
    probe = root if root.exists() else root.parent
    return os.access(probe, os.W_OK)


def validate_uploaded_package(upload_file) -> dict[str, Any]:
    path = _save_upload(upload_file)
    return validate_import_package(path)


def _extract_client_tree(zf: zipfile.ZipFile) -> Path | None:
    members = [
        info
        for info in zf.infolist()
        if info.filename.startswith("client_tree/") and not info.is_dir()
    ]
    if not members:
        return None
    root = Path(tempfile.mkdtemp(prefix="tenant-client-tree-"))
    for info in members:
        rel = Path(info.filename[len("client_tree/"):])
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"unsafe client tree path: {info.filename}")
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    return root


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _restore_client_tree(target: str, source_root: Path, previous_root: Path) -> None:
    target_root = _tenant_root(target)
    previous_compose = _read_text_if_exists(previous_root / "docker-compose.yml")
    source_config = source_root / "config" / "client.json"
    source_client = {}
    if source_config.exists():
        try:
            loaded = json.loads(source_config.read_text(encoding="utf-8"))
            source_client = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            source_client = {}
    source_slug = validate_slug(str(source_client.get("slug") or target))
    previous_config = previous_root / "config" / "client.json"
    previous_client = {}
    if previous_config.exists():
        try:
            loaded = json.loads(previous_config.read_text(encoding="utf-8"))
            previous_client = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            previous_client = {}

    if target_root.exists():
        shutil.rmtree(target_root)
    target_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source_root, target_root)

    client_path = target_root / "config" / "client.json"
    if client_path.exists():
        try:
            data = json.loads(client_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        data["slug"] = target
        # Preserve target-only runtime fields when present. Business content
        # still comes from the donor backup.
        for key in ("host_port", "port"):
            if previous_client.get(key) is not None:
                data[key] = previous_client[key]
        business = data.get("business")
        if isinstance(business, dict):
            business["slug"] = target
        client_path.parent.mkdir(parents=True, exist_ok=True)
        client_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    env_path = target_root / "config" / "platform.env"
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
        seen_tenant_id = False
        seen_tenant_slug = False
        out: list[str] = []
        for line in lines:
            if line.startswith("TENANT_ID="):
                out.append(f"TENANT_ID={target}")
                seen_tenant_id = True
            elif line.startswith("TENANT_SLUG="):
                out.append(f"TENANT_SLUG={target}")
                seen_tenant_slug = True
            elif line.startswith("# platform.env for tenant "):
                out.append(f"# platform.env for tenant {target}")
            else:
                out.append(line)
        if not seen_tenant_id:
            out.append(f"TENANT_ID={target}")
        if not seen_tenant_slug:
            out.append(f"TENANT_SLUG={target}")
        env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")

    if previous_compose:
        (target_root / "docker-compose.yml").write_text(previous_compose, encoding="utf-8")
    else:
        compose_path = target_root / "docker-compose.yml"
        if compose_path.exists():
            text = compose_path.read_text(encoding="utf-8")
            text = text.replace(f"container_name: wtyj-{source_slug}", f"container_name: wtyj-{target}")
            text = text.replace(f"/api/{source_slug}/", f"/api/{target}/")
            text = text.replace(f"tenant {source_slug}", f"tenant {target}")
            compose_path.write_text(text, encoding="utf-8")


def _restore_channel_connection(target: str, connections: dict[str, Any]) -> None:
    connection = connections.get("whatsapp") if isinstance(connections, dict) else None
    if not isinstance(connection, dict):
        return
    zernio_profile_id = connection.get("zernio_profile_id")
    if zernio_profile_id:
        channel_connections.set_tenant_zernio_profile_id(
            tenant_id=target,
            name=target,
            zernio_profile_id=str(zernio_profile_id),
        )
    metadata = connection.get("metadata_json")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except json.JSONDecodeError:
            metadata = {"raw": metadata}
    if not isinstance(metadata, dict):
        metadata = {}
    status = str(connection.get("status") or "not_connected")
    if status not in channel_connections.TENANT_CONNECTION_STATUSES:
        status = "not_connected"
    channel_connections.upsert_tenant_channel_connection(
        tenant_id=target,
        channel=str(connection.get("channel") or "whatsapp"),
        provider=str(connection.get("provider") or "zernio"),
        status=status,
        zernio_profile_id=connection.get("zernio_profile_id"),
        zernio_account_id=connection.get("zernio_account_id"),
        phone_number_id=connection.get("phone_number_id"),
        display_phone_number=connection.get("display_phone_number"),
        waba_id=connection.get("waba_id"),
        metadata=metadata,
        last_request_id=connection.get("last_request_id"),
        last_error=connection.get("last_error"),
        connected_at=connection.get("connected_at"),
    )


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
        channel_connections.forget_tenant(target)
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
        client_tree = _extract_client_tree(zf) if _can_restore_runtime_in_process(target) else None

    runtime_restore_package = ""
    if client_tree is None:
        runtime_restore_package = str(_persist_import_payload(package_path, target))

    if client_tree is not None:
        _restore_client_tree(target, client_tree, _tenant_root(target))

    account = tenant_payload.get("account") if isinstance(tenant_payload, dict) else {}
    if not isinstance(account, dict):
        account = {}
    if not runtime_restore_package:
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
    connections = channels.get("connections") if isinstance(channels, dict) else {}
    if isinstance(connections, dict):
        _restore_channel_connection(target, connections)
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
        "client_tree_restored": client_tree is not None,
        "runtime_restore_package": runtime_restore_package,
        "channels_require_reconnect": False,
    }
