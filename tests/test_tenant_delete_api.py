import json
import shutil

import pytest
from fastapi.testclient import TestClient

from app import channel_connections
from app.channel_connections import current_tenant_generation_id
from app.delete_operations import (
    load_delete_operation,
    read_tenant_generation,
    start_delete_operation,
    update_delete_operation,
)
from app.main import app
from app.provisioning import AutoProvisionResult, reconcile_host_action_results
from app.tenants import register_tenant
from app.zernio import ZernioAccountSummary


def _write_tenant(root, slug="lawyer", name="Lawyer"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def _client(monkeypatch, tmp_path):
    from app.routes.tenant_api import _DELETE_ATTEMPTS

    _DELETE_ATTEMPTS.clear()
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv(
        "NR3_DELETE_OPERATIONS_DIR", str(tmp_path / "delete-operations")
    )
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _delete(
    client: TestClient,
    slug="lawyer",
    typed_slug="lawyer",
    final="DELETE FOREVER",
    generation_id=None,
):
    if generation_id is None:
        operation = load_delete_operation(slug)
        generation_id = (
            operation["tenant_generation_id"]
            if operation is not None
            else current_tenant_generation_id(slug)
        )
    return client.request(
        "DELETE",
        f"/internal/api/tenants/{slug}",
        json={
            "typedSlug": typed_slug,
            "finalConfirmation": final,
            "tenantGenerationId": generation_id,
        },
    )


def test_delete_tenant_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = _delete(client)

    assert response.status_code == 401


def test_delete_tenant_rejects_reserved_unboks(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root, "unboks", "Unboks")
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _delete(client, slug="unboks", typed_slug="unboks")

    assert response.status_code == 403
    assert response.json()["detail"] == "The master Unboks tenant cannot be deleted."


def test_delete_tenant_requires_exact_slug_and_final_text(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    bad_slug = _delete(client, typed_slug="Lawyer")
    bad_final = _delete(client, final="delete forever")

    assert bad_slug.status_code == 400
    assert bad_slug.json()["detail"] == "Typed tenant slug does not match exactly."
    assert bad_final.status_code == 400
    assert bad_final.json()["detail"] == "Final confirmation text is invalid."


def test_delete_tenant_refuses_when_worker_disabled(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _delete(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "Tenant delete worker is disabled. No tenant was deleted."


def test_delete_waits_for_verified_backup_before_provider_cleanup(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        zernio_account_verified=True,
    )
    calls = []

    def fake_queue(**kwargs):
        calls.append(kwargs)
        return AutoProvisionResult(
            status="queued",
            message="Backup worker still running.",
            job_id="job_prepare",
        )

    class UnexpectedZernioService:
        def __init__(self):
            raise AssertionError("provider cleanup must wait for verified backup")

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)
    monkeypatch.setattr("app.routes.tenant_api.ZernioService", UnexpectedZernioService)

    response = _delete(client)

    assert response.status_code == 200
    assert response.json()["status"] == "backup_pending"
    assert response.json()["jobId"].endswith("-prepare-1")
    assert [call["action"] for call in calls] == ["prepare_delete_tenant"]
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.zernio_account_id == "account_lawyer"


def test_delete_tenant_queues_host_action_and_cleans_local_state(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        zernio_account_verified=True,
    )
    calls = []
    deleted_accounts = []
    deleted_profiles = []

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return []

        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_lawyer",
                profile_name="Lawyer",
                display_name="Lawyer",
                username="+599 1",
                display_phone_number="+599 1",
                phone_number_id="phone_lawyer",
                waba_id="waba_lawyer",
                enabled=True,
                is_active=True,
                platform_status="active",
            )

        def delete_account(self, account_id):
            deleted_accounts.append(account_id)
            return {"success": True}

        def delete_profile(self, profile_id):
            deleted_profiles.append(profile_id)
            return {"success": True}

    def fake_queue(**kwargs):
        calls.append(dict(kwargs))
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="Tenant backup verified.",
                job_id=kwargs["requested_job_id"],
                details=("backup created at /root/_deleted_tenants/lawyer-ts",),
                backup_path="/root/_deleted_tenants/lawyer-ts",
                backup_digest="sha256:backup-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        shutil.rmtree(tenants_root / "lawyer")
        return AutoProvisionResult(
            status="succeeded",
            message="Tenant lawyer was permanently deleted on the VPS.",
            job_id=kwargs["requested_job_id"],
            details=("runtime removed",),
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=True,
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)
    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FakeZernioService)

    response = _delete(client)

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert response.json()["jobId"].endswith("-delete-1")
    assert calls[1] == {
        "slug": "lawyer",
        "action": "delete_tenant",
        "dashboard_url": "https://dashboard.unboks.org/lawyer",
        "typed_slug": "lawyer",
        "final_confirmation": "DELETE FOREVER",
        "requested_job_id": calls[1]["requested_job_id"],
        "delete_operation_id": calls[1]["delete_operation_id"],
        "generation_fingerprint": calls[1]["generation_fingerprint"],
        "prepared_backup_path": "/root/_deleted_tenants/lawyer-ts",
        "prepared_backup_digest": "sha256:backup-proof",
    }
    assert deleted_accounts == ["account_lawyer"]
    assert deleted_profiles == ["profile_lawyer"]
    assert "zernio account disconnected: account_lawyer" in response.json()["details"]
    assert "zernio profile deleted: profile_lawyer" in response.json()["details"]
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") is None
    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="no longer belongs",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="lawyer",
            status="connected",
            zernio_profile_id="profile_late",
            zernio_account_id="account_late",
            zernio_account_verified=True,
            last_request_id="deleted_request",
        )


def test_delete_tenant_blocks_when_zernio_cleanup_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        zernio_account_verified=True,
    )
    queued = []

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return []

        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_lawyer",
                profile_name="Lawyer",
                display_name="Lawyer",
                username="+599 1",
                display_phone_number="+599 1",
                phone_number_id="phone_lawyer",
                waba_id="waba_lawyer",
                enabled=True,
                is_active=True,
                platform_status="active",
            )

        def delete_account(self, account_id):
            from app.zernio import ZernioAPIError

            raise ZernioAPIError(401, "Unauthorized")

        def delete_profile(self, profile_id):
            raise AssertionError("profile cleanup should not run after account failure")

    def fake_queue(**kwargs):
        queued.append(kwargs["action"])
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="Tenant backup verified.",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-ts",
                backup_digest="sha256:backup-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        raise AssertionError("host delete must not publish after cleanup fails")

    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FakeZernioService)
    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    response = _delete(client)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Zernio cleanup failed, so tenant deletion was blocked. "
        "Retry after fixing the provider connection."
    )
    assert queued == ["prepare_delete_tenant"]
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.zernio_profile_id == "profile_lawyer"
    assert connection.zernio_account_id == "account_lawyer"


def test_delete_tenant_never_deletes_account_from_another_provider_profile(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client_path = tenants_root / "lawyer" / "config" / "client.json"
    client_data = json.loads(client_path.read_text(encoding="utf-8"))
    client_data["channel_account_allowlist"] = {
        "mode": "strict",
        "zernio_accounts": ["account_other_tenant"],
    }
    client_path.write_text(json.dumps(client_data), encoding="utf-8")
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    deleted = []

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return []

        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_other_tenant",
                profile_name="Other",
                display_name="Other",
                username="+599 2",
                display_phone_number="+599 2",
                phone_number_id="phone_other",
                waba_id="waba_other",
                enabled=True,
                is_active=True,
                platform_status="active",
            )

        def delete_account(self, account_id):
            deleted.append(account_id)

        def delete_profile(self, profile_id):
            deleted.append(profile_id)

    def fake_queue(**kwargs):
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="Tenant backup verified.",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-ts",
                backup_digest="sha256:backup-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        raise AssertionError("A provider ownership failure must not publish a job")

    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FakeZernioService)
    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    response = _delete(client)

    assert response.status_code == 409
    assert "ownership could not be verified" in response.json()["detail"]
    assert deleted == []


def test_delete_resumes_same_preparation_job_after_request_timeout(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    prepare_ids = []

    def fake_queue(**kwargs):
        if kwargs["action"] == "prepare_delete_tenant":
            prepare_ids.append(kwargs["requested_job_id"])
            if len(prepare_ids) == 1:
                return AutoProvisionResult(
                    status="queued",
                    message="backup running",
                    job_id=kwargs["requested_job_id"],
                )
            return AutoProvisionResult(
                status="succeeded",
                message="backup ready",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-resume",
                backup_digest="sha256:resume-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        shutil.rmtree(tenants_root / "lawyer")
        return AutoProvisionResult(
            status="succeeded",
            message="deleted",
            job_id=kwargs["requested_job_id"],
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=True,
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    first = _delete(client)
    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="deletion is in progress",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="lawyer",
            status="connected",
            zernio_profile_id="profile_late",
            zernio_account_id="account_late",
            zernio_account_verified=True,
        )
    second = _delete(client)

    assert first.status_code == 200
    assert first.json()["status"] == "backup_pending"
    assert second.status_code == 200
    assert second.json()["status"] == "deleted"
    assert len(prepare_ids) == 2
    assert prepare_ids[0] == prepare_ids[1]


def test_delete_generation_change_blocks_resume_before_provider_cleanup(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    calls = []

    def fake_queue(**kwargs):
        calls.append(kwargs["action"])
        return AutoProvisionResult(
            status="queued",
            message="backup running",
            job_id=kwargs["requested_job_id"],
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    first = _delete(client)
    client_path = tenants_root / "lawyer" / "config" / "client.json"
    changed = json.loads(client_path.read_text(encoding="utf-8"))
    changed["name"] = "Replacement generation"
    client_path.write_text(json.dumps(changed), encoding="utf-8")
    second = _delete(client)

    assert first.json()["status"] == "backup_pending"
    assert second.status_code == 409
    assert "generation changed" in second.json()["detail"]
    assert calls == ["prepare_delete_tenant"]


def test_delete_blocks_unreadable_client_config_before_any_job(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client_path = tenants_root / "lawyer" / "config" / "client.json"
    client_path.write_text("{broken", encoding="utf-8")
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})

    monkeypatch.setattr(
        "app.routes.tenant_api.queue_tenant_host_action",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("no job may be queued without valid client.json")
        ),
    )

    response = _delete(client)

    assert response.status_code == 503
    assert "client.json is unreadable" in response.json()["detail"]


def test_delete_retries_idempotent_provider_cleanup_without_repreparing(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        zernio_account_verified=True,
    )
    prepare_calls = []
    delete_account_calls = []

    class FlakyZernioService:
        def list_accounts(self, *, platform=None):
            return []

        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_lawyer",
                profile_name="Lawyer",
                display_name="Lawyer",
                username="+599 1",
                display_phone_number="+599 1",
                phone_number_id="phone_lawyer",
                waba_id="waba_lawyer",
                enabled=True,
                is_active=True,
                platform_status="active",
            )

        def delete_account(self, account_id):
            delete_account_calls.append(account_id)
            if len(delete_account_calls) == 1:
                from app.zernio import ZernioAPIError

                raise ZernioAPIError(503, "temporary failure")
            return {"success": True}

        def delete_profile(self, profile_id):
            return {"success": True}

    def fake_queue(**kwargs):
        if kwargs["action"] == "prepare_delete_tenant":
            prepare_calls.append(kwargs["requested_job_id"])
            return AutoProvisionResult(
                status="succeeded",
                message="backup ready",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-provider-retry",
                backup_digest="sha256:provider-retry-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        shutil.rmtree(tenants_root / "lawyer")
        return AutoProvisionResult(
            status="succeeded",
            message="deleted",
            job_id=kwargs["requested_job_id"],
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=True,
        )

    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FlakyZernioService)
    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    first = _delete(client)
    pending = load_delete_operation("lawyer")
    second = _delete(client)

    assert first.status_code == 502
    assert pending is not None
    assert pending["phase"] == "provider_cleanup_failed"
    assert second.status_code == 200
    assert second.json()["status"] == "deleted"
    assert len(prepare_calls) == 1
    assert delete_account_calls == ["account_lawyer", "account_lawyer"]


def test_delete_never_forgets_local_state_without_explicit_safe_release(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})

    def fake_queue(**kwargs):
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="backup ready",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-unsafe",
                backup_digest="sha256:unsafe-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        return AutoProvisionResult(
            status="succeeded",
            message="legacy success without proof",
            job_id=kwargs["requested_job_id"],
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=False,
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    response = _delete(client)
    operation = load_delete_operation("lawyer")

    assert response.status_code == 502
    assert "safe-release" in response.json()["detail"]
    assert operation is not None
    assert operation["phase"] == "delete_failed"
    assert (tenants_root / "lawyer").is_dir()
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") is None


def test_reused_slug_starts_a_new_generation_bound_delete_operation(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root, name="Original")
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Original", "status": "active"})
    operation_ids = []

    def fake_queue(**kwargs):
        operation_ids.append(kwargs["delete_operation_id"])
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="backup ready",
                job_id=kwargs["requested_job_id"],
                backup_path=f"/root/_deleted_tenants/{kwargs['delete_operation_id']}",
                backup_digest=f"sha256:{kwargs['delete_operation_id']}",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        shutil.rmtree(tenants_root / "lawyer")
        return AutoProvisionResult(
            status="succeeded",
            message="deleted",
            job_id=kwargs["requested_job_id"],
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=True,
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    first = _delete(client)
    from app.delete_operations import bind_tenant_generation_for_creation
    from app.provisioning import tenant_creation_lock

    with tenant_creation_lock("lawyer"):
        bind_tenant_generation_for_creation(
            slug="lawyer",
            generation_id="replacement-generation",
            status="active",
        )
    _write_tenant(tenants_root, name="Replacement")
    register_tenant({"slug": "lawyer", "name": "Replacement", "status": "active"})
    second = _delete(client)

    assert first.json()["status"] == second.json()["status"] == "deleted"
    assert operation_ids[0] == operation_ids[1]
    assert operation_ids[2] == operation_ids[3]
    assert operation_ids[0] != operation_ids[2]
    history = list((tmp_path / "delete-operations" / "history").glob("*.json"))
    assert len(history) == 1


def test_async_delete_reconciliation_requires_correlated_safe_release_proof(
    monkeypatch, tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(tmp_path / "reconciled"))
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    _, fingerprint = read_tenant_generation("lawyer")
    generation_id = current_tenant_generation_id("lawyer")
    operation = start_delete_operation(
        slug="lawyer",
        tenant_generation_id=generation_id,
        generation_fingerprint=fingerprint,
        account_ids=[],
        profile_ids=[],
    )
    operation = update_delete_operation(
        slug="lawyer",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="prepared",
        prepare_backup_path="/root/_deleted_tenants/lawyer-async",
        prepare_backup_digest="sha256:async-proof",
    )
    operation = update_delete_operation(
        slug="lawyer",
        operation_id=operation["operation_id"],
        expected_phases={"prepared"},
        phase="provider_cleaned",
    )
    operation = update_delete_operation(
        slug="lawyer",
        operation_id=operation["operation_id"],
        expected_phases={"provider_cleaned"},
        phase="delete_queued",
    )
    shutil.rmtree(tenants_root / "lawyer")
    results = tmp_path / "results"
    results.mkdir()
    result_path = results / f"{operation['delete_job_id']}.json"
    result_payload = {
        "job_id": operation["delete_job_id"],
        "job_type": "tenant_action",
        "action": "delete_tenant",
        "slug": "lawyer",
        "status": "succeeded",
        "safe_to_release": False,
        "delete_operation_id": operation["operation_id"],
        "generation_fingerprint": fingerprint,
        "backup_path": operation["prepare_backup_path"],
        "backup_digest": operation["prepare_backup_digest"],
        "prepared_backup_path": operation["prepare_backup_path"],
        "prepared_backup_digest": operation["prepare_backup_digest"],
        "details": ["runtime absence proved"],
    }
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")

    assert reconcile_host_action_results() == 0
    assert load_delete_operation("lawyer")["phase"] == "delete_queued"

    result_payload["safe_to_release"] = True
    result_path.write_text(json.dumps(result_payload), encoding="utf-8")

    assert reconcile_host_action_results() == 1
    assert load_delete_operation("lawyer")["phase"] == "deleted"
    assert reconcile_host_action_results() == 0


def test_local_cleanup_failure_keeps_completed_host_delete_quarantined(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})

    def fake_queue(**kwargs):
        if kwargs["action"] == "prepare_delete_tenant":
            return AutoProvisionResult(
                status="succeeded",
                message="backup ready",
                job_id=kwargs["requested_job_id"],
                backup_path="/root/_deleted_tenants/lawyer-local-failure",
                backup_digest="sha256:local-failure-proof",
                operation_id=kwargs["delete_operation_id"],
                generation_fingerprint=kwargs["generation_fingerprint"],
            )
        shutil.rmtree(tenants_root / "lawyer")
        return AutoProvisionResult(
            status="succeeded",
            message="host deleted",
            job_id=kwargs["requested_job_id"],
            backup_path=kwargs["prepared_backup_path"],
            backup_digest=kwargs["prepared_backup_digest"],
            prepared_backup_path=kwargs["prepared_backup_path"],
            prepared_backup_digest=kwargs["prepared_backup_digest"],
            operation_id=kwargs["delete_operation_id"],
            generation_fingerprint=kwargs["generation_fingerprint"],
            safe_to_release=True,
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)
    monkeypatch.setattr(
        "app.routes.tenant_api.forget_tenant_state_strict",
        lambda slug: (_ for _ in ()).throw(RuntimeError("disk failure")),
    )

    response = _delete(client)
    operation = load_delete_operation("lawyer")

    assert response.status_code == 503
    assert "remains quarantined" in response.json()["detail"]
    assert operation is not None
    assert operation["phase"] == "delete_queued"


def _seed_delete_ledger(*, phase: str) -> dict:
    operation = start_delete_operation(
        slug="lawyer",
        tenant_generation_id="generation-delete-ledger",
        generation_fingerprint="sha256:" + "f" * 64,
        account_ids=[],
        profile_ids=[],
    )
    if phase != "preparing":
        operation = update_delete_operation(
            slug="lawyer",
            operation_id=operation["operation_id"],
            expected_phases={"preparing"},
            phase=phase,
        )
    return operation


def test_missing_tenant_with_nonterminal_delete_is_explicitly_quarantined(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    (tmp_path / "tenants").mkdir()
    _login(client)
    operation = _seed_delete_ledger(phase="delete_queued")

    response = _delete(client)

    assert response.status_code == 503
    assert operation["operation_id"] in response.json()["detail"]
    assert operation["delete_job_id"] in response.json()["detail"]
    assert "quarantined and retryable" in response.json()["detail"]


def test_completed_delete_requires_real_mounted_root_before_idempotent_success(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    _login(client)
    _seed_delete_ledger(phase="deleted")

    response = _delete(client)

    assert response.status_code == 409
    assert "runtime absence cannot be proved" in response.json()["detail"]


def test_completed_delete_treats_dangling_slug_symlink_as_present(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    tenants_root = tmp_path / "tenants"
    tenants_root.mkdir()
    (tenants_root / "lawyer").symlink_to(tmp_path / "missing-runtime")
    _login(client)
    _seed_delete_ledger(phase="deleted")

    response = _delete(client)

    assert response.status_code == 409
    assert "slug remains quarantined" in response.json()["detail"]


def test_stale_rendered_generation_cannot_delete_recreated_tenant(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    old_generation = current_tenant_generation_id("lawyer")
    _, old_fingerprint = read_tenant_generation("lawyer")
    operation = start_delete_operation(
        slug="lawyer",
        tenant_generation_id=old_generation,
        generation_fingerprint=old_fingerprint,
        account_ids=[],
        profile_ids=[],
    )
    update_delete_operation(
        slug="lawyer",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    from app.delete_operations import bind_tenant_generation_for_creation
    from app.provisioning import tenant_creation_lock

    with tenant_creation_lock("lawyer"):
        bind_tenant_generation_for_creation(
            slug="lawyer",
            generation_id="replacement-generation",
            status="active",
        )
    client_path = tenants_root / "lawyer" / "config" / "client.json"
    client_path.write_text(
        json.dumps({
            "slug": "lawyer",
            "name": "Replacement",
            "status": "active",
            "tenant_generation_id": "replacement-generation",
        }),
        encoding="utf-8",
    )

    side_effects: list[str] = []
    monkeypatch.setattr(
        "app.routes.tenant_api.queue_tenant_host_action",
        lambda **_kwargs: side_effects.append("queue"),
    )
    monkeypatch.setattr(
        "app.routes.tenant_api._collect_tenant_zernio_cleanup_ids",
        lambda *_args, **_kwargs: side_effects.append("provider-read"),
    )

    response = _delete(client, generation_id=old_generation)

    assert response.status_code == 409
    assert "stale" in response.json()["detail"].lower()
    assert side_effects == []
    assert load_delete_operation("lawyer") is None


def test_delete_resume_requires_same_confirmed_generation(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    current_generation = current_tenant_generation_id("lawyer")
    _, fingerprint = read_tenant_generation("lawyer")
    operation = start_delete_operation(
        slug="lawyer",
        tenant_generation_id=current_generation,
        generation_fingerprint=fingerprint,
        account_ids=[],
        profile_ids=[],
    )
    queued: list[dict] = []
    monkeypatch.setattr(
        "app.routes.tenant_api.queue_tenant_host_action",
        lambda **kwargs: queued.append(kwargs),
    )

    response = _delete(client, generation_id="different-generation")

    assert response.status_code == 409
    assert "stale" in response.json()["detail"].lower()
    assert queued == []
    assert load_delete_operation("lawyer")["operation_id"] == operation["operation_id"]
