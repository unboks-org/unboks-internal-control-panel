import json

import pytest
from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    tenants_root = tmp_path / "tenants"
    for slug, name in (("unboks", "Unboks"), ("test", "Test")):
        config_dir = tenants_root / slug / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "client.json").write_text(
            json.dumps({"slug": slug, "name": name, "status": "active"}),
            encoding="utf-8",
        )
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def test_usage_monitor_shows_unknown_when_usage_tracking_missing(client):
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        phone_number_id="phone_test",
        display_phone_number="+599 9 000 0000",
    )

    response = client.get("/admin/usage")

    assert response.status_code == 200
    assert "Usage & costs" in response.text
    assert "Runtime usage telemetry is not available yet" in response.text
    assert "automatic pause enforcement requires runtime usage tracking" in response.text
    assert "Request AI pause when cap is reached" in response.text
    assert "Unknown" in response.text
    assert "Usage tracking not available" in response.text
    assert "No tenant cost caps configured." in response.text
    assert "accoun...test" in response.text
    assert "api_usage_events" not in response.text


def test_admin_can_save_tenant_cost_guardrails(client, tmp_path):
    response = client.post(
        "/admin/tenants/test/cost-guardrails",
        data={
            "daily_ai_reply_cap": "75",
            "daily_estimated_cost_cap_usd": "12.50",
            "pause_ai_on_cap": "on",
            "notes": "Watch this tenant during launch.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    client_json = tmp_path / "tenants" / "test" / "config" / "client.json"
    data = json.loads(client_json.read_text(encoding="utf-8"))
    guardrails = data["tenant_cost_guardrails"]
    assert guardrails["daily_ai_reply_cap"] == 75
    assert guardrails["daily_estimated_cost_cap_usd"] == 12.5
    assert guardrails["pause_ai_on_cap"] is True
    assert guardrails["notes"] == "Watch this tenant during launch."
    assert guardrails["updated_by"] == "nr3"

    page = client.get("/admin/usage")
    assert "75" in page.text
    assert "12.5" in page.text
    assert "Watch this tenant during launch." in page.text
