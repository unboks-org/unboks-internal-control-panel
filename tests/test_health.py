from fastapi.testclient import TestClient

from app.main import app


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
