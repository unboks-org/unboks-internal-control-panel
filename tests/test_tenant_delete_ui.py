import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    tenants_root = tmp_path / "tenants"
    for slug, name in (("unboks", "Unboks"), ("lawyer", "Lawyer")):
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


def test_non_reserved_tenant_renders_permanent_delete_ui(client):
    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert "Delete Tenant (Permanent Wipe)" in response.text
    assert "DELETE TENANT FOREVER" in response.text
    assert "FINAL WARNING" in response.text
    assert "data-delete-tenant" in response.text
    assert "/root/clients/lawyer" in response.text


def test_reserved_unboks_does_not_render_permanent_delete_ui(client):
    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "The master Unboks tenant is protected from suspension." in response.text
    assert "Delete Tenant (Permanent Wipe)" not in response.text
    assert "data-delete-tenant" not in response.text


def test_admin_js_contains_permanent_delete_handlers():
    js = open("app/static/js/admin.js", encoding="utf-8").read()

    assert "initTenantPermanentDelete" in js
    assert 'method: "DELETE"' in js
    assert "DELETE FOREVER" in js
    assert "/internal/api/tenants/" in js
