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
    assert "Generate authorization link" in response.text
    assert "Refresh status" in response.text
    assert "Client authorization link" in response.text


def test_admin_js_contains_whatsapp_connection_handlers():
    js = open("app/static/js/admin.js", encoding="utf-8").read()

    assert "initWhatsAppConnectionCard" in js
    assert "/channels/whatsapp/status" in js
    assert "/channels/whatsapp/connect/start" in js
    assert "/channels/whatsapp/phone-numbers/select" in js
