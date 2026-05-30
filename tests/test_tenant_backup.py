import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import channel_state, icp_overrides, tenant_notes
from app.main import app
from app.tenant_backup import build_export_package, import_uploaded_package, validate_import_package
from app.tenants import register_tenant


def _seed(monkeypatch, tmp_path, slug="acme"):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp_overrides.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channel_state.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "tenant_notes.json"))
    monkeypatch.setenv("NR3_TENANT_EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("NR3_TENANT_IMPORT_ROLLBACK_DIR", str(tmp_path / "rollbacks"))
    root = tmp_path / "clients" / slug / "config"
    root.mkdir(parents=True)
    (root / "client.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "name": "Acme Co",
                "email": "owner@example.com",
                "password": "do-not-export",
                "access_key": "do-not-export",
            }
        ),
        encoding="utf-8",
    )
    register_tenant({"slug": slug, "name": "Acme Co", "status": "active"})
    icp_overrides.set_ai_tone(slug, "Warm", notes="Helpful")
    icp_overrides.set_agent_name_override(slug, "Sofia")
    icp_overrides.add_sot_entry(slug, title="Hours", content="Open 9-5", category="hours")
    channel_state.set_channel(slug, "whatsapp", True)
    tenant_notes.add_note(slug, "Important internal note", priority="important")
    return slug


def test_export_package_contains_manifest_and_excludes_secrets(monkeypatch, tmp_path):
    slug = _seed(monkeypatch, tmp_path)
    package = build_export_package(slug, include_logs=True)

    with zipfile.ZipFile(package) as zf:
        names = set(zf.namelist())
        assert {
            "manifest.json",
            "tenant.json",
            "prompts.json",
            "channels.json",
            "learning.json",
            "settings.json",
            "README_RESTORE.txt",
            "checksums.json",
        }.issubset(names)
        tenant = json.loads(zf.read("tenant.json"))
        assert tenant["client_json_sanitized"]["password"]["excluded"] is True
        assert tenant["client_json_sanitized"]["access_key"]["excluded"] is True
        prompts = json.loads(zf.read("prompts.json"))
        assert prompts["ai_agent_settings"]["agent_name"]["name"] == "Sofia"

    summary = validate_import_package(package)
    assert summary["tenant_slug"] == slug
    assert summary["partial"] is True


def test_import_validate_only_changes_nothing(monkeypatch, tmp_path):
    slug = _seed(monkeypatch, tmp_path)
    package = build_export_package(slug)
    before = icp_overrides.sot_entries_for_tenant(slug)

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant=slug,
        mode="validate",
    )

    assert result["status"] == "validated"
    assert icp_overrides.sot_entries_for_tenant(slug) == before


def test_import_clone_restores_nr3_state_to_new_slug(monkeypatch, tmp_path):
    slug = _seed(monkeypatch, tmp_path)
    package = build_export_package(slug)

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant=slug,
        mode="clone",
        new_slug="acme-clone",
        confirmation="IMPORT CLONE",
    )

    assert result["status"] == "imported"
    assert result["target_tenant"] == "acme-clone"
    assert channel_state.read_channels("acme-clone")["whatsapp"] is True
    assert icp_overrides.ai_agent_settings_for_tenant("acme-clone")["agent_name"]["name"] == "Sofia"
    assert tenant_notes.list_notes("acme-clone")[0].body == "Important internal note"


def test_workspace_renders_backup_restore_section(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, slug="unboks")
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "Configuration Backup & Restore" in response.text
    assert "not a full disaster-recovery export yet" in response.text
    assert "/admin/tenants/unboks/backup/export" in response.text
    assert "/admin/tenants/unboks/backup/import" in response.text
