import json

import pytest
from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    tenants_root = tmp_path / "tenants"
    for slug, name in (("roberto", "Roberto"), ("lawyer", "Lawyer")):
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


def _set_allowlist(tmp_path, slug, allowlist):
    path = tmp_path / "tenants" / slug / "config" / "client.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["channel_account_allowlist"] = allowlist
    path.write_text(json.dumps(data), encoding="utf-8")


def test_sidebar_shows_connected_whatsapp_badge(client, tmp_path):
    _set_allowlist(
        tmp_path,
        "roberto",
        {"mode": "strict", "zernio_accounts": ["account_roberto"]},
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="roberto",
        status="connected",
        zernio_profile_id="profile_roberto",
        zernio_account_id="account_roberto",
        phone_number_id="phone_roberto",
        display_phone_number="+599 9 123 4567",
        last_request_id="cr_roberto",
    )

    response = client.get("/admin/tenants/roberto")

    assert response.status_code == 200
    assert "WhatsApp: Connected" in response.text
    assert "Strict" in response.text
    assert "tenant-wa-connected" in response.text
    assert "tenant-wa-critical" not in response.text
    assert "account_roberto" not in response.text
    assert "accoun...erto" in response.text
    assert "data-wa-connected-toast" in response.text
    assert "+599 9 123 4567" in response.text


def test_sidebar_shows_pending_when_link_generated(client):
    channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_lawyer",
        status="link_generated",
    )

    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert "WhatsApp: Awaiting activation" in response.text
    assert "tenant-wa-pending" in response.text
    assert "data-wa-connected-toast" not in response.text


def test_sidebar_shows_awaiting_activation_for_tenant_connect_token(client, tmp_path):
    config_dir = tmp_path / "tenants" / "clinica-roberto" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({
            "slug": "clinica-roberto",
            "name": "Clinica Roberto",
            "status": "active",
            "whatsapp_connect_token": "token-for-client-start",
        }),
        encoding="utf-8",
    )

    response = client.get("/admin/tenants/clinica-roberto")

    assert response.status_code == 200
    assert "WhatsApp: Awaiting activation" in response.text
    assert "tenant-wa-pending" in response.text
    assert "data-wa-connected-toast" not in response.text


def test_sidebar_hides_whatsapp_badge_without_connection(client):
    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert "WhatsApp: Connected" not in response.text
    assert "WhatsApp: Pending" not in response.text
    assert "tenant-wa-connected" not in response.text


def test_connected_whatsapp_without_allowlist_shows_critical(client):
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        phone_number_id="phone_lawyer",
        display_phone_number="+599 9 765 4321",
        last_request_id="cr_lawyer",
    )

    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert "WhatsApp: Critical: Missing strict allowlist" in response.text
    assert "tenant-wa-critical" in response.text
    assert "tenant-wa-connected" not in response.text
    assert "data-wa-connected-toast" not in response.text


@pytest.mark.parametrize(
    ("allowlist", "expected"),
    [
        ({"mode": "permissive", "zernio_accounts": ["account_lawyer"]}, "Allowlist is not strict"),
        ({"mode": "strict", "zernio_accounts": []}, "Allowlist is empty"),
        ({"mode": "strict", "zernio_accounts": ["*"]}, "Allowlist is permissive"),
        (
            {"mode": "strict", "zernio_accounts": ["account_other"]},
            "Connected account not allowlisted",
        ),
    ],
)
def test_connected_whatsapp_rejects_non_strict_allowlists(
    client,
    tmp_path,
    allowlist,
    expected,
):
    _set_allowlist(tmp_path, "lawyer", allowlist)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_lawyer",
        phone_number_id="phone_lawyer",
        display_phone_number="+599 9 765 4321",
        last_request_id="cr_lawyer",
    )

    response = client.get("/admin/tenants/lawyer")

    assert response.status_code == 200
    assert f"WhatsApp: Critical: {expected}" in response.text
    assert "tenant-wa-critical" in response.text
    assert "tenant-wa-connected" not in response.text
    assert "data-wa-connected-toast" not in response.text
