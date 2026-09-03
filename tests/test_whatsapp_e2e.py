import json
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app import audit_log, channel_connections
from app.main import app
from app.zernio import (
    ZernioAccountSummary,
    ZernioAPIError,
    ZernioConnectUrl,
    ZernioProfile,
)


def _write_tenant(root, slug="lawyer", name="Lawyer"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def test_whatsapp_authorization_flow_end_to_end_mocked(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenants_root))
    monkeypatch.setenv("ZERNIO_API_KEY", "secret-zernio-key")
    monkeypatch.setenv(
        "NR3_BASE_URL",
        "https://icp.unboks.org",
    )

    callback_context = {}

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            assert platform == "whatsapp"
            assert profile_id == "profile_lawyer"
            parsed_callback = urlparse(redirect_url)
            assert parsed_callback._replace(query="").geturl() == (
                "https://icp.unboks.org/internal/api/connect/whatsapp/callback"
            )
            callback_context["nr3_token"] = parse_qs(
                parsed_callback.query
            )["nr3_token"][0]
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/lawyer",
                state="provider_internal_state_not_echoed",
            )

        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
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

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    client = TestClient(app)
    login = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    start = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )
    assert start.status_code == 200
    start_payload = start.json()
    assert start_payload["authUrl"] == "https://facebook.com/connect/lawyer"
    assert start_payload["status"] == "link_generated"
    assert "provider_internal_state_not_echoed" not in json.dumps(start_payload)
    assert callback_context["nr3_token"] not in json.dumps(start_payload)

    callback = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "nr3_token": callback_context["nr3_token"],
            # A provider-owned value must never override Nr3's own nonce.
            "state": "provider_internal_state_not_echoed",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "phone_1",
            "displayPhoneNumber": "+599 9 694 5527",
            "wabaId": "waba_1",
        },
        follow_redirects=False,
    )
    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/connect/whatsapp/result?status=success&tenantId=lawyer"
    )

    status = client.get("/internal/api/tenants/lawyer/channels/whatsapp/status")
    assert status.status_code == 200
    status_payload = status.json()
    assert status_payload["status"] == "connected_healthy"
    assert status_payload["connected"] is True
    assert status_payload["displayPhoneNumber"] == "+599 9 694 5527"
    assert status_payload["providerAccountId"] == "account_1"

    workspace = client.get("/admin/tenants/lawyer")
    assert workspace.status_code == 200
    assert "WhatsApp Business" in workspace.text
    assert "secret-zernio-key" not in workspace.text

    events = audit_log.list_events()
    assert [event.action for event in events] == [
        "whatsapp.callback_connected",
        "whatsapp.connect_link_generated",
    ]


def test_transient_callback_lookup_failure_recovers_by_exact_account_on_refresh(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenants_root))
    monkeypatch.setenv("ZERNIO_API_KEY", "secret-zernio-key")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    callback_context: dict[str, str] = {}

    class EventuallyConsistentZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            callback_context["nr3_token"] = parse_qs(
                urlparse(redirect_url).query
            )["nr3_token"][0]
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/lawyer",
                state=None,
            )

        def get_account(self, account_id):
            raise ZernioAPIError(503, "Provider account is not visible yet.")

        def list_accounts(self, *, platform=None):
            assert platform == "whatsapp"
            return [
                ZernioAccountSummary(
                    id="account_exact",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Lawyer WhatsApp",
                    username="+599 9 694 5527",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 694 5527",
                    phone_number_id="phone_exact",
                    waba_id="waba_exact",
                )
            ]

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        EventuallyConsistentZernioService,
    )
    client = TestClient(app)
    assert client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    ).status_code == 303
    start = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    callback = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "nr3_token": callback_context["nr3_token"],
            "connected": "whatsapp",
            "accountId": "account_exact",
        },
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert callback.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )
    failed_request = channel_connections.get_connection_request(
        start.json()["requestId"]
    )
    assert failed_request is not None
    assert failed_request.status == "failed"
    assert failed_request.zernio_account_id == "account_exact"
    assert failed_request.zernio_account_verified is False
    failed_connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert failed_connection is not None
    assert failed_connection.status == "failed"
    assert failed_connection.zernio_account_id == "account_exact"
    assert failed_connection.zernio_account_verified is False

    refreshed = client.get(
        "/internal/api/tenants/lawyer/channels/whatsapp/status"
    )

    assert refreshed.status_code == 200
    assert refreshed.json()["status"] == "connected_healthy"
    assert refreshed.json()["providerAccountId"] == "account_exact"
    recovered_request = channel_connections.get_connection_request(
        start.json()["requestId"]
    )
    assert recovered_request is not None
    assert recovered_request.status == "connected"
    assert recovered_request.zernio_account_verified is True
