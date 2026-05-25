import json

from fastapi.testclient import TestClient

from app.main import app


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.delenv("NR3_AUTO_PROVISION", raising=False)
    return TestClient(app)


def test_public_signup_creates_trial_tenant_and_redirects(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/signup",
        data={
            "full_name": "Ada Lovelace",
            "business_name": "Lovelace Law",
            "email": "ada@example.com",
            "phone": "+599 123 4567",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "https://dashboard.unboks.org/login?workspace=lovelace-law"
    )
    cfg = tmp_path / "clients" / "lovelace-law" / "config" / "client.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["status"] == "active"
    assert data["billing_status"] == "trialing"
    assert data["email"] == "ada@example.com"
    assert data["business"]["name"] == "Lovelace Law"
    assert data["whatsapp_connect_token"]
    assert data["whatsapp_connect_token_expires_at"]


def test_public_signup_auto_provision_queues_without_precreating_root(
    monkeypatch,
    tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    response = client.post(
        "/signup",
        data={
            "full_name": "Grace Hopper",
            "business_name": "Grace Legal",
            "email": "grace@example.com",
            "phone": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert not (tmp_path / "clients" / "grace-legal").exists()
    queued = list(jobs.glob("*.json"))
    assert len(queued) == 1
    payload = json.loads(queued[0].read_text(encoding="utf-8"))
    assert payload["slug"] == "grace-legal"
    assert payload["client_data"]["whatsapp_connect_token"]
    assert payload["client_data"]["whatsapp_connect_token_expires_at"]
