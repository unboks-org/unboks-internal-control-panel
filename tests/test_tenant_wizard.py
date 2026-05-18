"""Add-New-Tenant wizard: GET form + POST submit.

Creates the on-disk folder + client.json, saves uploaded files, and
optionally sends a welcome email. Tenant discovery picks the new
folder up on the next request.
"""
import json
import os

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import tenants


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "client_root"))
    (tmp_path / "client_root").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def _read_client_json(tmp_path, slug):
    return json.loads(
        (tmp_path / "client_root" / slug / "config" / "client.json").read_text()
    )


# --- GET /admin/tenants/new ----------------------------------------


def test_create_form_renders(client):
    r = client.get("/admin/tenants/new")
    assert r.status_code == 200
    body = r.text
    assert "Create a new tenant" in body
    assert 'name="files"' in body
    assert 'name="send_welcome"' in body


def test_create_form_requires_auth():
    c = TestClient(app)
    r = c.get("/admin/tenants/new", follow_redirects=False)
    assert r.status_code in (303, 401, 403)


# --- POST /admin/tenants/create — happy paths ----------------------


def test_create_minimal_tenant(client, tmp_path):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme Charters"},
        follow_redirects=False)
    assert r.status_code == 303, r.text
    loc = r.headers["location"]
    assert loc.startswith("/admin/tenants/acme-charters")
    payload = _read_client_json(tmp_path, "acme-charters")
    assert payload["business"]["slug"] == "acme-charters"
    assert payload["business"]["name"] == "Acme Charters"
    assert payload["business"]["plan"] == "trial"
    assert payload["business"]["status"] == "trial"
    assert any(t.id == "acme-charters" for t in tenants.list_tenants())


def test_create_full_tenant_with_contacts_and_tone(client, tmp_path):
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Marina Bay Tours",
            "slug": "marina-bay",
            "contact_person": "Calvin",
            "contact_email": "calvin@example.com",
            "phone": "+1 555 4321",
            "plan": "monthly",
            "status": "active",
            "tone": "Friendly",
            "notes": "Greet customers with hola.",
        },
        follow_redirects=False)
    assert r.status_code == 303
    payload = _read_client_json(tmp_path, "marina-bay")
    biz = payload["business"]
    assert biz["slug"] == "marina-bay"
    assert biz["name"] == "Marina Bay Tours"
    assert biz["plan"] == "monthly"
    assert biz["status"] == "active"
    assert biz["contact_person"] == "Calvin"
    assert biz["email"] == "calvin@example.com"
    assert biz["whatsapp"] == "+1 555 4321"
    assert biz["agent_tone"] == "Friendly"
    assert biz["notes"] == "Greet customers with hola."


def test_create_with_file_upload(client, tmp_path):
    files = [("files", ("hello.txt", b"hello world", "text/plain"))]
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Upload Co"},
        files=files,
        follow_redirects=False)
    assert r.status_code == 303
    uploads = tmp_path / "client_root" / "upload-co" / "data" / "uploads"
    assert uploads.is_dir()
    saved = list(uploads.iterdir())
    assert len(saved) == 1
    assert saved[0].name == "hello.txt"
    assert saved[0].read_bytes() == b"hello world"


# --- POST /admin/tenants/create — error paths ----------------------


def test_create_rejects_empty_name(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": ""},
        follow_redirects=False)
    assert r.status_code == 400
    assert "Business / tenant name is required" in r.text


def test_create_rejects_bad_slug(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Nope", "slug": "9-starts-with-digit"},
        follow_redirects=False)
    assert r.status_code == 400
    assert "Slug must be" in r.text


def test_create_rejects_duplicate_slug(client):
    r1 = client.post(
        "/admin/tenants/create",
        data={"name": "First", "slug": "dupe"},
        follow_redirects=False)
    assert r1.status_code == 303
    r2 = client.post(
        "/admin/tenants/create",
        data={"name": "Second", "slug": "dupe"},
        follow_redirects=False)
    assert r2.status_code == 400
    assert "already exists" in r2.text


# --- helpers --------------------------------------------------------


def test_validate_slug_accepts_clean():
    assert tenants.validate_slug("Acme-Co") == "acme-co"
    assert tenants.validate_slug("good_name1") == "good_name1"


def test_validate_slug_rejects_bad():
    bad = ["", "x", "9starts", "-starts", "has space", "UPPER!"]
    for s in bad:
        with pytest.raises(tenants.TenantCreateError):
            tenants.validate_slug(s)


def test_derive_slug_from_name():
    assert tenants.derive_slug_from_name("Acme Charters!") == "acme-charters"
    assert tenants.derive_slug_from_name("  Multiple   Spaces ") == "multiple-spaces"
    assert tenants.derive_slug_from_name("123 Numbers First") == "numbers-first"
