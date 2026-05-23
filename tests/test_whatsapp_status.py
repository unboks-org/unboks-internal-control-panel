import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.zernio import ZernioAccountSummary


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
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _status(client: TestClient):
    return client.get("/internal/api/tenants/lawyer/channels/whatsapp/status")


def test_whatsapp_status_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = _status(client)

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin authentication required."


def test_whatsapp_status_returns_404_for_unknown_tenant(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _status(client)

    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found."


def test_whatsapp_status_returns_not_connected(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _status(client)

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "tenantId": "lawyer",
        "channel": "whatsapp",
        "provider": "zernio",
        "status": "not_connected",
        "connected": False,
        "displayPhoneNumber": None,
        "phoneNumberId": None,
        "providerAccountId": None,
        "zernioProfileId": None,
        "connectedAt": None,
        "lastUpdatedAt": None,
        "lastError": None,
    }


def test_whatsapp_status_returns_pending(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="pending",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        last_request_id="cr_pending",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["connected"] is False
    assert payload["providerAccountId"] == "account_1"
    assert payload["zernioProfileId"] == "profile_lawyer"
    assert payload["lastUpdatedAt"]
    assert payload["lastError"] is None


def test_whatsapp_status_returns_connected(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        phone_number_id="phone_1",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_1",
        last_request_id="cr_connected",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected"
    assert payload["connected"] is True
    assert payload["displayPhoneNumber"] == "+599 9 694 5527"
    assert payload["phoneNumberId"] == "phone_1"
    assert payload["providerAccountId"] == "account_1"
    assert payload["zernioProfileId"] == "profile_lawyer"
    assert payload["connectedAt"]
    assert payload["lastUpdatedAt"]


def test_whatsapp_status_returns_failed(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        last_request_id="cr_failed",
        last_error="Client denied authorization.",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["connected"] is False
    assert payload["lastError"] == "Client denied authorization."


def test_whatsapp_status_reconciles_connected_zernio_account(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_missed_callback",
        status="link_generated",
    ).request

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return [
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Lawyer WhatsApp",
                    username="+599 9 694 5527",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 694 5527",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected"
    assert payload["connected"] is True
    assert payload["providerAccountId"] == "account_1"
    assert payload["displayPhoneNumber"] == "+599 9 694 5527"
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
