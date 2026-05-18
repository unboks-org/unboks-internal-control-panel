from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app import push_changes


SCHEMA_VERSION = 1
IMPORT_MAX_BYTES = 50 * 1024 * 1024


_TABLES_AND_COLS: tuple = (
    ("tenant_display_overrides", ("tenant_id", "key", "value", "updated_at", "updated_by")),
    ("tenant_feature_overrides", ("tenant_id", "feature_key", "enabled", "updated_at", "updated_by")),
    ("tenant_sot_overrides", ("id", "tenant_id", "title", "content", "category", "created_at", "updated_at", "created_by", "updated_by", "file_path", "file_size")),
    ("tenant_ai_tone_overrides", ("tenant_id", "tone", "notes", "updated_at", "updated_by")),
    ("tenant_ai_escalation_overrides", ("tenant_id", "soft_enabled", "soft_when", "hard_enabled", "hard_when", "updated_at", "updated_by")),
    ("tenant_log", ("tenant_id", "timestamp", "kind", "actor", "title", "body")),
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="microseconds")


def _client_json_path(tenant_id: str) -> Optional[Path]:
    root = os.environ.get("NR3_TENANTS_CLIENT_DIR")
    if not root:
        return None
    return Path(root) / tenant_id / "config" / "client.json"


def _sot_files_dir(tenant_id: str) -> Path:
    db_path = Path(get_settings().db_path)
    return db_path.parent / "sot_uploads" / tenant_id


def _connect():
    db = sqlite3.connect(Path(get_settings().db_path))
    db.row_factory = sqlite3.Row
    return db


def build_tenant_export(tenant_id: str) -> bytes:
    """Build a single ZIP of this tenant's state + config + SOT files."""
    if not (isinstance(tenant_id, str) and tenant_id.strip()):
        raise ValueError("tenant_id is required")

    push_changes.init_db()

    state: dict = {}
    with _connect() as conn:
        for table, cols in _TABLES_AND_COLS:
            rows = conn.execute(
                f"SELECT {', '.join(cols)} FROM {table} WHERE tenant_id = ? ORDER BY rowid ASC",
                (tenant_id,),
            ).fetchall()
            state[table] = [dict(r) for r in rows]

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tenant_id": tenant_id,
        "exported_at": _utcnow_iso(),
        "exported_by": "owner",
        "row_counts": {t: len(state[t]) for t, _ in _TABLES_AND_COLS},
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("state.json", json.dumps(state, indent=2))

        cj = _client_json_path(tenant_id)
        if cj is not None and cj.exists() and cj.is_file():
            zf.write(cj, arcname="client.json")

        sot_dir = _sot_files_dir(tenant_id)
        if sot_dir.exists() and sot_dir.is_dir():
            for entry_dir in sorted(sot_dir.iterdir()):
                if not entry_dir.is_dir():
                    continue
                for fp in sorted(entry_dir.iterdir()):
                    if fp.is_file():
                        zf.write(fp, arcname=f"sot_files/{entry_dir.name}/{fp.name}")

    return buf.getvalue()


def restore_tenant_export(tenant_id: str, zip_bytes: bytes) -> dict:
    """REPLACE all state for tenant_id from the uploaded ZIP."""
    if not (isinstance(tenant_id, str) and tenant_id.strip()):
        raise ValueError("tenant_id is required")
    if not isinstance(zip_bytes, (bytes, bytearray)) or len(zip_bytes) == 0:
        raise ValueError("Empty or missing import file")
    if len(zip_bytes) > IMPORT_MAX_BYTES:
        raise ValueError(f"Import file too large ({len(zip_bytes)} bytes)")

    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ValueError(f"Not a valid ZIP file: {exc}")

    names = set(zf.namelist())
    if "manifest.json" not in names or "state.json" not in names:
        raise ValueError("ZIP missing manifest.json or state.json")

    try:
        manifest = json.loads(zf.read("manifest.json"))
        state = json.loads(zf.read("state.json"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in import: {exc}")

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported schema_version")

    src_tenant = manifest.get("tenant_id")
    if src_tenant != tenant_id:
        raise ValueError("tenant_id mismatch")

    push_changes.init_db()

    with _connect() as conn:
        for table, _ in _TABLES_AND_COLS:
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,))
        for table, cols in _TABLES_AND_COLS:
            for row in state.get(table, []):
                if not isinstance(row, dict):
                    continue
                row = {**row, "tenant_id": tenant_id}
                values = tuple(row.get(c) for c in cols)
                placeholders = ", ".join("?" for _ in cols)
                conn.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})", values)
        conn.commit()

    # SOT files
    sot_dir = _sot_files_dir(tenant_id)
    if sot_dir.exists():
        shutil.rmtree(sot_dir)
    sot_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        if name.startswith("sot_files/") and not name.endswith("/"):
            rel = name[len("sot_files/"):]
            target = sot_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(name))

    # client.json
    if "client.json" in names:
        cj = _client_json_path(tenant_id)
        if cj is not None:
            cj.parent.mkdir(parents=True, exist_ok=True)
            cj.write_bytes(zf.read("client.json"))

    return manifest


# ===================== Tenant Provisioning (New) =====================

def provision_new_tenant(slug: str, password: str):
    """
    Creates the tenant folder + client.json on the server.
    Call this when a new tenant is created in the ICP.
    """
    base = os.environ.get("NR3_TENANTS_CLIENT_DIR", "/root/wtyj/tenant_root")
    tenant_dir = Path(base) / slug / "config"
    tenant_dir.mkdir(parents=True, exist_ok=True)

    client_data = {
        "business": {
            "name": slug,
            "password": password
        }
    }

    (tenant_dir / "client.json").write_text(json.dumps(client_data, indent=2))
    print(f"[ICP] Provisioned tenant on disk: {slug}")