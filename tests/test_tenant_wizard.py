"""J3-BE-50 Manual-Mode Add-New-Tenant wizard.

The wizard:
  - Validates name + slug.
  - Builds a flat client.json (slug, name, password, status,
    created_at + optional wizard fields).
  - Optionally sends the welcome email.
  - Does NOT write to local disk.
  - Does NOT call any provisioning service.
  - Renders a 200 success page with the JSON + Copy/Download buttons.
"""
import html
import json
import os
import re
import stat
from pathlib import Path
from types import SimpleNamespace

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
    monkeypatch.setenv(
        "NR3_PORT_REGISTRY_PATH",
        str(tmp_path / "port_registry.json"),
    )
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp_overrides.json"))
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
    assert "Register existing tenant" not in body
    assert "/admin/tenants/import" not in body
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
    assert "https://dashboard.unboks.org/login?workspace=acme-charters" in r.text
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
    for field in ("slug", "name", "password", "status", "created_at"):
        assert field in data, f"missing required field: {field}"
    assert data["slug"] == "acme-charters"
    assert data["name"] == "Acme Charters"
    assert isinstance(data["password"], str) and len(data["password"]) >= 12
    assert "plan" not in data
    assert data["status"] == "active"
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
            "status": "inactive",
            "tone": "Friendly",
            "notes": "Be brief.",
        },
        follow_redirects=False)
    assert r.status_code == 200
    data = _extract_client_json(r.text)
    assert data["slug"] == "marina-bay"
    assert data["name"] == "Marina Bay"
    assert "plan" not in data
    assert data["status"] == "inactive"
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
    assert written["channel_account_allowlist"]["mode"] == "strict"
    assert written["channel_account_allowlist"]["zernio_accounts"] == []
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    state = _json.loads((tmp_path / "icp_overrides.json").read_text())
    toggles = state["tenants"]["sidebar-co"]["feature_toggles"]
    assert toggles["ai_auto_reply"]["value"] is False
    assert toggles["whatsapp_inbox"]["value"] is False
    assert toggles["facebook_dms"]["value"] is False
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
            "status": "inactive",
        },
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/tenants/pepe"

    registry_path = tmp_path / "tenant_registry.json"
    registered = json.loads(registry_path.read_text())
    assert registered["tenants"]["pepe"]["name"] == "Pepe Test"

    sidebar = client.get("/admin/tenants/pepe")
    assert sidebar.status_code == 200
    assert 'class="tenant-selector-name">Unboks<' not in sidebar.text
    assert 'class="tenant-selector-slug muted">unboks<' not in sidebar.text
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


def test_create_rejects_reserved_slug(client):
    response = client.post(
        "/admin/tenants/create",
        data={"name": "Reserved", "slug": "unboks"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert "reserved and cannot be created" in response.text


def test_create_rejects_duplicate_slug(client, tmp_path):
    """The wizard refuses to overwrite an existing slug folder so a
    duplicate submit can't silently regenerate the password and
    invalidate the operator's paper trail."""
    r1 = client.post(
        "/admin/tenants/create",
        data={"name": "Dup A", "slug": "dupe-slug"},
        follow_redirects=False)
    assert r1.status_code == 200
    from app import icp_overrides
    icp_overrides.set_feature_toggle(
        "dupe-slug", "ai_auto_reply", True, updated_by="existing-tenant"
    )
    r2 = client.post(
        "/admin/tenants/create",
        data={"name": "Dup B", "slug": "dupe-slug"},
        follow_redirects=False)
    assert r2.status_code == 400
    assert "already exists" in r2.text
    state = json.loads((tmp_path / "icp_overrides.json").read_text())
    toggle = state["tenants"]["dupe-slug"]["feature_toggles"]["ai_auto_reply"]
    assert toggle["value"] is True
    assert toggle["updated_by"] == "existing-tenant"


def test_create_releases_reserved_port_if_paused_state_cannot_initialize(
    client, monkeypatch, tmp_path,
):
    from app import icp_overrides

    def fail_initialize(*_args, **_kwargs):
        raise OSError("read-only state")

    monkeypatch.setattr(
        icp_overrides, "initialize_new_tenant_fail_closed", fail_initialize
    )
    response = client.post(
        "/admin/tenants/create",
        data={"name": "State Failure", "slug": "state-failure"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    registry_path = tmp_path / "port_registry.json"
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    assert "state-failure" not in registry


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


def _mock_successful_provision(monkeypatch):
    from app.routes import admin

    captured = {}

    def fake_auto_provision_tenant(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            status="succeeded",
            message="Tenant was provisioned.",
            job_id="job-welcome",
            details=("health check passed",),
            dashboard_url=kwargs["dashboard_url"],
            health_url="http://127.0.0.1:8123/health",
        )

    monkeypatch.setattr(admin, "auto_provision_tenant", fake_auto_provision_tenant)
    return captured


def test_welcome_email_sent_only_after_provisioning_succeeds(
    client, email_capture, monkeypatch,
):
    captured = _mock_successful_provision(monkeypatch)
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
    assert "https://dashboard.unboks.org/login?workspace=acme" in msg["body"]
    assert captured["client_data"]["password"] in msg["body"]
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


def test_welcome_email_skipped_without_contact_email(
    client, email_capture, monkeypatch,
):
    _mock_successful_provision(monkeypatch)
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
    _mock_successful_provision(monkeypatch)
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
    assert "Workspace: boom" in r.text


def test_welcome_email_no_smtp(client, monkeypatch):
    from app import emailer
    _mock_successful_provision(monkeypatch)
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


def test_disabled_provisioning_never_emails_unusable_credentials(
    client, email_capture,
):
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Manual First",
            "slug": "manual-first",
            "contact_email": "owner@example.com",
            "send_welcome": "1",
        },
        follow_redirects=False,
    )

    assert r.status_code == 200
    assert email_capture == []
    assert "Welcome email was not sent or scheduled" in r.text
    assert "Complete and verify provisioning" in r.text
    assert "ct-full-vps-setup" in r.text


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
    assert "NR3_INTERNAL_OVERRIDES_URL=http://wtyj-admin:8010" in env_text
    assert "NR3_INTERNAL_API_TOKEN=SET_BY_FULL_VPS_SETUP_SCRIPT" in env_text
    assert "ANTHROPIC_API_KEY=SET_BY_FULL_VPS_SETUP_SCRIPT" in env_text
    assert "PASTE_NR3_INTERNAL_API_TOKEN_HERE" not in env_text
    assert "ICP_OVERRIDES_TTL_SECONDS=5" in env_text
    assert "TENANT_RUNTIME_CONTROLS_REQUIRED=true" in env_text
    assert "TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true" in env_text


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
    assert "TENANT_RUNTIME_CONTROLS_REQUIRED=true" in compose
    assert "TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true" in compose
    assert "./logs:/app/logs" in compose
    assert re.search(r'"127\.0\.0\.1:\d{4}:8001"', compose),         f"no localhost host_port mapping in compose: {compose!r}"


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
    assert "Cache-Control, Pragma" in nginx
    assert "proxy_hide_header X-Unboks-Tenant;" in nginx
    assert 'add_header X-Unboks-Tenant "acme" always;' in nginx
    assert 'add_header Access-Control-Expose-Headers "X-Unboks-Tenant" always;' in nginx
    assert nginx.count('add_header X-Unboks-Tenant "acme" always;') == 1
    assert re.search(r"proxy_pass http://127\.0\.0\.1:\d{4}/;", nginx)


def test_host_port_is_stable_and_collision_safe(client):
    """The registry keeps each slug stable while avoiding the old
    100-port hash collision window."""
    r1 = client.post(
        "/admin/tenants/create",
        data={"name": "Stable A", "slug": "stable-a"},
        follow_redirects=False)
    r2 = client.post(
        "/admin/tenants/create",
        data={"name": "Stable B", "slug": "stable-b"},
        follow_redirects=False)
    assert r1.status_code == 200 and r2.status_code == 200
    port_a = re.search(r'127\.0\.0\.1:(\d{4}):8001', _extract_block(r1.text, "ct-docker-compose"))
    port_b = re.search(r'127\.0\.0\.1:(\d{4}):8001', _extract_block(r2.text, "ct-docker-compose"))
    assert port_a and port_b
    assert port_a.group(1) != port_b.group(1)
    assert 8100 <= int(port_a.group(1)) <= 8999
    assert 8100 <= int(port_b.group(1)) <= 8999


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
    assert 'chmod 600 "$TENANT_DIR/config/client.json" "$TENANT_DIR/config/platform.env"' in script
    assert "umask 077" in script
    assert "PASTE_NR3_INTERNAL_API_TOKEN_HERE" not in script
    assert "BRIDGE_TOKEN=$(tr -d" in script
    assert "ANTHROPIC_API_KEY=$(tr -d" in script
    assert "ANTHROPIC_KEY_FILE=/root/clients/_shared/anthropic_api_key" in script
    assert "ICP bridge token loaded from $BRIDGE_TOKEN_FILE" in script
    assert "cat > \"$TENANT_DIR/docker-compose.yml\"" in script
    assert "docker network inspect unboks-control" in script
    assert "NR3_INTERNAL_OVERRIDES_URL=http://wtyj-admin:8010" in script
    assert "TENANT_RUNTIME_CONTROLS_REQUIRED=true" in script
    assert "TENANT_ACCOUNT_ALLOWLIST_REQUIRED=true" in script
    assert "python3 - <<'UNBOKS_NGINX_INSERT'" in script
    assert "# BEGIN UNBOKS TENANT one-paste" in script
    assert "docker compose down || true" in script
    assert "docker compose up -d" in script
    assert "nginx -t" in script
    assert "systemctl reload nginx" in script
    assert "https://dashboard.unboks.org/login?workspace=one-paste" in script


def test_create_shows_auto_provision_success_when_worker_succeeds(client, monkeypatch):
    from app.routes import admin

    def fake_auto_provision_tenant(**kwargs):
        return SimpleNamespace(
            status="succeeded",
            message="Tenant was provisioned.",
            job_id="job-123",
            details=("wrote tenant files", "nginx config tested and reloaded"),
            dashboard_url=kwargs["dashboard_url"],
            health_url="http://127.0.0.1:8123/health",
        )

    monkeypatch.setattr(admin, "auto_provision_tenant", fake_auto_provision_tenant)
    r = client.post(
        "/admin/tenants/create",
        data={"name": "Auto Provision", "slug": "auto-provision"},
        follow_redirects=False)
    assert r.status_code == 200
    assert "Automatic VPS provisioning succeeded" in r.text
    assert "Open tenant dashboard" in r.text
    assert "wrote tenant files" in r.text
    # Successful automatic provisioning must not render internal secrets
    # or fallback files that contain access_key / platform credentials.
    assert "ct-full-vps-setup" not in r.text
    assert "ct-client-json" not in r.text
    assert "ct-platform-env" not in r.text
    assert "access_key" not in r.text
    assert "Internal access keys were written by automatic provisioning" in r.text


def test_create_shows_auto_provision_failure_without_fake_success(
    client, monkeypatch, email_capture,
):
    from app.routes import admin

    def fake_auto_provision_tenant(**kwargs):
        return SimpleNamespace(
            status="failed",
            message="nginx config test failed",
            job_id="job-456",
            details=(),
            dashboard_url=kwargs["dashboard_url"],
            health_url="",
            safe_to_release=False,
        )

    monkeypatch.setattr(admin, "auto_provision_tenant", fake_auto_provision_tenant)
    r = client.post(
        "/admin/tenants/create",
        data={
            "name": "Failed Auto",
            "slug": "failed-auto",
            "contact_email": "owner@example.com",
            "send_welcome": "1",
        },
        follow_redirects=False)
    assert r.status_code == 200
    assert "Automatic provisioning failed" in r.text
    assert "nginx config test failed" in r.text
    assert "No success was faked" in r.text
    assert "remain reserved" in r.text
    assert "Tenant creation failed" in r.text
    assert "ct-full-vps-setup" not in r.text
    assert "ct-tenant-login" not in r.text
    registry_path = Path(os.environ["NR3_PORT_REGISTRY_PATH"])
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    assert registry["failed-auto"] == 8100
    state_path = Path(os.environ["NR3_ICP_STATE_PATH"])
    state = json.loads(state_path.read_text()) if state_path.exists() else {"tenants": {}}
    toggles = state["tenants"]["failed-auto"]["feature_toggles"]
    assert toggles["ai_auto_reply"]["value"] is False
    assert toggles["whatsapp_inbox"]["value"] is False
    assert toggles["facebook_dms"]["value"] is False
    assert email_capture == []
