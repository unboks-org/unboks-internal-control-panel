import json

from fastapi.testclient import TestClient

from app import audit_log, channel_connections
from app.emailer import build_whatsapp_connection_email
from app.main import app


def _write_tenant(root, *, email="client@example.com", contact_person="Roberto"):
    config_dir = root / "lawyer" / "config"
    config_dir.mkdir(parents=True)
    payload = {"slug": "lawyer", "name": "Lawyer", "status": "active"}
    if email is not None:
        payload["email"] = email
    if contact_person is not None:
        payload["contact_person"] = contact_person
    (config_dir / "client.json").write_text(
        json.dumps(payload),
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


def _send(client: TestClient):
    return client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/connect/send-link"
    )


def test_whatsapp_connection_email_template_uses_link_and_first_name():
    draft = build_whatsapp_connection_email(
        client_first_name="Roberto",
        authorization_link="https://facebook.com/connect/lawyer",
    )

    assert draft.subject == "Next Step: Connect Your WhatsApp Business Number"
    assert "Hi Roberto," in draft.body
    assert "https://facebook.com/connect/lawyer" in draft.body
    assert "your own Meta account" in draft.body


def test_send_whatsapp_link_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = _send(client)

    assert response.status_code == 401


def test_send_whatsapp_link_requires_generated_link(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _send(client)

    assert response.status_code == 409
    assert response.json()["detail"] == "Generate an authorization link first."


def test_send_whatsapp_link_requires_contact_email(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root, email=None)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_1",
        status="link_generated",
    )

    response = _send(client)

    assert response.status_code == 409
    assert response.json()["detail"] == "Tenant contact email is missing."


def test_send_whatsapp_link_requires_smtp(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_1",
        status="link_generated",
    )
    monkeypatch.setattr("app.routes.connect.smtp_is_configured", lambda settings: False)

    response = _send(client)

    assert response.status_code == 503
    assert response.json()["detail"] == "SMTP is not configured."


def test_send_whatsapp_link_sends_email_and_audits(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_1",
        status="link_generated",
    )
    sent = {}

    def fake_send_email(to_email, subject, body, settings):
        sent["to_email"] = to_email
        sent["subject"] = subject
        sent["body"] = body

    monkeypatch.setattr("app.routes.connect.smtp_is_configured", lambda settings: True)
    monkeypatch.setattr("app.routes.connect.send_email", fake_send_email)

    response = _send(client)

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "tenantId": "lawyer",
        "sent": True,
        "email": "client@example.com",
        "message": "Email sent successfully to client@example.com",
    }
    assert sent["to_email"] == "client@example.com"
    assert sent["subject"] == "Next Step: Connect Your WhatsApp Business Number"
    assert "Hi Roberto," in sent["body"]
    assert "https://facebook.com/connect/lawyer" in sent["body"]
    events = audit_log.list_events()
    assert events[0].action == "whatsapp.connect_email_sent"
    assert "facebook.com" not in events[0].metadata_json
