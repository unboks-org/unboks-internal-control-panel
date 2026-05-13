from fastapi.testclient import TestClient

from app.main import app
from app.onboarding import (
    LeadInput,
    create_lead,
    create_or_refresh_token,
    get_lead,
    hash_token,
)


def test_healthz_returns_ok() -> None:
    client = TestClient(app)
    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "wtyj-admin"}


def test_admin_redirects_unauthenticated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.get("/admin", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_create_onboarding_lead_persists_and_rejects_duplicate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    login = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert login.status_code == 303

    created = client.post(
        "/admin/onboarding/leads",
        data={
            "email": "test@example.com",
            "business_name": "Test Business",
            "contact_name": "Test Contact",
            "language": "English",
            "notes": "Internal note",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/admin"

    admin = client.get("/admin")
    assert admin.status_code == 200
    assert "test@example.com" in admin.text
    assert "Test Business" in admin.text
    assert "lead_created" in admin.text

    duplicate = client.post(
        "/admin/onboarding/leads",
        data={"email": "TEST@example.com"},
    )
    assert duplicate.status_code == 400
    assert "already exists" in duplicate.text

    refreshed = TestClient(app)
    refreshed.cookies.update(client.cookies)
    persisted = refreshed.get("/admin")
    assert persisted.status_code == 200
    assert "test@example.com" in persisted.text


def test_admin_send_email_route_requires_auth(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app)

    response = client.post(
        "/admin/onboarding/leads/1/send-email",
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_token_generation_and_public_onboarding_placeholder(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_BASE_URL", "http://testserver")
    lead = create_lead(
        LeadInput(
            email="token@example.com",
            business_name="Token Business",
            contact_name=None,
            language=None,
            notes=None,
        )
    )

    updated, token = create_or_refresh_token(lead.id)

    assert token
    assert updated.onboarding_token_hash == hash_token(token)
    assert token not in updated.onboarding_token_hash

    client = TestClient(app)
    valid = client.get(f"/onboarding/{token}")
    assert valid.status_code == 200
    assert "Your secure link is valid" in valid.text
    assert "Token Business" in valid.text

    invalid = client.get("/onboarding/not-a-valid-token")
    assert invalid.status_code == 404
    assert "invalid or expired" in invalid.text


def test_missing_smtp_generates_preview_without_fake_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_BASE_URL", "http://testserver")
    monkeypatch.delenv("NR3_SMTP_HOST", raising=False)
    monkeypatch.delenv("NR3_SMTP_USERNAME", raising=False)
    monkeypatch.delenv("NR3_SMTP_PASSWORD", raising=False)
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})
    created = client.post(
        "/admin/onboarding/leads",
        data={"email": "manual@example.com"},
        follow_redirects=False,
    )
    assert created.status_code == 303

    send = client.post("/admin/onboarding/leads/1/send-email")

    assert send.status_code == 200
    assert "Email not configured" in send.text
    assert "Welcome to Unboks" in send.text
    assert "http://testserver/onboarding/" in send.text

    lead = get_lead(1)
    assert lead.status == "email_pending"
    assert lead.email_sent_at is None
    assert lead.email_last_error == "Email not configured."
