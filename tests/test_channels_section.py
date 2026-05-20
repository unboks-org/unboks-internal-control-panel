"""Channels section in the tenant workspace + toggle persistence.

Calvin's brief: render a Channels section (2nd block, after AI
Agent) with green/red toggles for each channel. Toggle state
persists between renders so a click survives a refresh.
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "ch.json"))
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


# --- workspace renders Channels section ----------------------------


def test_channels_section_rendered_in_workspace(client):
    r = client.get("/admin/tenants/unboks")
    assert r.status_code == 200
    # The section header.
    assert "channels-section" in r.text
    assert "channels-panel" in r.text
    # All 8 channels listed.
    for label in ("WhatsApp", "Email", "Instagram", "Facebook",
                   "Messenger", "Telegram", "Tiktok", "X"):
        assert f">{label}<" in r.text, f"missing channel: {label}"


def test_channels_default_all_off(client):
    """Fresh tenant with no toggles yet -> every channel renders Off."""
    r = client.get("/admin/tenants/unboks")
    assert r.status_code == 200
    # 8 channels, all Off.
    assert r.text.count("channel-toggle is-off") == 8
    assert r.text.count("channel-toggle is-on") == 0


# --- toggle works + persists --------------------------------------


def test_toggle_one_channel_then_render_again(client, tmp_path):
    r = client.post(
        "/admin/tenants/unboks/channels/whatsapp/toggle",
        follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/admin/tenants/unboks#channels-section"

    # State file written.
    state_path = tmp_path / "ch.json"
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["unboks"]["whatsapp"] is True

    # Re-render the workspace: WhatsApp now shows On, the others Off.
    r2 = client.get("/admin/tenants/unboks")
    assert r2.status_code == 200
    assert r2.text.count("channel-toggle is-on") == 1
    assert r2.text.count("channel-toggle is-off") == 7


def test_toggle_round_trip(client):
    """Click twice -> back to Off."""
    client.post("/admin/tenants/unboks/channels/email/toggle",
                 follow_redirects=False)
    client.post("/admin/tenants/unboks/channels/email/toggle",
                 follow_redirects=False)
    r = client.get("/admin/tenants/unboks")
    assert r.text.count("channel-toggle is-on") == 0


def test_toggle_rejects_unknown_channel(client, tmp_path):
    """A tampered URL with a not-in-CHANNEL_KEYS value must not
    persist anything into the state file."""
    r = client.post(
        "/admin/tenants/unboks/channels/sms/toggle",
        follow_redirects=False)
    # 303 either way (idempotent redirect), but the file must
    # either not exist or contain an empty tenant block.
    state_path = tmp_path / "ch.json"
    if state_path.exists():
        data = json.loads(state_path.read_text())
        assert "sms" not in (data.get("unboks") or {})


def test_toggle_isolates_tenants(client):
    """Toggling whatsapp for tenant A must not flip it for tenant B.
    Provision BOTH tenants via the wizard so list_tenants() sees both
    on disk (avoiding the unboks fallback that would silently drop out
    once any real tenant lands on disk)."""
    client.post("/admin/tenants/create",
                 data={"name": "Alpha", "slug": "alpha"},
                 follow_redirects=False)
    client.post("/admin/tenants/create",
                 data={"name": "Bravo", "slug": "bravo"},
                 follow_redirects=False)
    client.post("/admin/tenants/alpha/channels/whatsapp/toggle",
                 follow_redirects=False)
    r_alpha = client.get("/admin/tenants/alpha")
    r_bravo = client.get("/admin/tenants/bravo")
    assert r_alpha.text.count("channel-toggle is-on") == 1
    assert r_bravo.text.count("channel-toggle is-on") == 0


# --- sidebar slug display ------------------------------------------


def test_sidebar_renders_slug_under_name(client):
    """After J3-CONTROL: sidebar rows show slug + name + status dot."""
    r = client.get("/admin/tenants/unboks")
    assert r.status_code == 200
    assert 'class="tenant-selector-name">Unboks<' in r.text
    assert 'class="tenant-selector-slug muted">unboks<' in r.text
    # Status-coloured dot still present.
    assert 'tenant-selector-dot tenant-status-' in r.text
