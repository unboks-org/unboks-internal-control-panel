import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "port_registry.json"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_REQUESTS_PATH", str(tmp_path / "signup_requests.json"))
    monkeypatch.delenv("NR3_AUTO_PROVISION", raising=False)
    monkeypatch.delenv("NR3_PUBLIC_SIGNUP_AUTO_PROVISION_AFTER_VERIFY", raising=False)
    monkeypatch.delenv("NR3_SMTP_HOST", raising=False)
    monkeypatch.delenv("NR3_SMTP_USERNAME", raising=False)
    monkeypatch.delenv("NR3_SMTP_PASSWORD", raising=False)
    return TestClient(app)


def _signup(client, email="ada@example.com"):
    return client.post(
        "/signup",
        data={
            "full_name": "Ada Lovelace",
            "business_name": "Lovelace Law",
            "email": email,
            "phone": "+599 123 4567",
        },
        follow_redirects=False,
    )


def _stored_request(tmp_path):
    data = json.loads((tmp_path / "signup_requests.json").read_text(encoding="utf-8"))
    return next(iter(data["requests"].values()))


def test_public_signup_stores_request_without_creating_tenant(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = _signup(client)

    assert response.status_code == 202
    assert "Signup received" in response.text
    assert not (tmp_path / "clients" / "lovelace-law").exists()
    assert not (tmp_path / "registry.json").exists()
    record = _stored_request(tmp_path)
    assert record["email"] == "ada@example.com"
    assert record["business_name"] == "Lovelace Law"
    assert record["status"] == "verification_pending"
    assert record["token_hash"]
    assert record["token_expires_at"]
    assert "token" not in record


def test_public_signup_honeypot_does_not_create_request(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/signup",
        data={
            "full_name": "Ada Lovelace",
            "business_name": "Lovelace Law",
            "email": "ada@example.com",
            "website": "spam.example",
        },
        follow_redirects=False,
    )

    assert response.status_code == 202
    assert not (tmp_path / "signup_requests.json").exists()
    assert not (tmp_path / "clients").exists()


def test_public_signup_rate_limits_same_email(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_RATE_LIMIT_PER_EMAIL_PER_DAY", "1")
    client = _client(monkeypatch, tmp_path)

    first = _signup(client, email="ada@example.com")
    second = _signup(client, email="ada@example.com")

    assert first.status_code == 202
    assert second.status_code == 400
    assert "too many signup attempts" in second.text.lower()


def test_public_signup_email_verification_without_auto_provision(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "Check your email" in response.text
    assert sent
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    verify = client.get(verify_path, follow_redirects=False)
    assert verify.status_code == 200
    assert "Email confirmed" in verify.text
    assert not (tmp_path / "clients" / "lovelace-law").exists()
    assert _stored_request(tmp_path)["status"] == "verified_pending_review"


def test_admin_public_signups_page_lists_verified_request(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)
    assert response.status_code == 202
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    verify = client.get(verify_path, follow_redirects=False)
    assert verify.status_code == 200

    client.post("/login", data={"password": "test-password"})
    signups = client.get("/admin/signups")

    assert signups.status_code == 200
    assert "Free trial signups" in signups.text
    assert "Lovelace Law" in signups.text
    assert "Ada Lovelace" in signups.text
    assert "ada@example.com" in signups.text
    assert "awaiting review" in signups.text
    assert "not provisioned" in signups.text
    assert "token_hash" not in signups.text


def test_public_signup_email_mentions_verification_expiry(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_VERIFICATION_TTL_HOURS", "24")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "This link expires in 24 hours." in sent[0]["body"]


def test_public_signup_expired_verification_link_rejected(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_VERIFICATION_TTL_HOURS", "1")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)
    assert response.status_code == 202

    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    request_id = next(iter(data["requests"]))
    data["requests"][request_id]["token_expires_at"] = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    ).isoformat()
    store_path.write_text(json.dumps(data), encoding="utf-8")

    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    verify = client.get(verify_path, follow_redirects=False)

    assert verify.status_code == 400
    assert "Invalid or expired verification link" in verify.text
    assert _stored_request(tmp_path)["status"] == "verification_expired"
    assert not (tmp_path / "clients" / "lovelace-law").exists()


def test_public_signup_verified_auto_provision_requires_explicit_flag(
    monkeypatch,
    tmp_path,
):
    sent = []

    def fake_send_email(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_AUTO_PROVISION_AFTER_VERIFY", "true")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)
    monkeypatch.setattr("app.signup_service.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    verify = client.get(verify_path, follow_redirects=False)
    assert verify.status_code == 303
    assert verify.headers["location"] == (
        "https://dashboard.unboks.org/login?workspace=lovelace-law"
    )
    cfg = tmp_path / "clients" / "lovelace-law" / "config" / "client.json"
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["status"] == "active"
    assert data["billing_status"] == "trialing"
    assert _stored_request(tmp_path)["status"] == "provisioned"
