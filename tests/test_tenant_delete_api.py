import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.provisioning import AutoProvisionResult
from app.tenants import register_tenant


def _write_tenant(root, slug="lawyer", name="Lawyer"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _delete(client: TestClient, slug="lawyer", typed_slug="lawyer", final="DELETE FOREVER"):
    return client.request(
        "DELETE",
        f"/internal/api/tenants/{slug}",
        json={"typedSlug": typed_slug, "finalConfirmation": final},
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
    )
    calls = {}
    deleted_accounts = []
    deleted_profiles = []

    class FakeZernioService:
        def delete_account(self, account_id):
            deleted_accounts.append(account_id)
            return {"success": True}

        def delete_profile(self, profile_id):
            deleted_profiles.append(profile_id)
            return {"success": True}

    def fake_queue(**kwargs):
        calls.update(kwargs)
        return AutoProvisionResult(
            status="succeeded",
            message="Tenant lawyer was permanently deleted on the VPS.",
            job_id="job_delete",
            details=("backup created at /root/_deleted_tenants/lawyer-ts",),
        )

    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)
    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FakeZernioService)

    response = _delete(client)

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert response.json()["jobId"] == "job_delete"
    assert calls == {
        "slug": "lawyer",
        "action": "delete_tenant",
        "dashboard_url": "https://dashboard.unboks.org/lawyer",
        "typed_slug": "lawyer",
        "final_confirmation": "DELETE FOREVER",
    }
    assert deleted_accounts == ["account_lawyer"]
    assert deleted_profiles == ["profile_lawyer"]
    assert "zernio account disconnected: account_lawyer" in response.json()["details"]
    assert "zernio profile deleted: profile_lawyer" in response.json()["details"]
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") is None


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
    )
    queued = []

    class FakeZernioService:
        def delete_account(self, account_id):
            from app.zernio import ZernioAPIError

            raise ZernioAPIError(401, "Unauthorized")

        def delete_profile(self, profile_id):
            raise AssertionError("profile cleanup should not run after account failure")

    def fake_queue(**kwargs):
        queued.append(kwargs)
        raise AssertionError("host delete must not queue when Zernio cleanup fails")

    monkeypatch.setattr("app.routes.tenant_api.ZernioService", FakeZernioService)
    monkeypatch.setattr("app.routes.tenant_api.queue_tenant_host_action", fake_queue)

    response = _delete(client)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Zernio cleanup failed, so tenant deletion was blocked. "
        "Retry after fixing the provider connection."
    )
    assert queued == []
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.zernio_profile_id == "profile_lawyer"
    assert connection.zernio_account_id == "account_lawyer"
