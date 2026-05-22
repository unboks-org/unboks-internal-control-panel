import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.zernio import ZernioAPIError, ZernioConnectUrl, ZernioProfile


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
    monkeypatch.setenv(
        "UNBOKS_ADMIN_API_URL",
        "https://api.unboks.org/internal/api",
    )
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_whatsapp_connect_start_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin authentication required."


def test_whatsapp_connect_start_returns_404_for_unknown_tenant(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = client.post(
        "/internal/api/tenants/missing/channels/whatsapp/connect/start"
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found."


def test_whatsapp_connect_start_creates_profile_and_stores_request(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    calls: dict[str, object] = {}

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            calls["profile"] = {
                "name": name,
                "description": description,
                "color": color,
            }
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            calls["connect"] = {
                "platform": platform,
                "profile_id": profile_id,
                "redirect_url": redirect_url,
                "headless": headless,
            }
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/lawyer",
                state="zernio_state_123",
            )

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "success": True,
        "tenantId": "lawyer",
        "authUrl": "https://facebook.com/connect/lawyer",
        "status": "link_generated",
        "expiresAt": payload["expiresAt"],
        "requestId": payload["requestId"],
    }
    assert payload["expiresAt"]
    assert payload["requestId"].startswith("cr_")
    assert "zernio_state_123" not in json.dumps(payload)
    assert "profile_lawyer" not in json.dumps(payload)

    assert calls["profile"] == {
        "name": "Lawyer",
        "description": "Unboks tenant workspace: lawyer",
        "color": None,
    }
    assert calls["connect"] == {
        "platform": "whatsapp",
        "profile_id": "profile_lawyer",
        "redirect_url": "https://api.unboks.org/internal/api/connect/whatsapp/callback",
        "headless": False,
    }

    stored = channel_connections.get_connection_request(payload["requestId"])
    assert stored is not None
    assert stored.tenant_id == "lawyer"
    assert stored.status == "link_generated"
    assert stored.auth_url == "https://facebook.com/connect/lawyer"
    assert stored.zernio_profile_id == "profile_lawyer"
    assert stored.state_token_hash == channel_connections.hash_state_token(
        "zernio_state_123"
    )
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") == "profile_lawyer"


def test_whatsapp_connect_start_reuses_existing_profile(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_existing",
    )
    calls: dict[str, object] = {}

    class FakeZernioService:
        def create_profile(self, **kwargs):
            calls["created"] = True
            raise AssertionError("create_profile should not be called")

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            calls["profile_id"] = profile_id
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/existing",
                state="zernio_state_existing",
            )

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert response.status_code == 200
    assert calls == {"profile_id": "profile_existing"}
    assert response.json()["authUrl"] == "https://facebook.com/connect/existing"


def test_whatsapp_connect_start_handles_zernio_error(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, **kwargs):
            raise ZernioAPIError(400, "Zernio rejected the request.")

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Zernio rejected the request."
