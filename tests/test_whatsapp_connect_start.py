import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.zernio import ZernioAPIError, ZernioConnectUrl, ZernioProfile


def _write_tenant(root, slug="lawyer", name="Lawyer", extra=None):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    data = {"slug": slug, "name": name, "status": "active"}
    if extra:
        data.update(extra)
    (config_dir / "client.json").write_text(
        json.dumps(data),
        encoding="utf-8",
    )


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv(
        "NR3_BASE_URL",
        "https://icp.unboks.org",
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
                # Standard Zernio callbacks do not echo this provider-owned
                # OAuth state, so Nr3 must not use it for callback correlation.
                state=None,
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
    assert "profile_lawyer" not in json.dumps(payload)

    assert calls["profile"] == {
        "name": "Lawyer",
        "description": "Unboks tenant workspace: lawyer",
        "color": None,
    }
    connect_call = calls["connect"]
    assert connect_call["platform"] == "whatsapp"
    assert connect_call["profile_id"] == "profile_lawyer"
    assert connect_call["headless"] is False
    parsed_callback = urlparse(str(connect_call["redirect_url"]))
    assert parsed_callback._replace(query="").geturl() == (
        "https://icp.unboks.org/internal/api/connect/whatsapp/callback"
    )
    callback_tokens = parse_qs(parsed_callback.query).get("nr3_token", [])
    assert len(callback_tokens) == 1
    correlation_token = callback_tokens[0]
    assert correlation_token not in json.dumps(payload)

    stored = channel_connections.get_connection_request(payload["requestId"])
    assert stored is not None
    assert stored.tenant_id == "lawyer"
    assert stored.status == "link_generated"
    assert stored.auth_url == "https://facebook.com/connect/lawyer"
    assert stored.zernio_profile_id == "profile_lawyer"
    assert stored.state_token_hash == channel_connections.hash_state_token(
        correlation_token
    )
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") == "profile_lawyer"


def test_new_whatsapp_link_supersedes_old_callback_without_tenant_mutation(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    callback_tokens: list[str] = []
    account_lookups: list[str] = []

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            callback_tokens.append(
                parse_qs(urlparse(redirect_url).query)["nr3_token"][0]
            )
            return ZernioConnectUrl(
                auth_url=f"https://facebook.com/connect/{len(callback_tokens)}",
                state=None,
            )

        def get_account(self, account_id):
            account_lookups.append(account_id)
            raise AssertionError("A superseded callback must not query or mutate provider state")

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    first = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )
    second = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert first.status_code == second.status_code == 200
    first_request = channel_connections.get_connection_request(
        first.json()["requestId"]
    )
    second_request = channel_connections.get_connection_request(
        second.json()["requestId"]
    )
    assert first_request is not None and first_request.status == "cancelled"
    assert second_request is not None and second_request.status == "link_generated"
    assert channel_connections.get_latest_connection_request_for_tenant(
        "lawyer"
    ).id == second_request.id

    old_callback = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "nr3_token": callback_tokens[0],
            "connected": "whatsapp",
            "accountId": "old_account",
        },
        follow_redirects=False,
    )

    assert old_callback.status_code == 303
    assert old_callback.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )
    assert account_lookups == []
    assert channel_connections.get_tenant_channel_connection("lawyer") is None
    stored_client = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in stored_client


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


def test_customer_whatsapp_start_rejects_expired_public_token(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    expired = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    _write_tenant(
        tenants_root,
        extra={
            "whatsapp_connect_token": "expired-token",
            "whatsapp_connect_token_expires_at": expired,
        },
    )
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/connect/whatsapp/customer/start",
        params={"tenantId": "lawyer", "token": "expired-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )


def test_customer_whatsapp_start_accepts_unexpired_public_token(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    _write_tenant(
        tenants_root,
        extra={
            "whatsapp_connect_token": "valid-token",
            "whatsapp_connect_token_expires_at": expires,
        },
    )
    client = _client(monkeypatch, tmp_path)

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/lawyer",
                state="state_for_public_start",
            )

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.get(
        "/connect/whatsapp/customer/start",
        params={"tenantId": "lawyer", "token": "valid-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "https://facebook.com/connect/lawyer"
