import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event

from fastapi.testclient import TestClient

from app.main import app


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "port_registry.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp_overrides.json"))
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


def test_public_signup_nginx_block_sets_exact_tenant_identity_header():
    from app.signup_service import _managed_nginx_block_text

    block = _managed_nginx_block_text("lovelace-law", 8123)

    assert "proxy_hide_header X-Unboks-Tenant;" in block
    assert 'add_header X-Unboks-Tenant "lovelace-law" always;' in block
    assert 'add_header Access-Control-Expose-Headers "X-Unboks-Tenant" always;' in block
    assert block.count('add_header X-Unboks-Tenant "lovelace-law" always;') == 1


def _stored_request(tmp_path):
    data = json.loads((tmp_path / "signup_requests.json").read_text(encoding="utf-8"))
    return next(iter(data["requests"].values()))


def _prepare_provisioned_delivery(
    tmp_path,
    *,
    signup_status="provisioned",
    delivery_status="pending",
    delivery_error="",
    attempt_count=0,
    lease_expires_at=None,
    job_id="job-1",
):
    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    record = next(iter(data["requests"].values()))
    secret_digest = hashlib.sha256(
        b"v1\0creation-1\0lovelace-law\0initial-secret"
    ).hexdigest()
    record.update({
        "status": signup_status,
        "provisioned_slug": "lovelace-law",
        "provisioning_creation_id": "creation-1",
        "provisioning_job_id": job_id,
        "credential_delivery_status": delivery_status,
        "credential_delivery_attempt_id": (
            "existing-attempt" if delivery_status == "sending" else ""
        ),
        "credential_delivery_attempt_count": attempt_count,
        "credential_delivery_lease_expires_at": lease_expires_at,
        "credential_delivery_error": delivery_error,
        "credential_delivery_secret_digest": secret_digest,
    })
    store_path.write_text(json.dumps(data), encoding="utf-8")

    config_dir = tmp_path / "clients" / "lovelace-law" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({
            "slug": "lovelace-law",
            "name": "Lovelace Law",
            "password": "initial-secret",
        }),
        encoding="utf-8",
    )
    return record


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
    assert "Approve &amp; send onboarding link" in detail.text
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

    sent_before = len(sent)
    approved = client.post(
        f"/admin/signups/{signup_id}/approve-send-onboarding",
        data={"review_note": "Looks good"},
        follow_redirects=True,
    )
    assert approved.status_code == 200
    assert "Signup approved and onboarding link sent" in approved.text
    assert "https://icp.unboks.org/onboarding/" in approved.text
    assert "Copy link" in approved.text
    assert len(sent) == sent_before + 1
    assert sent[-1]["to"] == "ada@example.com"
    assert "Welcome to Unboks" in sent[-1]["subject"]
    stored = _stored_request(tmp_path)
    assert stored["status"] == "onboarding_link_sent"
    assert stored["review_status"] == "approved"
    assert stored["onboarding_email_sent_at"]
    assert stored["onboarding_link"].startswith("https://icp.unboks.org/onboarding/")
    assert "token_hash" not in approved.text

    resent_attempt = client.post(
        f"/admin/signups/{signup_id}/approve-send-onboarding",
        data={"review_note": "Looks good"},
        follow_redirects=True,
    )
    assert resent_attempt.status_code == 200
    assert "already sent" in resent_attempt.text
    assert len(sent) == sent_before + 1


def test_onboarding_email_claim_blocks_concurrent_reject_before_send(
    monkeypatch, tmp_path,
):
    from app.config import get_settings
    from app.emailer import EmailDraft, EmailSendResult
    from app.public_signup_requests import (
        get_signup_request,
        update_signup_request,
    )
    from app.tenants import TenantCreateError

    client = _client(monkeypatch, tmp_path)
    response = _signup(client)
    assert response.status_code == 202
    record = _stored_request(tmp_path)
    signup_id = record["id"]
    settings = get_settings()
    update_signup_request(
        signup_id,
        settings,
        allowed_current_statuses={"verification_pending"},
        status="verified_pending_review",
    )

    observed = []

    def fake_prepare(lead_id):
        current = get_signup_request(signup_id, settings)
        observed.append(current["status"])
        try:
            update_signup_request(
                signup_id,
                settings,
                allowed_current_statuses={"verified_pending_review"},
                status="archived",
            )
        except TenantCreateError:
            observed.append("reject-blocked")
        return EmailSendResult(
            lead_id=lead_id,
            sent=True,
            smtp_configured=True,
            error=None,
            draft=EmailDraft(
                subject="Welcome",
                body="Body",
                onboarding_link="https://icp.unboks.org/onboarding/claimed",
            ),
        )

    monkeypatch.setattr(
        "app.routes.admin.prepare_or_send_onboarding_email",
        fake_prepare,
    )
    client.post("/login", data={"password": "test-password"})
    sent = client.post(
        f"/admin/signups/{signup_id}/approve-send-onboarding",
        data={"review_note": "Approved"},
        follow_redirects=False,
    )

    assert sent.status_code == 303
    assert observed == ["onboarding_email_sending", "reject-blocked"]
    assert _stored_request(tmp_path)["status"] == "onboarding_link_sent"


def test_failed_signup_with_reserved_slug_never_renders_workspace_link(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    response = _signup(client)
    assert response.status_code == 202
    record = _stored_request(tmp_path)
    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    data["requests"][record["id"]].update({
        "status": "failed",
        "provisioned_slug": "lovelace-law",
        "workspace_error": "Host rollback could not be proven.",
    })
    store_path.write_text(json.dumps(data), encoding="utf-8")

    client.post("/login", data={"password": "test-password"})
    detail = client.get(f"/admin/signups/{record['id']}")
    listing = client.get("/admin/signups")

    assert detail.status_code == 200
    assert listing.status_code == 200
    assert "Open tenant workspace" not in detail.text
    assert "Open tenant" not in listing.text
    assert "unsafe host rollback keeps the slug reserved" in detail.text


def test_failed_signup_retry_never_skips_unsafe_claim_to_second_slug(
    monkeypatch, tmp_path,
):
    import pytest

    from app.config import get_settings
    from app.provisioning import (
        create_tenant_provision_claim,
        tenant_provision_claim,
    )
    from app.public_signup_requests import update_signup_request
    from app.signup_service import create_public_signup_tenant
    from app.tenants import TenantCreateError

    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    record = _stored_request(tmp_path)
    settings = get_settings()
    update_signup_request(
        record["id"],
        settings,
        allowed_current_statuses={"verification_pending"},
        status="failed",
        provisioned_slug="lovelace-law",
        provisioning_creation_id="unsafe-generation",
        workspace_error="Rollback not proven.",
    )
    assert create_tenant_provision_claim("lovelace-law", "unsafe-generation")
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")

    with pytest.raises(TenantCreateError, match="original workspace slug is still reserved"):
        create_public_signup_tenant(
            full_name="Ada Lovelace",
            business_name="Lovelace Law",
            email="ada@example.com",
            phone="+599 123 4567",
            settings=settings,
            signup_request_id=record["id"],
        )

    assert tenant_provision_claim("lovelace-law")["creation_id"] == "unsafe-generation"
    assert tenant_provision_claim("lovelace-law-1") is None


def test_admin_public_signup_create_workspace_requires_successful_provision(
    monkeypatch,
    tmp_path,
):
    from app.provisioning import AutoProvisionResult
    from app.signup_service import SignupResult

    client = _client(monkeypatch, tmp_path)
    response = _signup(client)
    assert response.status_code == 202
    record = _stored_request(tmp_path)
    signup_id = record["id"]
    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    data["requests"][signup_id]["status"] = "approved"
    store_path.write_text(json.dumps(data), encoding="utf-8")

    def fake_create_public_signup_tenant(**kwargs):
        return SignupResult(
            slug="lovelace-law",
            name="Lovelace Law",
            email="ada@example.com",
            dashboard_url="https://dashboard.unboks.org/login?workspace=lovelace-law",
            password="temporary-password",
            access_key="access-key",
            trial_ends_at="2026-06-19T00:00:00+00:00",
            welcome_status="not_sent",
            welcome_error="Workspace provisioning did not complete.",
            provision_result=AutoProvisionResult(
                status="failed",
                message="worker failed",
                job_id="job-1",
            ),
        )

    monkeypatch.setattr(
        "app.routes.admin.create_public_signup_tenant",
        fake_create_public_signup_tenant,
    )

    client.post("/login", data={"password": "test-password"})
    created = client.post(
        f"/admin/signups/{signup_id}/create-workspace",
        follow_redirects=True,
    )

    assert created.status_code == 200
    assert "Workspace provisioning did not complete: worker failed" in created.text
    stored = _stored_request(tmp_path)
    assert stored["status"] == "failed"
    assert stored.get("provisioned_slug") is None


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


def test_admin_public_signups_marks_historical_email_tracking_without_sending(
    monkeypatch,
    tmp_path,
):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)
    response = _signup(client, email="legacy@example.com")
    assert response.status_code == 202
    assert sent == []

    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    request_id, record = next(iter(data["requests"].items()))
    record.pop("admin_alert_status", None)
    record.pop("confirmation_email_status", None)
    data["requests"][request_id] = record
    store_path.write_text(json.dumps(data), encoding="utf-8")

    client.post("/login", data={"password": "test-password"})
    list_page = client.get("/admin/signups")

    assert list_page.status_code == 200
    assert sent == []
    assert "historical" in list_page.text
    stored = json.loads(store_path.read_text(encoding="utf-8"))["requests"][request_id]
    assert stored["admin_alert_status"] == "historical_untracked"
    assert stored["confirmation_email_status"] == "historical_untracked"
    assert stored["confirmation_email_recipient"] == "legacy@example.com"


def test_admin_public_signups_hides_older_duplicates_by_default(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    first = _signup(client, email="duplicate@example.com")
    second = _signup(client, email="duplicate@example.com")
    assert first.status_code == 202
    assert second.status_code == 202

    client.post("/login", data={"password": "test-password"})
    list_page = client.get("/admin/signups")
    archive_page = client.get("/admin/signups?archived=1")

    assert list_page.status_code == 200
    assert list_page.text.count('<article class="signup-card"') == 1
    assert "View history" in list_page.text
    assert archive_page.status_code == 200
    assert archive_page.text.count('<article class="signup-card"') == 2
    assert "Possible duplicate" in archive_page.text
    assert "token_hash" not in archive_page.text


def test_admin_public_signups_keeps_duplicate_with_failed_credentials_visible(
    monkeypatch,
    tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    assert _signup(client, email="duplicate@example.com").status_code == 202
    assert _signup(client, email="duplicate@example.com").status_code == 202
    store_path = tmp_path / "signup_requests.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    older_id, newer_id = data["requests"]
    data["requests"][older_id]["created_at"] = "2026-01-01T00:00:00+00:00"
    data["requests"][newer_id]["created_at"] = "2026-01-02T00:00:00+00:00"
    data["requests"][older_id].update({
        "status": "provisioned",
        "provisioned_slug": "lovelace-law",
        "credential_delivery_status": "failed",
        "credential_delivery_error": "temporary SMTP outage",
    })
    store_path.write_text(json.dumps(data), encoding="utf-8")

    client.post("/login", data={"password": "test-password"})
    list_page = client.get("/admin/signups")

    assert list_page.status_code == 200
    assert list_page.text.count('<article class="signup-card"') == 2
    assert "<code>failed</code>" in list_page.text


def test_admin_public_signup_pages_do_not_send_email(monkeypatch, tmp_path):
    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body, "html": kwargs.get("html_body")})

    client = _client(monkeypatch, tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_ADMIN_EMAIL", "calvin@example.com")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)

    response = _signup(client, email="event-only@example.com")
    assert response.status_code == 202
    assert len(sent) == 2
    signup_id = _stored_request(tmp_path)["id"]

    client.post("/login", data={"password": "test-password"})
    assert client.get("/admin/signups").status_code == 200
    assert client.get(f"/admin/signups/{signup_id}").status_code == 200
    assert client.get("/admin/signups?archived=1").status_code == 200

    assert len(sent) == 2


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


def test_public_signup_auto_activation_stays_reviewable_when_worker_disabled(
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
    assert verify.status_code == 200
    assert "Email confirmed" in verify.text
    cfg = tmp_path / "clients" / "lovelace-law" / "config" / "client.json"
    assert not cfg.exists()
    assert not (tmp_path / "icp_overrides.json").exists()
    stored = _stored_request(tmp_path)
    assert stored["status"] == "verified_pending_review"
    assert not stored.get("provisioned_slug")
    assert not stored.get("provisioning_job_id")


def test_queued_public_signup_replay_is_idempotent_and_async_success_sends_once(
    monkeypatch,
    tmp_path,
):
    from app.provisioning import reconcile_host_action_results, tenant_provision_claim

    sent = []

    def fake_send_email(to_email, subject, body, settings, **kwargs):
        sent.append({"to": to_email, "subject": subject, "body": body})

    client = _client(monkeypatch, tmp_path)
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    reconciled = tmp_path / "reconciled"
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_PUBLIC_SIGNUP_AUTO_PROVISION_AFTER_VERIFY", "true")
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(reconciled))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    monkeypatch.setattr("app.routes.signup.send_email", fake_send_email)
    monkeypatch.setattr("app.emailer.send_email", fake_send_email)

    response = _signup(client)
    verify_path = sent[0]["body"].split("https://icp.unboks.org", 1)[1].split()[0]
    first = client.get(verify_path, follow_redirects=False)
    second = client.get(verify_path, follow_redirects=False)

    assert response.status_code == 202
    assert first.status_code == 202
    assert second.status_code == 202
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    job = json.loads(job_files[0].read_text(encoding="utf-8"))
    signup = _stored_request(tmp_path)
    assert signup["status"] == "provisioning_pending"
    assert signup["provisioning_job_id"] == job["job_id"]
    assert signup["provisioning_creation_id"] == job["creation_id"]
    assert job["signup_request_id"] == signup["id"]
    assert tenant_provision_claim("lovelace-law")["job_id"] == job["job_id"]
    assert len(sent) == 1

    config_dir = tmp_path / "clients" / "lovelace-law" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps(job["client_data"]),
        encoding="utf-8",
    )
    results.mkdir(parents=True, exist_ok=True)
    (results / f"{job['job_id']}.json").write_text(
        json.dumps({
            "job_id": job["job_id"],
            "job_type": "tenant_provision",
            "status": "succeeded",
            "slug": "lovelace-law",
            "creation_id": job["creation_id"],
            "signup_request_id": signup["id"],
            "message": "workspace ready",
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    assert reconcile_host_action_results() == 0
    completed = _stored_request(tmp_path)
    assert completed["status"] == "provisioned"
    assert completed["credential_delivery_status"] == "sent"
    assert tenant_provision_claim("lovelace-law") is None
    assert len(sent) == 2
    assert sent[-1]["to"] == "ada@example.com"

    replay = client.get(verify_path, follow_redirects=False)
    assert replay.status_code == 303
    assert replay.headers["location"].endswith("workspace=lovelace-law")
    assert len(list(jobs.glob("*.json"))) == 1
    assert len(sent) == 2


def test_success_result_replay_retries_failed_credential_delivery(
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.public_signup_requests import reconcile_signup_provisioning_result

    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    record = _prepare_provisioned_delivery(
        tmp_path,
        signup_status="provisioning_pending",
    )
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    attempts = []

    def flaky_send_email(to_email, subject, body, settings, **kwargs):
        attempts.append(to_email)
        if len(attempts) == 1:
            raise RuntimeError("temporary SMTP outage")

    monkeypatch.setattr("app.emailer.send_email", flaky_send_email)
    settings = get_settings()
    def reconcile():
        return reconcile_signup_provisioning_result(
            record["id"],
            slug="lovelace-law",
            creation_id="creation-1",
            job_id="job-1",
            status="succeeded",
            message="workspace ready",
            settings=settings,
        )

    assert reconcile() is True
    failed = _stored_request(tmp_path)
    assert failed["status"] == "provisioned"
    assert failed["credential_delivery_status"] == "failed"
    assert failed["credential_delivery_attempt_count"] == 1
    assert failed["credential_delivery_error"] == "temporary SMTP outage"

    assert reconcile() is True
    delivered = _stored_request(tmp_path)
    assert delivered["credential_delivery_status"] == "sent"
    assert delivered["credential_delivery_attempt_count"] == 2
    assert delivered["credential_delivery_sent_at"]
    assert attempts == ["ada@example.com", "ada@example.com"]

    assert reconcile() is True
    assert attempts == ["ada@example.com", "ada@example.com"]


def test_success_result_replay_reclaims_stale_credential_delivery_lease(
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.public_signup_requests import (
        get_signup_request,
        reconcile_signup_provisioning_result,
    )

    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    expired_lease = (
        datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=1)
    ).isoformat()
    record = _prepare_provisioned_delivery(
        tmp_path,
        delivery_status="sending",
        attempt_count=1,
        lease_expires_at=expired_lease,
    )
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    sent = []
    monkeypatch.setattr(
        "app.emailer.send_email",
        lambda to_email, subject, body, settings, **kwargs: sent.append(to_email),
    )
    settings = get_settings()

    assert "credential_delivery_attempt_id" not in get_signup_request(
        record["id"], settings
    )
    assert "credential_delivery_secret_digest" not in get_signup_request(
        record["id"], settings
    )
    assert reconcile_signup_provisioning_result(
        record["id"],
        slug="lovelace-law",
        creation_id="creation-1",
        job_id="job-1",
        status="succeeded",
        message="workspace ready",
        settings=settings,
    ) is True

    delivered = _stored_request(tmp_path)
    assert sent == ["ada@example.com"]
    assert delivered["credential_delivery_status"] == "sent"
    assert delivered["credential_delivery_attempt_count"] == 2
    assert delivered["credential_delivery_attempt_id"] == ""
    assert delivered["credential_delivery_lease_expires_at"] is None


def test_concurrent_success_replays_share_one_credential_delivery_lease(
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.public_signup_requests import reconcile_signup_provisioning_result

    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    record = _prepare_provisioned_delivery(tmp_path)
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    entered_send = Event()
    release_send = Event()
    sent = []

    def blocking_send_email(to_email, subject, body, settings, **kwargs):
        sent.append(to_email)
        entered_send.set()
        if not release_send.wait(timeout=5):
            raise RuntimeError("test send was not released")

    monkeypatch.setattr("app.emailer.send_email", blocking_send_email)
    settings = get_settings()

    def reconcile():
        return reconcile_signup_provisioning_result(
            record["id"],
            slug="lovelace-law",
            creation_id="creation-1",
            job_id="job-1",
            status="succeeded",
            message="workspace ready",
            settings=settings,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(reconcile)
        assert entered_send.wait(timeout=3)
        second = executor.submit(reconcile)
        assert second.result(timeout=3) is True
        release_send.set()
        assert first.result(timeout=3) is True

    delivered = _stored_request(tmp_path)
    assert sent == ["ada@example.com"]
    assert delivered["credential_delivery_status"] == "sent"
    assert delivered["credential_delivery_attempt_count"] == 1


def test_credential_retry_refuses_password_from_reused_workspace_slug(
    monkeypatch,
    tmp_path,
):
    from app.config import get_settings
    from app.public_signup_requests import retry_signup_credential_delivery

    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    record = _prepare_provisioned_delivery(
        tmp_path,
        delivery_status="failed",
        delivery_error="temporary SMTP outage",
        attempt_count=1,
    )
    client_path = tmp_path / "clients" / "lovelace-law" / "config" / "client.json"
    replacement = json.loads(client_path.read_text(encoding="utf-8"))
    replacement["password"] = "replacement-tenant-secret"
    client_path.write_text(json.dumps(replacement), encoding="utf-8")
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    sent = []
    monkeypatch.setattr(
        "app.emailer.send_email",
        lambda to_email, subject, body, settings, **kwargs: sent.append(to_email),
    )

    retried = retry_signup_credential_delivery(record["id"], get_settings())

    assert sent == []
    assert retried["credential_delivery_status"] == "failed"
    assert retried["credential_delivery_attempt_count"] == 2
    assert "generation does not match" in retried["credential_delivery_error"]


def test_admin_shows_and_retries_exact_credential_delivery_state(
    monkeypatch,
    tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    assert _signup(client).status_code == 202
    record = _prepare_provisioned_delivery(
        tmp_path,
        delivery_status="failed",
        delivery_error="temporary SMTP outage",
        attempt_count=1,
        job_id="",
    )
    monkeypatch.setenv("NR3_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("NR3_SMTP_USERNAME", "user")
    monkeypatch.setenv("NR3_SMTP_PASSWORD", "password")
    sent = []
    monkeypatch.setattr(
        "app.emailer.send_email",
        lambda to_email, subject, body, settings, **kwargs: sent.append(to_email),
    )
    client.post("/login", data={"password": "test-password"})

    listing = client.get("/admin/signups")
    detail = client.get(f"/admin/signups/{record['id']}")
    assert listing.status_code == 200
    assert detail.status_code == 200
    assert "Credentials" in listing.text
    assert "<code>failed</code>" in listing.text
    assert "<code>failed</code>" in detail.text
    assert "temporary SMTP outage" in detail.text
    assert "Retry workspace credential email" in detail.text

    retry = client.post(
        f"/admin/signups/{record['id']}/retry-credentials",
        follow_redirects=True,
    )
    assert retry.status_code == 200
    assert "Workspace credentials were sent successfully." in retry.text
    assert "<code>sent</code>" in retry.text
    assert sent == ["ada@example.com"]

    listing_after_delivery = client.get("/admin/signups")
    assert "ada@example.com" not in listing_after_delivery.text
