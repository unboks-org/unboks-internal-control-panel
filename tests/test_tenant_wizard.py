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
    monkeypatch.setenv(
        "NR3_TENANT_REGISTRY_PATH",
        str(tmp_path / "tenant_registry.json"),
    )
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
    assert "Register existing tenant" in body
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


def test_create_writes_tenant_registry_for_icp_sidebar(client, tmp_path):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Registry Co", "slug": "registry-co"},
        follow_redirects=False)
    assert r.status_code == 200
    registry_path = tmp_path / "tenant_registry.json"
    assert registry_path.exists()
    registered = json.loads(registry_path.read_text())
    assert registered["tenants"]["registry-co"]["name"] == "Registry Co"


def test_import_existing_tenant_registers_sidebar_row(client, tmp_path):
    r = client.post(
        "/admin/tenants/import",
        data={
            "slug": "pepe",
            "name": "Pepe Test",
            "status": "trial",
            "plan": "trial",
        },
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/tenants/pepe"

    registry_path = tmp_path / "tenant_registry.json"
    registered = json.loads(registry_path.read_text())
    assert registered["tenants"]["pepe"]["name"] == "Pepe Test"

    sidebar = client.get("/admin/tenants/pepe")
    assert sidebar.status_code == 200
    assert 'class="tenant-selector-name">Pepe Test<' in sidebar.text
    assert 'class="tenant-selector-slug muted">pepe<' in sidebar.text
    assert "channels-section" in sidebar.text


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



# --- J3 provisioner artifacts -------------------------------------


def _extract_block(rendered_html: str, dom_id: str) -> str:
    """Pull the <pre id=...>...</pre> body for one artifact."""
    m = re.search(
        rf'<pre id="{re.escape(dom_id)}"[^>]*>([^<]+)</pre>',
        rendered_html, re.DOTALL)
    assert m, f"<pre id={dom_id!r}> not found"
    return html.unescape(m.group(1))


def test_success_page_renders_all_four_provisioner_files(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Provisioner Demo", "slug": "prov-demo"},
        follow_redirects=False)
    assert r.status_code == 200
    assert 'id="ct-full-vps-setup"' in r.text
    assert 'data-ct-download-filename="setup-prov-demo.sh"' in r.text
    for dom_id in ("ct-client-json", "ct-platform-env",
                    "ct-docker-compose", "ct-nginx-snippet",
                    "ct-deploy-script"):
        assert f'id="{dom_id}"' in r.text, f"missing block: {dom_id}"
    for fname in ("client.json", "platform.env", "docker-compose.yml",
                   "prov-demo.nginx.conf", "deploy-prov-demo.sh"):
        assert f'data-ct-download-filename="{fname}"' in r.text


def test_client_json_carries_access_key(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme", "slug": "acme"},
        follow_redirects=False)
    assert r.status_code == 200
    data = _extract_client_json(r.text)
    assert "access_key" in data
    assert isinstance(data["access_key"], str)
    assert len(data["access_key"]) >= 30
    assert data["password"] != data["access_key"]


def test_platform_env_carries_dashboard_password(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme", "slug": "acme"},
        follow_redirects=False)
    assert r.status_code == 200
    data = _extract_client_json(r.text)
    env_text = _extract_block(r.text, "ct-platform-env")
    assert "DASHBOARD_PASSWORD=" + data["password"] in env_text
    assert "TENANT_ID=acme" in env_text
    assert "TENANT_SLUG=acme" in env_text
    assert "NR3_INTERNAL_OVERRIDES_URL=http://127.0.0.1:8010" in env_text
    assert "NR3_INTERNAL_API_TOKEN=PASTE_NR3_INTERNAL_API_TOKEN_HERE" in env_text
    assert "ICP_OVERRIDES_TTL_SECONDS=5" in env_text


def test_docker_compose_names_container_and_port(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme", "slug": "acme"},
        follow_redirects=False)
    assert r.status_code == 200
    compose = _extract_block(r.text, "ct-docker-compose")
    assert "container_name: wtyj-acme" in compose
    assert "image: wtyj-agent" in compose
    assert "env_file:\n      - ./config/platform.env" in compose
    assert "./logs:/app/logs" in compose
    assert re.search(r'"\d{4}:8001"', compose),         f"no host_port mapping in compose: {compose!r}"


def test_nginx_snippet_routes_slug_to_proxy_pass(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Acme", "slug": "acme"},
        follow_redirects=False)
    assert r.status_code == 200
    nginx = _extract_block(r.text, "ct-nginx-snippet")
    assert "location ^~ /api/acme/" in nginx
    assert "proxy_set_header X-Tenant-Slug acme;" in nginx
    assert "Access-Control-Allow-Credentials" in nginx
    assert re.search(r"proxy_pass http://127\.0\.0\.1:\d{4}/;", nginx)


def test_host_port_is_deterministic_and_in_range(client):
    """The slug-derived host port must be deterministic so the
    operator can re-generate the artifacts and get the SAME port."""
    r1 = client.post(
        "/admin/tenants/create",
        data={"name": "Stable A", "slug": "stable-a"},
        follow_redirects=False)
    r2 = client.post(
        "/admin/tenants/create",
        data={"name": "Stable B", "slug": "stable-b"},
        follow_redirects=False)
    assert r1.status_code == 200 and r2.status_code == 200
    port_a = re.search(r'(\d{4}):8001', _extract_block(r1.text, "ct-docker-compose"))
    port_b = re.search(r'(\d{4}):8001', _extract_block(r2.text, "ct-docker-compose"))
    assert port_a and port_b
    assert 8100 <= int(port_a.group(1)) <= 8199
    assert 8100 <= int(port_b.group(1)) <= 8199


def test_full_vps_setup_script_is_ready_to_paste(client):
    r = client.post(
        "/admin/tenants/create",
        data={"name": "One Paste", "slug": "one-paste"},
        follow_redirects=False)
    assert r.status_code == 200
    script = _extract_block(r.text, "ct-full-vps-setup")
    assert "Paste this entire block into the VPS terminal as root" in script
    assert "TENANT_DIR=/root/clients/one-paste" in script
    assert "cat > \"$TENANT_DIR/config/client.json\"" in script
    assert '"slug": "one-paste"' in script
    assert "cat > \"$TENANT_DIR/config/platform.env\"" in script
    assert "cat > \"$TENANT_DIR/docker-compose.yml\"" in script
    assert "python3 - <<'UNBOKS_NGINX_INSERT'" in script
    assert "# BEGIN UNBOKS TENANT one-paste" in script
    assert "docker compose down || true" in script
    assert "docker compose up -d" in script
    assert "nginx -t" in script
    assert "systemctl reload nginx" in script
    assert "https://dashboard.unboks.org/one-paste" in script
