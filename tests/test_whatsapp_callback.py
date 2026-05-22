import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    return TestClient(app)


def _connection_request(state_token="zernio_state_123"):
    return channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token=state_token,
        status="link_generated",
    ).request


def test_whatsapp_callback_marks_connection_connected(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request()

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "zernio_state_123",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "phone_1",
            "displayPhoneNumber": "+599 9 694 5527",
            "wabaId": "waba_1",
            "code": "do-not-store-this",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=success&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    assert stored.zernio_account_id == "account_1"
    assert stored.selected_phone_number_id == "phone_1"
    assert stored.display_phone_number == "+599 9 694 5527"
    assert stored.callback_payload_json is not None
    callback_payload = json.loads(stored.callback_payload_json)
    assert callback_payload["state"] == "zernio_state_123"
    assert "code" not in callback_payload

    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "connected"
    assert connection.zernio_profile_id == "profile_lawyer"
    assert connection.zernio_account_id == "account_1"
    assert connection.phone_number_id == "phone_1"
    assert connection.display_phone_number == "+599 9 694 5527"
    assert connection.waba_id == "waba_1"


def test_whatsapp_callback_marks_pending_when_number_is_missing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("state_pending")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "state_pending",
            "status": "pending-number",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=pending-number&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "pending_number"
    assert stored.zernio_account_id == "account_1"
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "pending"
    assert connection.zernio_account_id == "account_1"


def test_whatsapp_callback_marks_failed_on_error(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("state_failed")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "state_failed",
            "status": "failed",
            "error": "Client denied authorization.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_summary == "Client denied authorization."
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "failed"
    assert connection.last_error == "Client denied authorization."


def test_whatsapp_callback_rejects_invalid_state_safely(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _connection_request("state_real")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={"state": "wrong_state", "status": "success"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connect/whatsapp/result?status=failed"


def test_whatsapp_callback_rejects_missing_state_safely(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={"status": "success"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connect/whatsapp/result?status=failed"


def test_whatsapp_result_pages_render_safe_copy(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    success = client.get("/connect/whatsapp/result?status=success")
    pending = client.get("/connect/whatsapp/result?status=pending-number")
    failed = client.get("/connect/whatsapp/result?status=failed")
    unknown = client.get("/connect/whatsapp/result?status=<script>")

    assert success.status_code == 200
    assert "Connection received" in success.text
    assert pending.status_code == 200
    assert "Phone number needs review" in pending.text
    assert failed.status_code == 200
    assert "Connection not completed" in failed.text
    assert unknown.status_code == 200
    assert "Connection not completed" in unknown.text
    assert "<script>" not in unknown.text
