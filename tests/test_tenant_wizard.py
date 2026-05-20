"""J3-BE-50 Manual-Mode Add-New-Tenant wizard.

The wizard:
  - Validates name + slug.
  - Builds a flat client.json (slug, name, password, status, plan,
    created_at + optional wizard fields).
  - Optionally sends the welcome email.
  - Does NOT write to local disk.
  - Does NOT call any provisioning service.
  - Renders a 200 success page with the JSON + Copy/Download buttons.
"""
import html
import json
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import tenants


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    # Point NR3_TENANTS_CLIENT_DIR somewhere safe so the discovery
    # code stays happy, but the Manual-Mode wizard never touches it.
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "client_root"))
    (tmp_path / "client_root").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def _extract_client_json(rendered_html: str) -> dict:
    """Pull the <pre id="ct-client-json">...</pre> body out of the
    success page, HTML-unescape it (the browser does the same when
    rendering), and parse as JSON."""
    m = re.search(
        r'<pre id="ct-client-json"[^>]*>([^<]+)</pre>',
        rendered_html, re.DOTALL)
    assert m, "client.json <pre> not found on success page"
    return json.loads(html.unescape(m.group(1)))


# --- GET /admin/tenants/new ---------------------------------------


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


# --- POST /admin/tenants/create — happy paths ---------------------


def test_create_minimal_tenant_renders_success_page(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme Charters"},
        follow_redirects=False)
    assert r.status_code == 200, r.text
    assert "Tenant created" in r.text
    assert "acme-charters" in r.text
    assert "https://dashboard.unboks.org/acme-charters" in r.text
    assert "data-ct-copy" in r.text
    assert "data-ct-download" in r.text
    assert 'data-ct-download-filename="client.json"' in r.text


def test_create_minimal_tenant_client_json_required_fields(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme Charters"},
        follow_redirects=False)
    assert r.status_code == 200
    data = _extract_client_json(r.text)
    for field in ("slug", "name", "password", "status", "plan", "created_at"):
        assert field in data, f"missing required field: {field}"
    assert data["slug"] == "acme-charters"
    assert data["name"] == "Acme Charters"
    assert isinstance(data["password"], str) and len(data["password"]) >= 12
    assert data["plan"] == "trial"
    assert data["status"] == "trial"
    assert "T" in data["created_at"]
    assert data["created_at"].endswith("+00:00")


def test_create_full_form_propagates_optional_fields(client):
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Marina Bay",
            "slug": "marina-bay",
            "contact_person": "Calvin",
            "contact_email": "calvin@example.com",
            "phone": "+1 555 4321",
            "plan": "monthly",
            "status": "active",
            "tone": "Friendly",
            "notes": "Be brief.",
        },
        follow_redirects=False)
    assert r.status_code == 200
    data = _extract_client_json(r.text)
    assert data["slug"] == "marina-bay"
    assert data["name"] == "Marina Bay"
    assert data["plan"] == "monthly"
    assert data["status"] == "active"
    assert data["contact_person"] == "Calvin"
    assert data["email"] == "calvin@example.com"
    assert data["whatsapp"] == "+1 555 4321"
    assert data["agent_tone"] == "Friendly"
    assert data["notes"] == "Be brief."


def test_create_writes_client_json_locally_for_sidebar(client, tmp_path):
    """Sidebar fix: the wizard writes the flat client.json under
    NR3_TENANTS_CLIENT_DIR so list_tenants() picks the new tenant
    up on the next page render."""
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Sidebar Co", "slug": "sidebar-co"},
        follow_redirects=False)
    assert r.status_code == 200
    config_path = tmp_path / "client_root" / "sidebar-co" / "config" / "client.json"
    assert config_path.exists()
    import json as _json
    written = _json.loads(config_path.read_text())
    assert written["slug"] == "sidebar-co"
    assert written["name"] == "Sidebar Co"
    # And list_tenants() now sees the new tenant.
    listed = [t.id for t in tenants.list_tenants()]
    assert "sidebar-co" in listed


def test_create_with_file_upload_is_silently_accepted(client, tmp_path):
    """Form's optional file-upload still submits cleanly. The file
    bytes themselves are discarded (Manual Mode does not store
    uploads); the tenant folder + client.json still get written."""
    files = [("files", ("hello.txt", b"hello world", "text/plain"))]
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Upload Co"},
        files=files,
        follow_redirects=False)
    assert r.status_code == 200
    # client.json is written...
    assert (tmp_path / "client_root" / "upload-co" / "config" / "client.json").exists()
    # ...but the upload bytes are NOT persisted anywhere.
    uploads_dir = tmp_path / "client_root" / "upload-co" / "data" / "uploads"
    assert not uploads_dir.exists() or not any(uploads_dir.iterdir())


# --- POST /admin/tenants/create — error paths ---------------------


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
    """The wizard refuses to overwrite an existing slug folder so a
    duplicate submit can't silently regenerate the password and
    invalidate the operator's paper trail."""
    r1 = client.post(
        "/admin/tenants/create",
        data={"name": "Dup A", "slug": "dupe-slug"},
        follow_redirects=False)
    assert r1.status_code == 200
    r2 = client.post(
        "/admin/tenants/create",
        data={"name": "Dup B", "slug": "dupe-slug"},
        follow_redirects=False)
    assert r2.status_code == 400
    assert "already exists" in r2.text


# --- welcome email -----------------------------------------------


@pytest.fixture
def email_capture(monkeypatch):
    """Capture every send_email call without hitting SMTP."""
    sent = []

    def fake_send(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    from app import emailer
    monkeypatch.setattr(emailer, "send_email", fake_send)
    monkeypatch.setattr(emailer, "smtp_is_configured", lambda s: True)
    return sent


def test_welcome_email_sent_when_checked(client, email_capture):
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Acme",
            "slug": "acme",
            "contact_email": "ops@acme.test",
            "send_welcome": "1",
        },
        follow_redirects=False)
    assert r.status_code == 200
    assert len(email_capture) == 1
    msg = email_capture[0]
    assert msg["to"] == "ops@acme.test"
    assert "Acme" in msg["subject"]
    assert "https://dashboard.unboks.org/acme" in msg["body"]
    data = _extract_client_json(r.text)
    assert data["password"] in msg["body"]
    assert "Welcome email sent to" in r.text


def test_welcome_email_skipped_when_unchecked(client, email_capture):
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Acme",
            "slug": "acme",
            "contact_email": "ops@acme.test",
        },
        follow_redirects=False)
    assert r.status_code == 200
    assert len(email_capture) == 0
    assert "checkbox was not ticked" in r.text


def test_welcome_email_skipped_without_contact_email(client, email_capture):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "No Email", "send_welcome": "1"},
        follow_redirects=False)
    assert r.status_code == 200
    assert len(email_capture) == 0
    assert "no contact email was provided" in r.text


def test_welcome_email_send_failure_does_not_crash(client, monkeypatch):
    """SMTP raise → success page still renders with a warning; the
    JSON is still shown so the operator can send credentials
    manually."""
    from app import emailer
    monkeypatch.setattr(emailer, "smtp_is_configured", lambda s: True)

    def boom(*args, **kwargs):
        raise RuntimeError("smtp connection refused")

    monkeypatch.setattr(emailer, "send_email", boom)
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Boom",
            "slug": "boom",
            "contact_email": "ops@boom.test",
            "send_welcome": "1",
        },
        follow_redirects=False)
    assert r.status_code == 200
    assert "Welcome email send failed" in r.text
    data = _extract_client_json(r.text)
    assert data["slug"] == "boom"


def test_welcome_email_no_smtp(client, monkeypatch):
    from app import emailer
    monkeypatch.setattr(emailer, "smtp_is_configured", lambda s: False)
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "No SMTP",
            "slug": "no-smtp",
            "contact_email": "x@y.com",
            "send_welcome": "1",
        },
        follow_redirects=False)
    assert r.status_code == 200
    assert "SMTP is not configured" in r.text


# --- slug helpers (unchanged unit tests) --------------------------


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
