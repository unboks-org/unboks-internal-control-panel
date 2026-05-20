import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_INTERNAL_API_TOKEN", "bridge-token")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _bridge_headers(tenant: str = "unboks") -> dict[str, str]:
    return {
        "Authorization": "Bearer bridge-token",
        "X-Tenant-Identity": tenant,
    }


def test_internal_overrides_requires_bridge_token(client):
    r = client.get("/internal/tenants/unboks/overrides")
    assert r.status_code == 401

    r2 = client.get(
        "/internal/tenants/unboks/overrides",
        headers={"Authorization": "Bearer wrong"},
    )
    assert r2.status_code == 401


def test_internal_overrides_rejects_tenant_identity_mismatch(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers("pepe"),
    )
    assert r.status_code == 403


def test_internal_overrides_reports_empty_available_envelope(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["tenant_id"] == "unboks"
    assert body["feature_toggles"] == {}
    assert body["display_metadata"] == {}
    assert body["sot_entries"] == []
    assert body["ai_agent_settings"] == {
        "tone": None,
        "escalation_rules": None,
    }


def test_channel_toggle_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/channels/whatsapp/toggle",
        follow_redirects=False,
    )
    assert r.status_code == 303

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    toggles = bridge.json()["feature_toggles"]
    assert toggles["whatsapp_inbox"]["value"] is True
    assert toggles["whatsapp_inbox"]["source"] == "icp_override"
    assert toggles["whatsapp_inbox"]["wired"] is True
    assert "whatsapp" not in toggles


def test_channel_toggle_off_reflects_false_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    client.post("/admin/tenants/unboks/channels/tiktok/toggle", follow_redirects=False)
    client.post("/admin/tenants/unboks/channels/tiktok/toggle", follow_redirects=False)

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    assert bridge.json()["feature_toggles"]["tiktok_dms"]["value"] is False
