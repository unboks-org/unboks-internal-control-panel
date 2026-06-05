import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "ch.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "ov.json"))
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def test_workspace_renders_whatsapp_business_connection_card(client):
    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert 'data-whatsapp-connect data-tenant-id="unboks"' in response.text
    assert "WhatsApp Business" in response.text
    assert "Generate Authorization Link" in response.text
    assert "Send Link to Client" in response.text
    assert "Copy Link" in response.text
    assert "Refresh status" in response.text
    assert "Client authorization link" in response.text
    assert "Billing & outbound policy" in response.text
    assert "Templates off" in response.text
    assert "Allow outbound WhatsApp templates for this tenant" in response.text
    assert "Allow campaign / high-volume outbound messaging" in response.text
    assert "Zernio connected accounts, Meta WhatsApp charges" in response.text


def test_admin_can_save_whatsapp_billing_policy(client, tmp_path):
    config_dir = tmp_path / "tenants" / "unboks" / "config"
    config_dir.mkdir(parents=True)
    client_json = config_dir / "client.json"
    client_json.write_text(
        json.dumps({"slug": "unboks", "name": "Unboks", "status": "active"}),
        encoding="utf-8",
    )

    response = client.post(
        "/admin/tenants/unboks/channels/whatsapp/billing-policy",
        data={
            "outbound_templates_enabled": "on",
            "high_volume_review_required": "on",
            "notes": "Enable only after tenant billing sign-off.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    data = json.loads(client_json.read_text(encoding="utf-8"))
    policy = data["whatsapp_billing_policy"]
    assert policy["outbound_templates_enabled"] is True
    assert policy["campaigns_enabled"] is False
    assert policy["high_volume_review_required"] is True
    assert policy["notes"] == "Enable only after tenant billing sign-off."
    assert policy["updated_by"] == "nr3"


def test_admin_js_contains_whatsapp_connection_handlers():
    js = open("app/static/js/admin.js", encoding="utf-8").read()

    assert "initWhatsAppConnectionCard" in js
    assert "/channels/whatsapp/status" in js
    assert "/channels/whatsapp/connect/start" in js
    assert "/channels/whatsapp/connect/send-link" in js
    assert "/channels/whatsapp/phone-numbers/select" in js
