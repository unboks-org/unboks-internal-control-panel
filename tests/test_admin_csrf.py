from fastapi.testclient import TestClient

from app.main import app


def _production_client(monkeypatch, tmp_path) -> TestClient:
    monkeypatch.setenv("NR3_ENV", "production")
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app, base_url="https://icp.unboks.org")
    response = client.post("/login", data={"password": "test-password"})
    assert response.status_code == 200
    return client


def test_admin_post_rejects_missing_origin_in_production(monkeypatch, tmp_path):
    client = _production_client(monkeypatch, tmp_path)

    response = client.post(
        "/admin/todos",
        data={"content_html": "<p>Blocked</p>", "content_plain": "Blocked"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    assert response.text == "CSRF validation failed."


def test_admin_post_accepts_same_origin_in_production(monkeypatch, tmp_path):
    client = _production_client(monkeypatch, tmp_path)

    response = client.post(
        "/admin/todos",
        data={"content_html": "<p>Allowed</p>", "content_plain": "Allowed"},
        headers={"Origin": "https://icp.unboks.org"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/todos"


def test_admin_post_rejects_cross_origin_in_production(monkeypatch, tmp_path):
    client = _production_client(monkeypatch, tmp_path)

    response = client.post(
        "/admin/todos",
        data={"content_html": "<p>Blocked</p>", "content_plain": "Blocked"},
        headers={"Origin": "https://evil.example"},
        follow_redirects=False,
    )

    assert response.status_code == 403


def test_internal_tenant_mutation_rejects_cross_origin_before_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ENV", "production")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret")
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.unboks.org")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    client = TestClient(app, base_url="https://icp.unboks.org")

    response = client.request(
        "DELETE",
        "/internal/api/tenants/test",
        json={"typedSlug": "test", "finalConfirmation": "DELETE FOREVER"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
