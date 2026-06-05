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

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "Check your email" in response.text
    assert "ada@example.com" in response.text
    assert "14-day Unboks trial" in response.text
    assert "The link expires in 48 hours." in response.text
    assert "check your spam or promotions folder" in response.text
    assert "https://unboks.org" in response.text
    assert "Need help? Contact us" in response.text
    assert "What happens next?" in response.text
    assert "Resend confirmation email" not in response.text
    assert "/signup/verify/" not in response.text
    assert sent
    html = sent[0]["html"]
    assert html
    assert "Confirm email address" in html
    assert "background:#2563EB" in html
    assert 'href="https://icp.unboks.org/signup/verify/' in html
    assert "Please confirm your email address here:" not in sent[0]["body"]
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    verify = client.get(verify_path, follow_redirects=False)
    assert verify.status_code == 200
    assert "Email confirmed" in verify.text
    assert not (tmp_path / "clients" / "lovelace-law").exists()
    assert _stored_request(tmp_path)["status"] == "verified_pending_review"


def test_public_signup_sends_admin_alert_when_configured(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_ADMIN_EMAIL", "calvin@example.com")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert len(sent) == 2
    assert sent[0]["to"] == "ada@example.com"
    assert sent[0]["subject"] == "Confirm your Unboks signup"
    assert "Please confirm your Unboks signup." in sent[0]["body"]
    assert "Please confirm your Unboks signup." in sent[0]["html"]
    assert sent[1]["to"] == "calvin@example.com"
    assert sent[1]["subject"] == "New Unboks free-trial signup: Lovelace Law"
    assert sent[1]["body"].startswith("Admin alert: New free-trial signup received.")
    assert "Name: Ada Lovelace" in sent[1]["body"]
    assert "Email: ada@example.com" in sent[1]["body"]
    assert "Business: Lovelace Law" in sent[1]["body"]
    assert "Phone: +599 123 4567" in sent[1]["body"]
    assert "Status: verification_pending" in sent[1]["body"]
    assert "https://icp.unboks.org/admin/signups/" in sent[1]["body"]
    assert "token_hash" not in sent[1]["body"]
    record = _stored_request(tmp_path)
    assert record["admin_alert_status"] == "sent"
    assert record["admin_alert_sent_at"]
    assert record["admin_alert_error"] is None
    assert record["admin_alert_recipient"] == "calvin@example.com"
    assert record["confirmation_email_status"] == "sent"
    assert record["confirmation_email_sent_at"]
    assert record["confirmation_email_recipient"] == "ada@example.com"
    assert record["confirmation_email_error"] is None

    client.post("/login", data={"password": "test-password"})
    signups = client.get("/admin/signups")
    assert signups.status_code == 200
    assert "Admin email alerts are enabled for calvin@example.com." in signups.text
    assert "Admin alert:" in signups.text
    assert "sent" in signups.text
    assert "token_hash" not in signups.text

    detail = client.get(f"/admin/signups/{record['id']}")
    assert detail.status_code == 200
    assert "Email delivery" in detail.text
    assert "Confirmation email" in detail.text
    assert "ad***a@example.com" in detail.text
    assert "ca***n@example.com" in detail.text
    assert "token_hash" not in detail.text


def test_public_signup_admin_alert_missing_recipient_does_not_fail_signup(
    monkeypatch,
    tmp_path,
):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "Check your email" in response.text
    assert len(sent) == 1
    assert sent[0]["to"] == "ada@example.com"
    record = _stored_request(tmp_path)
    assert record["admin_alert_status"] == "not_configured"
    assert "recipient" in record["admin_alert_error"].lower()
    assert record["confirmation_email_status"] == "sent"

    client.post("/login", data={"password": "test-password"})
    signups = client.get("/admin/signups")
    assert signups.status_code == 200
    assert "Set NR3_PUBLIC_SIGNUP_ADMIN_EMAIL to receive signup alerts." in signups.text
    assert "Admin alert:" in signups.text
    assert "not configured" in signups.text
    assert "token_hash" not in signups.text


def test_public_signup_admin_alert_failure_does_not_fail_signup(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        if to_email == "calvin@example.com":
            raise RuntimeError("smtp failure")
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_ADMIN_EMAIL", "calvin@example.com")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "Check your email" in response.text
    assert len(sent) == 1
    assert sent[0]["to"] == "ada@example.com"
    record = _stored_request(tmp_path)
    assert record["admin_alert_status"] == "failed"
    assert record["admin_alert_sent_at"] is None
    assert record["admin_alert_error"] == "Admin alert email failed to send."
    assert record["admin_alert_recipient"] == "calvin@example.com"
    assert record["confirmation_email_status"] == "sent"


def test_admin_public_signups_page_lists_verified_request(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

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
    assert "Awaiting review" in signups.text
    assert "Not created" in signups.text
    assert "Review" in signups.text
    assert "lead-table" not in signups.text
    assert "token_hash" not in signups.text


def test_admin_public_signup_review_and_onboarding_actions(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)
    monkeypatch.setattr("app.emailer.send_email", fake_send_email)

    response = _signup(client)
    assert response.status_code == 202
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    assert client.get(verify_path, follow_redirects=False).status_code == 200
    record = _stored_request(tmp_path)
    signup_id = record["id"]

    client.post("/login", data={"password": "test-password"})
    detail = client.get(f"/admin/signups/{signup_id}")
    assert detail.status_code == 200
    assert "Current state" in detail.text
    assert "Review details" in detail.text
    assert "Approve signup" in detail.text
    assert "Send info request email" in detail.text
    assert "Generate onboarding link" not in detail.text
    assert "Send onboarding link by email" not in detail.text
    assert "token_hash" not in detail.text

    blocked_generate = client.post(
        f"/admin/signups/{signup_id}/generate-link",
        follow_redirects=True,
    )
    assert blocked_generate.status_code == 200
    assert "Approve this signup before generating an onboarding link." in blocked_generate.text
    assert _stored_request(tmp_path)["status"] == "verified_pending_review"

    info_sent = client.post(
        f"/admin/signups/{signup_id}/request-info",
        follow_redirects=True,
    )
    assert info_sent.status_code == 200
    assert "Information request sent" in info_sent.text
    assert "Generate onboarding link" not in info_sent.text
    assert _stored_request(tmp_path)["status"] == "info_requested"

    approved = client.post(
        f"/admin/signups/{signup_id}/approve",
        data={"review_note": "Looks good"},
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert "Signup approved." in approved.text
    assert _stored_request(tmp_path)["status"] == "approved"

    generated = client.post(
        f"/admin/signups/{signup_id}/generate-link",
        follow_redirects=True,
    )
    assert generated.status_code == 200
    assert "Generated onboarding link" in generated.text
    assert "https://icp.unboks.org/onboarding/" in generated.text
    assert "token_hash" not in generated.text
    assert _stored_request(tmp_path)["status"] == "onboarding_link_generated"

    sent_before = len(sent)
    mailed = client.post(
        f"/admin/signups/{signup_id}/send-onboarding",
        follow_redirects=True,
    )
    assert mailed.status_code == 200
    assert "Onboarding email sent" in mailed.text
    assert len(sent) == sent_before + 1
    assert sent[-1]["to"] == "ada@example.com"
    assert "Welcome to Unboks" in sent[-1]["subject"]
    assert _stored_request(tmp_path)["status"] == "onboarding_link_sent"


def test_admin_public_signup_reject_archives_and_hides_request(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client, email="trying@example.com")
    assert response.status_code == 202
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    assert client.get(verify_path, follow_redirects=False).status_code == 200
    record = _stored_request(tmp_path)

    client.post("/login", data={"password": "test-password"})
    rejected = client.post(
        f"/admin/signups/{record['id']}/reject",
        data={"reject_reason": "Not a real company"},
        follow_redirects=True,
    )

    assert rejected.status_code == 200
    assert "Signup rejected and archived." in rejected.text
    stored = _stored_request(tmp_path)
    assert stored["status"] == "archived"
    assert stored["review_status"] == "rejected"
    assert stored["archived_at"]

    list_page = client.get("/admin/signups")
    assert list_page.status_code == 200
    assert "trying@example.com" not in list_page.text

    archive_page = client.get("/admin/signups?archived=1")
    assert archive_page.status_code == 200
    assert "trying@example.com" in archive_page.text
    assert "Archived" in archive_page.text


def test_admin_public_signups_hides_legacy_rejected_requests(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = _signup(client, email="legacy-denied@example.com")
    assert response.status_code == 202
    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    request_id, record = next(iter(data["requests"].items()))
    record["status"] = "rejected"
    record["review_status"] = "rejected"
    data["requests"][request_id] = record
    store_path.write_text(json.dumps(data), encoding="utf-8")

    client.post("/login", data={"password": "test-password"})
    list_page = client.get("/admin/signups")
    assert list_page.status_code == 200
    assert "legacy-denied@example.com" not in list_page.text

    archive_page = client.get("/admin/signups?archived=1")
    assert archive_page.status_code == 200
    assert "legacy-denied@example.com" in archive_page.text
    assert "Archived" in archive_page.text


def test_archived_signup_workspace_is_hidden_from_sidebar(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    registry = {
        "tenants": {
            "trying": {"slug": "trying", "name": "Trying", "status": "active"},
            "test": {"slug": "test", "name": "Test", "status": "active"},
        }
    }
    (tmp_path / "registry.json").write_text(json.dumps(registry), encoding="utf-8")
    signup_store = {
        "requests": {
            "denied": {
                "id": "denied",
                "full_name": "Calvin Adamus",
                "business_name": "Trying",
                "email": "trying@example.com",
                "phone": "",
                "slug_hint": "trying",
                "status": "rejected",
                "created_at": "2026-06-01T00:00:00+00:00",
                "updated_at": "2026-06-01T00:00:00+00:00",
                "token_expires_at": "2026-06-02T00:00:00+00:00",
                "provisioned_slug": "trying",
                "review_status": "rejected",
            }
        }
    }
    (tmp_path / "signup_requests.json").write_text(
        json.dumps(signup_store),
        encoding="utf-8",
    )

    client.post("/login", data={"password": "test-password"})
    page = client.get("/admin/signups")

    assert page.status_code == 200
    assert 'class="tenant-selector-name">Trying<' not in page.text
    assert 'class="tenant-selector-slug muted">trying<' not in page.text
    assert 'class="tenant-selector-name">Test<' in page.text


def test_admin_public_signup_reject_requires_reason(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    response = _signup(client)
    assert response.status_code == 202
    record = _stored_request(tmp_path)

    client.post("/login", data={"password": "test-password"})
    rejected = client.post(
        f"/admin/signups/{record['id']}/reject",
        data={"reject_reason": ""},
        follow_redirects=True,
    )

    assert rejected.status_code == 200
    assert "Reject reason is required." in rejected.text
    assert _stored_request(tmp_path)["status"] == "verification_pending"


def test_public_signup_email_mentions_verification_expiry(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_VERIFICATION_TTL_HOURS", "24")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client)

    assert response.status_code == 202
    assert "This link expires in 24 hours." in sent[0]["body"]
    assert "This link expires in <strong>24 hours</strong>." in sent[0]["html"]


def test_public_signup_expired_verification_link_rejected(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

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

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

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
