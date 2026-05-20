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
        "/admin/tenants/action-co/agent/auto-reply/toggle",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("#agent-section")

    bridge = json.loads((tmp_path / "ov.json").read_text())
    toggle = bridge["tenants"]["action-co"]["feature_toggles"]["ai_auto_reply"]
    assert toggle["value"] is True
    assert toggle["source"] == "icp_override"

    workspace = client.get("/admin/tenants/action-co")
    assert workspace.status_code == 200
    assert "Source: ICP override" in workspace.text


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
