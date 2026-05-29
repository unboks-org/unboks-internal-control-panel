import json
import re

from fastapi.testclient import TestClient

from app.main import app
from app.provisioning import AutoProvisionResult


def _seed_tenant(root, slug="acme", email="owner@example.com"):
    tenant_dir = root / slug / "config"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "client.json").write_text(
        json.dumps({
            "slug": slug,
            "name": "Acme",
            "email": email,
            "password": "old-password",
            "status": "active",
        }),
        encoding="utf-8",
    )


def test_forgot_password_response_is_generic_for_unknown_email(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    calls = []
    monkeypatch.setattr("app.password_recovery.send_email", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    _seed_tenant(tmp_path / "clients")

    client = TestClient(app)
    response = client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "unknown@example.com"},
    )

    assert response.status_code == 200
    assert "If this email exists" in response.text
    assert calls == []


def test_password_reset_token_is_hashed_and_single_use(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")

    sent = []

    def fake_send(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    def fake_queue(**kwargs):
        assert kwargs["slug"] == "acme"
        assert kwargs["action"] == "reset_dashboard_password"
        assert kwargs["new_password"] == "Better-Password-123"
        return AutoProvisionResult(
            status="succeeded",
            message="Password reset.",
            job_id="job-reset",
        )

    monkeypatch.setattr("app.password_recovery.send_email", fake_send)
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    monkeypatch.setattr("app.password_recovery.queue_tenant_host_action", fake_queue)

    client = TestClient(app)
    response = client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "OWNER@example.com"},
    )
    assert response.status_code == 200
    assert len(sent) == 1
    match = re.search(r"https://icp\.example\.test/password/reset/([A-Za-z0-9_\-]+)", sent[0]["body"])
    assert match, sent[0]["body"]
    raw_token = match.group(1)
    assert raw_token not in (tmp_path / "nr3.db").read_bytes().decode("latin1", errors="ignore")

    reset = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Better-Password-123",
            "confirm_password": "Better-Password-123",
        },
    )
    assert reset.status_code == 200
    assert "Password reset complete" in reset.text

    reused = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Another-Password-123",
            "confirm_password": "Another-Password-123",
        },
    )
    assert reused.status_code == 200
    assert "invalid or expired" in reused.text


def test_reset_rejects_weak_password_before_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")
    sent = []
    monkeypatch.setattr("app.password_recovery.send_email", lambda to, subject, body, settings: sent.append(body))
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    monkeypatch.setattr(
        "app.password_recovery.queue_tenant_host_action",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("queue should not run")),
    )

    client = TestClient(app)
    client.post("/password/forgot", data={"workspace": "acme", "email": "owner@example.com"})
    raw_token = re.search(r"/password/reset/([A-Za-z0-9_\\-]+)", sent[0]).group(1)

    response = client.post(
        f"/password/reset/{raw_token}",
        data={"password": "short", "confirm_password": "short"},
    )

    assert response.status_code == 200
    assert "Use at least 12 characters" in response.text
