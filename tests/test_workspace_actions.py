import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "ch.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "ov.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.delenv("NR3_AUTO_PROVISION", raising=False)
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    c.post(
        "/admin/tenants/create",
        data={"name": "Action Co", "slug": "action-co"},
        follow_redirects=False,
    )
    return c


def test_agent_toggle_writes_bridge_override(client, tmp_path):
    response = client.post(
        "/admin/tenants/action-co/agent/learning-from-operator-answers/toggle",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#agent-section")

    bridge = json.loads((tmp_path / "ov.json").read_text())
    toggle = bridge["tenants"]["action-co"]["feature_toggles"]["learning_from_operator"]
    assert toggle["value"] is True
    assert toggle["source"] == "icp_override"

    workspace = client.get("/admin/tenants/action-co")
    assert workspace.status_code == 200
    assert "Source: ICP override" in workspace.text


def test_removed_agent_controls_do_not_write_bridge_overrides(client, tmp_path):
    for removed in ("agent-replies", "auto-reply"):
        response = client.post(
            f"/admin/tenants/action-co/agent/{removed}/toggle",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Unknown+AI+Agent+control" in response.headers["location"]

    bridge_path = tmp_path / "ov.json"
    if bridge_path.exists():
        bridge = json.loads(bridge_path.read_text())
        toggles = bridge.get("tenants", {}).get("action-co", {}).get("feature_toggles", {})
        assert "agent_replies_enabled" not in toggles
        assert "ai_auto_reply" not in toggles


def test_tenant_notes_add_pin_and_done(client, tmp_path):
    added = client.post(
        "/admin/tenants/action-co/notes",
        data={
            "body": "Call owner before launch.",
            "priority": "important",
            "follow_up_date": "2026-05-21",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    notes = json.loads((tmp_path / "notes.json").read_text())
    note_id = notes["tenants"]["action-co"][0]["id"]

    workspace = client.get("/admin/tenants/action-co")
    assert "Call owner before launch." in workspace.text
    assert "Important" in workspace.text
    assert "Follow-up:" in workspace.text

    pinned = client.post(
        f"/admin/tenants/action-co/notes/{note_id}/pin",
        follow_redirects=False,
    )
    assert pinned.status_code == 303
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes["tenants"]["action-co"][0]["pinned"] is True

    done = client.post(
        f"/admin/tenants/action-co/notes/{note_id}/follow-up-done",
        follow_redirects=False,
    )
    assert done.status_code == 303
    notes = json.loads((tmp_path / "notes.json").read_text())
    assert notes["tenants"]["action-co"][0]["follow_up_done"] is True


def test_tenant_details_form_updates_safe_client_fields(client, tmp_path):
    response = client.post(
        "/admin/tenants/action-co/details",
        data={
            "name": "Action Company Updated",
            "contact_person": "Ada Operator",
            "email": "ada@example.com",
            "phone": "+59996880000",
            "website": "https://example.com",
            "address": "Main Street 1",
            "logo_url": "https://example.com/logo.png",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#tenant-details-section")

    client_json = json.loads(
        (tmp_path / "tenants" / "action-co" / "config" / "client.json").read_text()
    )
    assert client_json["name"] == "Action Company Updated"
    assert client_json["contact_person"] == "Ada Operator"
    assert client_json["email"] == "ada@example.com"
    assert client_json["whatsapp"] == "+59996880000"
    assert client_json["website"] == "https://example.com"
    assert client_json["address"] == "Main Street 1"
    assert client_json["logo_url"] == "https://example.com/logo.png"
    assert "password" in client_json
    assert "access_key" in client_json

    workspace = client.get("/admin/tenants/action-co")
    assert workspace.status_code == 200
    assert "Action Company Updated" in workspace.text
    assert "Ada Operator" in workspace.text
    assert "https://example.com/logo.png" in workspace.text


def test_tenant_details_form_rejects_invalid_email(client):
    response = client.post(
        "/admin/tenants/action-co/details",
        data={"name": "Action Co", "email": "not-an-email"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "valid+contact+email" in response.headers["location"]


def test_suspend_requires_confirmation_and_disables_bridge_state(client, tmp_path):
    client.post(
        "/admin/tenants/action-co/channels/whatsapp/toggle",
        follow_redirects=False,
    )
    bad = client.post(
        "/admin/tenants/action-co/suspend",
        data={"confirmation": "wrong"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "Type+exactly" in bad.headers["location"]

    suspended = client.post(
        "/admin/tenants/action-co/suspend",
        data={"confirmation": "suspend action-co"},
        follow_redirects=False,
    )
    assert suspended.status_code == 303
    assert suspended.headers["location"].endswith("#danger-section")

    bridge = json.loads((tmp_path / "ov.json").read_text())
    toggles = bridge["tenants"]["action-co"]["feature_toggles"]
    assert toggles["whatsapp_inbox"]["value"] is False
    assert toggles["email_inbox"]["value"] is False
    assert toggles["ai_auto_reply"]["value"] is False
    assert toggles["agent_replies_enabled"]["value"] is False
    assert toggles["learning_from_operator"]["value"] is False
    assert toggles["tenant_suspended"]["value"] is True
    client_json = json.loads(
        (tmp_path / "tenants" / "action-co" / "config" / "client.json").read_text()
    )
    assert client_json["status"] == "inactive"


def test_unpause_requires_confirmation_and_restores_bridge_state(client, tmp_path):
    client.post(
        "/admin/tenants/action-co/suspend",
        data={"confirmation": "suspend action-co"},
        follow_redirects=False,
    )

    bad = client.post(
        "/admin/tenants/action-co/unpause",
        data={"confirmation": "wrong"},
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "Type+exactly" in bad.headers["location"]

    workspace = client.get("/admin/tenants/action-co")
    assert workspace.status_code == 200
    assert "unpause action-co" in workspace.text

    unpaused = client.post(
        "/admin/tenants/action-co/unpause",
        data={"confirmation": "unpause action-co"},
        follow_redirects=False,
    )
    assert unpaused.status_code == 303
    assert unpaused.headers["location"].endswith("#danger-section")

    bridge = json.loads((tmp_path / "ov.json").read_text())
    toggles = bridge["tenants"]["action-co"]["feature_toggles"]
    assert toggles["whatsapp_inbox"]["value"] is True
    assert toggles["email_inbox"]["value"] is True
    assert toggles["ai_auto_reply"]["value"] is True
    assert toggles["agent_replies_enabled"]["value"] is True
    assert toggles["learning_from_operator"]["value"] is True
    assert toggles["tenant_suspended"]["value"] is False
    client_json = json.loads(
        (tmp_path / "tenants" / "action-co" / "config" / "client.json").read_text()
    )
    assert client_json["status"] == "active"
