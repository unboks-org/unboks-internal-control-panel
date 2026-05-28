import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from app import channel_connections, channel_state, icp_overrides, tenant_backup
from app.main import app


def _setup_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "tenant_registry.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp_overrides.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channel_state.json"))
    root = tmp_path / "clients"
    root.mkdir()
    return root


def _write_tenant(root: Path, slug: str, *, name: str = "Lawyer") -> None:
    config = root / slug / "config"
    config.mkdir(parents=True)
    (root / slug / "data").mkdir(parents=True)
    (root / slug / "uploads").mkdir(parents=True)
    (config / "client.json").write_text(
        json.dumps({
            "slug": slug,
            "name": name,
            "status": "active",
            "email": f"{slug}@example.com",
            "password": "super-secret-password",
            "access_key": "super-secret-access-key",
            "business": {
                "slug": slug,
                "name": name,
                "api_token": "nested-token",
            },
        }),
        encoding="utf-8",
    )
    (config / "platform.env").write_text(
        "DASHBOARD_PASSWORD=secret\nNR3_INTERNAL_API_TOKEN=token\nTENANT_SLUG=" + slug,
        encoding="utf-8",
    )


def test_export_creates_zip_with_manifest_and_redacted_secrets(monkeypatch, tmp_path):
    root = _setup_paths(monkeypatch, tmp_path)
    _write_tenant(root, "lawyer")
    icp_overrides.set_ai_tone("lawyer", "Warm and precise")
    icp_overrides.add_sot_entry(
        "lawyer",
        title="Booking rule",
        content="Ask qualifying questions before offering an appointment.",
    )
    channel_state.set_channel("lawyer", "whatsapp", True)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_1",
        zernio_account_id="account_1",
        display_phone_number="+599 9 123 4567",
        metadata={"safe": "ok", "access_token": "do-not-export"},
    )

    result = tenant_backup.create_tenant_export(
        "lawyer",
        include_history=False,
        include_files=False,
        include_logs=False,
        include_archived=False,
    )

    assert result.zip_path.exists()
    with zipfile.ZipFile(result.zip_path) as zf:
        names = set(zf.namelist())
        assert tenant_backup.REQUIRED_FILES <= names
        manifest = json.loads(zf.read("manifest.json"))
        tenant_json = json.loads(zf.read("tenant.json"))
        channels = json.loads(zf.read("channels.json"))
        checksums = json.loads(zf.read("checksums.json"))

    assert manifest["tenant_slug"] == "lawyer"
    assert "raw_provider_tokens" in manifest["excluded_sections"]
    client_json = tenant_json["tenant"]["client_json"]
    assert client_json["password"] == tenant_backup.SECRET_MARKER
    assert client_json["access_key"] == tenant_backup.SECRET_MARKER
    assert client_json["business"]["api_token"] == tenant_backup.SECRET_MARKER
    assert channels["whatsapp_connection"]["metadata"]["access_token"] == tenant_backup.SECRET_MARKER
    assert checksums["files"]["manifest.json"]


def test_validate_import_rejects_bad_zip(monkeypatch, tmp_path):
    _setup_paths(monkeypatch, tmp_path)
    bad = tmp_path / "bad.zip"
    bad.write_text("not zip", encoding="utf-8")

    result = tenant_backup.validate_import_package(bad)

    assert result.ok is False
    assert "valid ZIP" in result.errors[0]


def test_validate_import_rejects_checksum_mismatch(monkeypatch, tmp_path):
    root = _setup_paths(monkeypatch, tmp_path)
    _write_tenant(root, "lawyer")
    result = tenant_backup.create_tenant_export("lawyer")
    corrupt = tmp_path / "corrupt.zip"
    with zipfile.ZipFile(result.zip_path) as src, zipfile.ZipFile(corrupt, "w") as dst:
        for name in src.namelist():
            payload = src.read(name)
            if name == "tenant.json":
                payload = payload.replace(b"Lawyer", b"Broken")
            dst.writestr(name, payload)

    validation = tenant_backup.validate_import_package(corrupt)

    assert validation.ok is False
    assert any("Checksum mismatch: tenant.json" in err for err in validation.errors)


def test_restore_existing_creates_rollback_and_preserves_current_secrets(monkeypatch, tmp_path):
    root = _setup_paths(monkeypatch, tmp_path)
    _write_tenant(root, "source", name="Source Tenant")
    exported = tenant_backup.create_tenant_export("source")
    _write_tenant(root, "target", name="Target Tenant")

    imported = tenant_backup.import_tenant_backup(
        exported.zip_path,
        mode="restore_existing",
        target_tenant_id="target",
        confirmation="target",
    )

    assert imported.ok is True
    assert imported.rollback_path and imported.rollback_path.exists()
    data = json.loads((root / "target" / "config" / "client.json").read_text())
    assert data["slug"] == "target"
    assert data["name"] == "Source Tenant"
    assert data["password"] == "super-secret-password"
    assert data["access_key"] == "super-secret-access-key"


def test_restore_as_clone_creates_new_tenant_without_secret_markers(monkeypatch, tmp_path):
    root = _setup_paths(monkeypatch, tmp_path)
    _write_tenant(root, "source", name="Source Tenant")
    exported = tenant_backup.create_tenant_export("source")

    result = tenant_backup.import_tenant_backup(
        exported.zip_path,
        mode="clone_new",
        target_tenant_id="source",
        confirmation="clone",
        new_slug="clone",
        new_name="Clone Tenant",
    )

    assert result.ok is True
    data = json.loads((root / "clone" / "config" / "client.json").read_text())
    assert data["slug"] == "clone"
    assert data["name"] == "Clone Tenant"
    assert "password" not in data
    assert "access_key" not in data


def test_tenant_workspace_renders_backup_restore_section(monkeypatch, tmp_path):
    root = _setup_paths(monkeypatch, tmp_path)
    _write_tenant(root, "lawyer")
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert "Tenant Backup &amp; Restore" in response.text
    assert "Export tenant data" in response.text
    assert "Validate only / dry run" in response.text
