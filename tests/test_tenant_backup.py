import io
import json
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import channel_connections, channel_state, icp_overrides, tenant_notes
from app.main import app
from app.provisioning import AutoProvisionResult
from app.tenant_backup import (
    _canonical_docker_compose_text,
    build_export_package,
    import_uploaded_package,
    validate_import_package,
)
from app.tenants import register_tenant


def _seed(monkeypatch, tmp_path, slug="acme"):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp_overrides.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channel_state.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "tenant_notes.json"))
    monkeypatch.setenv("NR3_TENANT_EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv("NR3_TENANT_IMPORT_ROLLBACK_DIR", str(tmp_path / "rollbacks"))
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(tmp_path / "claims.json"))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    tenant_root = tmp_path / "clients" / slug
    config_root = tenant_root / "config"
    data_root = tenant_root / "data" / "knowledge"
    config_root.mkdir(parents=True)
    data_root.mkdir(parents=True)
    (config_root / "client.json").write_text(
        json.dumps(
            {
                "slug": slug,
                "name": "Acme Co",
                "email": "owner@example.com",
                "password": "do-not-export",
                "access_key": "do-not-export",
                "whatsapp_connect_token": "donor-token",
                "channel_account_allowlist": {"zernio_accounts": ["donor-account"]},
                "zernio_account_id": "donor-account",
                "phone_number_id": "donor-phone",
            }
        ),
        encoding="utf-8",
    )
    (config_root / "platform.env").write_text(
        f"TENANT_ID={slug}\nTENANT_SLUG={slug}\nDASHBOARD_PASSWORD=do-not-export\n",
        encoding="utf-8",
    )
    (tenant_root / "docker-compose.yml").write_text(
        _canonical_docker_compose_text(slug, 8123),
        encoding="utf-8",
    )
    (data_root / "sot.txt").write_text("runtime knowledge", encoding="utf-8")
    register_tenant({"slug": slug, "name": "Acme Co", "status": "active"})
    icp_overrides.set_ai_tone(slug, "Warm", notes="Helpful")
    icp_overrides.set_agent_name_override(slug, "Sofia")
    icp_overrides.add_sot_entry(slug, title="Hours", content="Open 9-5", category="hours")
    channel_state.set_channel(slug, "email", True)
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
        assert "client_tree/config/client.json" in names
        assert "client_tree/config/platform.env" in names
        assert "client_tree/data/knowledge/sot.txt" in names
        tenant = json.loads(zf.read("tenant.json"))
        assert tenant["client_json_sanitized"]["password"]["excluded"] is True
        assert tenant["client_json_sanitized"]["access_key"]["excluded"] is True
        raw_client = json.loads(zf.read("client_tree/config/client.json"))
        assert raw_client["password"] == "do-not-export"
        prompts = json.loads(zf.read("prompts.json"))
        assert prompts["ai_agent_settings"]["agent_name"]["name"] == "Sofia"

    summary = validate_import_package(package)
    assert summary["tenant_slug"] == slug
    assert summary["partial"] is False
    assert summary["complete_clone"] is True
    assert summary["client_tree_included"] is True
    assert package.name.endswith(".unboksbackup")


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
    from app.port_registry import reserve_tenant_port
    from app.provisioning import create_tenant_provision_claim

    slug = _seed(monkeypatch, tmp_path)
    package = build_export_package(slug)
    creation_id = "clone-creation-acme"
    host_port = reserve_tenant_port("acme-clone")
    assert create_tenant_provision_claim("acme-clone", creation_id) is True

    with pytest.raises(ValueError, match="trusted reserved host port"):
        import_uploaded_package(
            package.open("rb"),
            target_tenant=slug,
            mode="clone",
            new_slug="acme-clone",
            confirmation="IMPORT CLONE",
            clone_creation_id=creation_id,
            trusted_clone_host_port=80,
        )

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant=slug,
        mode="clone",
        new_slug="acme-clone",
        confirmation="IMPORT CLONE",
        clone_creation_id=creation_id,
        trusted_clone_host_port=host_port,
    )

    assert result["status"] == "imported"
    assert result["target_tenant"] == "acme-clone"
    assert channel_state.read_channels("acme-clone")["whatsapp"] is False
    assert icp_overrides.ai_agent_settings_for_tenant("acme-clone")["agent_name"]["name"] == "Sofia"
    assert tenant_notes.list_notes("acme-clone")[0].body == "Important internal note"
    clone_root = tmp_path / "clients" / "acme-clone"
    clone_client = json.loads((clone_root / "config" / "client.json").read_text(encoding="utf-8"))
    assert clone_client["slug"] == "acme-clone"
    assert clone_client["creation_id"] == creation_id
    assert clone_client["whatsapp_connect_token"] == ""
    assert clone_client["channel_account_allowlist"] == {}
    assert clone_client["zernio_account_id"] == ""
    assert clone_client["phone_number_id"] == ""
    assert "TENANT_ID=acme-clone" in (clone_root / "config" / "platform.env").read_text(encoding="utf-8")
    assert "container_name: wtyj-acme-clone" in (clone_root / "docker-compose.yml").read_text(encoding="utf-8")


def test_import_restore_replaces_existing_nr3_state(monkeypatch, tmp_path):
    source = _seed(monkeypatch, tmp_path, slug="source")
    _seed(monkeypatch, tmp_path, slug="target")
    target_root = tmp_path / "clients" / "target"
    old_file = target_root / "data" / "knowledge" / "old.txt"
    old_file.write_text("old runtime data", encoding="utf-8")
    (target_root / "docker-compose.yml").write_text(
        _canonical_docker_compose_text("target", 8999),
        encoding="utf-8",
    )
    package = build_export_package(source)

    icp_overrides.add_sot_entry("target", title="Old", content="Delete me", category="general")
    tenant_notes.add_note("target", "Old note")
    channel_state.set_channel("target", "email", True)

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant="target",
        mode="restore",
        confirmation="target",
    )

    assert result["status"] == "imported"
    assert result["target_tenant"] == "target"
    sot_titles = [entry["title"] for entry in icp_overrides.sot_entries_for_tenant("target")]
    assert "Hours" in sot_titles
    assert "Old" not in sot_titles
    assert channel_state.read_channels("target")["whatsapp"] is False
    assert channel_state.read_channels("target")["email"] is True
    notes = [note.body for note in tenant_notes.list_notes("target")]
    assert notes == ["Important internal note"]
    restored_client = json.loads((target_root / "config" / "client.json").read_text(encoding="utf-8"))
    assert restored_client["slug"] == "target"
    assert restored_client["password"] == "do-not-export"
    assert restored_client["whatsapp_connect_token"] == ""
    assert restored_client["channel_account_allowlist"] == {}
    assert (target_root / "data" / "knowledge" / "sot.txt").read_text(encoding="utf-8") == "runtime knowledge"
    assert not old_file.exists()
    restored_env = (target_root / "config" / "platform.env").read_text(encoding="utf-8")
    assert "TENANT_ID=target" in restored_env
    assert "TENANT_SLUG=target" in restored_env
    assert "DASHBOARD_PASSWORD=do-not-export" in restored_env
    assert "container_name: wtyj-target" in (target_root / "docker-compose.yml").read_text(encoding="utf-8")
    assert result["client_tree_restored"] is True
    assert result["channels_require_reconnect"] is True


def test_import_restore_can_defer_runtime_restore_to_host_worker(monkeypatch, tmp_path):
    source = _seed(monkeypatch, tmp_path, slug="source")
    _seed(monkeypatch, tmp_path, slug="target")
    monkeypatch.setenv("NR3_TENANT_RUNTIME_RESTORE_MODE", "host")
    package = build_export_package(source)

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant="target",
        mode="restore",
        confirmation="target",
    )

    assert result["status"] == "imported"
    assert result["client_tree_restored"] is False
    assert result["runtime_restore_package"].endswith(".unboksbackup")
    assert Path(result["runtime_restore_package"]).exists()
    assert icp_overrides.ai_agent_settings_for_tenant("target")["agent_name"]["name"] == "Sofia"


def test_import_restore_same_tenant_carries_channel_connection_metadata(monkeypatch, tmp_path):
    source = _seed(monkeypatch, tmp_path, slug="source")
    channel_connections.upsert_tenant_channel_connection(
        tenant_id=source,
        status="connected",
        zernio_profile_id="profile-123",
        zernio_account_id="account-123",
        phone_number_id="phone-123",
        display_phone_number="+599 123",
        waba_id="waba-123",
        metadata={"channel_account_allowlist": ["account-123"]},
    )
    package = build_export_package(source)

    import_uploaded_package(
        package.open("rb"),
        target_tenant=source,
        mode="restore",
        confirmation=source,
    )

    restored = channel_connections.get_tenant_channel_connection(source)
    assert restored is not None
    assert restored.status == "connected"
    assert restored.zernio_profile_id == "profile-123"
    assert restored.zernio_account_id == "account-123"
    assert restored.phone_number_id == "phone-123"
    assert json.loads(restored.metadata_json)["channel_account_allowlist"] == ["account-123"]


def test_import_restore_cross_tenant_requires_provider_reconnect(monkeypatch, tmp_path):
    source = _seed(monkeypatch, tmp_path, slug="source")
    _seed(monkeypatch, tmp_path, slug="target")
    channel_connections.upsert_tenant_channel_connection(
        tenant_id=source,
        status="connected",
        zernio_profile_id="profile-123",
        zernio_account_id="account-123",
        phone_number_id="phone-123",
        display_phone_number="+599 123",
        waba_id="waba-123",
        metadata={"channel_account_allowlist": ["account-123"]},
    )
    package = build_export_package(source)

    result = import_uploaded_package(
        package.open("rb"),
        target_tenant="target",
        mode="restore",
        confirmation="target",
    )

    assert result["channels_require_reconnect"] is True
    assert channel_connections.get_tenant_channel_connection("target") is None
    assert channel_state.read_channels("target")["whatsapp"] is False


def test_workspace_renders_backup_restore_section(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, slug="unboks")
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "Full Tenant Backup & Restore" in response.text
    assert "tenant runtime folder" in response.text
    assert "Export one backup file" in response.text
    assert "Import backup file" in response.text
    assert "Validate / import configuration" not in response.text
    assert "/admin/tenants/unboks/backup/export" in response.text
    assert "/admin/tenants/unboks/backup/import" in response.text


def test_stale_restore_form_cannot_overwrite_recreated_tenant(monkeypatch, tmp_path):
    slug = _seed(monkeypatch, tmp_path, slug="target")
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    from app.channel_connections import current_tenant_generation_id
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock

    old_generation = current_tenant_generation_id(slug)
    operation = start_delete_operation(
        slug=slug,
        tenant_generation_id=old_generation,
        generation_fingerprint="sha256:" + "6" * 64,
        account_ids=[],
        profile_ids=[],
    )
    update_delete_operation(
        slug=slug,
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    with tenant_creation_lock(slug):
        bind_tenant_generation_for_creation(
            slug=slug,
            generation_id="replacement-generation",
            status="active",
        )

    side_effects: list[str] = []
    monkeypatch.setattr(
        "app.tenant_backup.import_uploaded_package",
        lambda *_args, **_kwargs: side_effects.append("import"),
    )
    monkeypatch.setattr(
        "app.routes.admin.queue_tenant_host_action",
        lambda **_kwargs: side_effects.append("queue"),
    )
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.post(
        f"/admin/tenants/{slug}/backup/import",
        data={
            "import_mode": "restore",
            "confirmation": slug,
            "tenant_generation_id": old_generation,
        },
        files={"backup_file": ("backup.unboksbackup", b"stale", "application/zip")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "blocked" in response.headers["location"].lower()
    assert side_effects == []


def test_restore_holds_lifecycle_lease_through_host_queue(monkeypatch, tmp_path):
    slug = _seed(monkeypatch, tmp_path, slug="target")
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    from app.channel_connections import current_tenant_generation_id
    from app.provisioning import tenant_creation_lock

    generation_id = current_tenant_generation_id(slug)
    contender_acquired = threading.Event()
    contender_started = threading.Event()
    contender: threading.Thread | None = None

    def fake_import(*_args, **_kwargs):
        nonlocal contender

        def contend_for_lifecycle_lock():
            contender_started.set()
            with tenant_creation_lock(slug):
                contender_acquired.set()

        contender = threading.Thread(target=contend_for_lifecycle_lock)
        contender.start()
        assert contender_started.wait(timeout=1)
        assert not contender_acquired.wait(timeout=0.05)
        return {
            "status": "imported",
            "target_tenant": slug,
            "runtime_restore_package": "",
            "host_port": None,
            "channels_require_reconnect": False,
            "verified_zernio_account_id": "",
            "creation_id": "",
            "client_tree_restored": True,
            "rollback_package": "/safe/rollback.unboksbackup",
        }

    def fake_queue(**_kwargs):
        assert not contender_acquired.is_set()
        return AutoProvisionResult(
            status="disabled",
            message="test worker disabled",
        )

    monkeypatch.setattr("app.tenant_backup.import_uploaded_package", fake_import)
    monkeypatch.setattr("app.routes.admin.queue_tenant_host_action", fake_queue)
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.post(
        f"/admin/tenants/{slug}/backup/import",
        data={
            "import_mode": "restore",
            "confirmation": slug,
            "tenant_generation_id": generation_id,
        },
        files={"backup_file": ("backup.unboksbackup", b"valid", "application/zip")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert contender is not None
    contender.join(timeout=1)
    assert not contender.is_alive()
    assert contender_acquired.is_set()
