import json

from fastapi.testclient import TestClient

from app import audit_log, channel_connections
from app.main import app
from app.zernio import ZernioConnectUrl, ZernioProfile


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
    monkeypatch.setenv("ZERNIO_API_KEY", "secret-zernio-key")
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_start_connection_records_safe_audit_event(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    class FakeZernioService:
        def create_profile(self, *, name, description=None, color=None):
            return ZernioProfile(id="profile_lawyer", name=name)

        def get_connect_url(self, *, platform, profile_id, redirect_url, headless=False):
            return ZernioConnectUrl(
                auth_url="https://facebook.com/connect/lawyer?token=secret-link",
                state="raw-callback-state",
            )

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/start"
    )

    assert response.status_code == 200
    events = audit_log.list_events()
    assert len(events) == 1
    event = events[0]
    assert event.tenant_id == "lawyer"
    assert event.action == "whatsapp.connect_link_generated"
    assert event.result == "ok"
    serialized = json.dumps(event.__dict__)
    assert "secret-zernio-key" not in serialized
    assert "raw-callback-state" not in serialized
    assert "secret-link" not in serialized


def test_callback_records_safe_audit_event(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer?token=secret-link",
        zernio_profile_id="profile_lawyer",
        state_token="raw-callback-state",
        status="link_generated",
    ).request

    class FakeZernioService:
        def get_account(self, account_id):
            return None

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "raw-callback-state",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "phone_1",
            "displayPhoneNumber": "+599 1",
            "code": "secret-oauth-code",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"

    events = audit_log.list_events()
    assert len(events) == 1
    event = events[0]
    assert event.action == "whatsapp.callback_connected"
    assert event.tenant_id == "lawyer"
    serialized = json.dumps(event.__dict__)
    assert "raw-callback-state" not in serialized
    assert "secret-oauth-code" not in serialized
    assert "secret-link" not in serialized


def test_settings_page_renders_audit_events(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _login(client)
    audit_log.record_event(
        tenant_id="lawyer",
        action="whatsapp.callback_connected",
        result="ok",
        safe_summary="WhatsApp connection completed.",
    )

    response = client.get("/admin/settings")

    assert response.status_code == 200
    assert "Global ICP audit log" in response.text
    assert "whatsapp.callback_connected (ok)" in response.text
    assert "lawyer" in response.text
