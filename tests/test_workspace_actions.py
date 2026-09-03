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
    bridge_path = tmp_path / "ov.json"
    before = json.loads(bridge_path.read_text())

    for removed in ("agent-replies", "auto-reply"):
        response = client.post(
            f"/admin/tenants/action-co/agent/{removed}/toggle",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "Unknown+AI+Agent+control" in response.headers["location"]

    after = json.loads(bridge_path.read_text())
    assert after == before
    toggles = after["tenants"]["action-co"]["feature_toggles"]
    assert "agent_replies_enabled" not in toggles
    assert toggles["ai_auto_reply"]["value"] is False
    assert toggles["ai_auto_reply"]["updated_by"] == "tenant-provisioner"


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


def test_tenant_note_mutations_fail_closed_without_overwriting_malformed_store(
    client, tmp_path,
):
    from app import tenant_notes

    notes_path = tmp_path / "notes.json"
    original = '{"version":1,"tenants":[]}'
    notes_path.write_text(original, encoding="utf-8")

    # Workspace display remains tolerant while every writer rejects the
    # malformed shared envelope.
    assert tenant_notes.list_notes("action-co") == ()
    with pytest.raises(RuntimeError, match="malformed"):
        tenant_notes.add_note("action-co", "Must not replace the store")
    assert notes_path.read_text(encoding="utf-8") == original

    # Permanent deletion depends on this exception reaching the durable
    # cleanup flow rather than being converted into a successful no-op.
    with pytest.raises(RuntimeError, match="malformed"):
        tenant_notes.forget_tenant("action-co")
    assert notes_path.read_text(encoding="utf-8") == original


def test_tenant_note_mutation_and_cleanup_preserve_other_tenant_state(
    client, tmp_path,
):
    from app import tenant_notes

    notes_path = tmp_path / "notes.json"
    peer_notes = [
        {
            "id": "note-peer",
            "body": "Keep this note exactly.",
            "author": "Operator",
            "created_at": "2026-09-01T12:00:00+00:00",
            "priority": "important",
            "pinned": True,
            "follow_up_date": None,
            "follow_up_done": False,
            "future_field": {"keep": True},
        }
    ]
    notes_path.write_text(
        json.dumps(
            {
                "version": 7,
                "tenants": {
                    "action-co": [],
                    "peer-tenant": peer_notes,
                },
            }
        ),
        encoding="utf-8",
    )

    tenant_notes.add_note("action-co", "New isolated note")
    after_mutation = json.loads(notes_path.read_text(encoding="utf-8"))
    assert after_mutation["version"] == 7
    assert after_mutation["tenants"]["peer-tenant"] == peer_notes
    assert after_mutation["tenants"]["action-co"][0]["body"] == "New isolated note"

    assert tenant_notes.forget_tenant("action-co") is True
    assert json.loads(notes_path.read_text(encoding="utf-8")) == {
        "version": 7,
        "tenants": {"peer-tenant": peer_notes},
    }


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
    from app.channel_connections import current_tenant_generation_id

    generation_id = current_tenant_generation_id("action-co")
    client.post(
        "/admin/tenants/action-co/channels/email/toggle",
        follow_redirects=False,
    )
    bad = client.post(
        "/admin/tenants/action-co/suspend",
        data={
            "confirmation": "wrong",
            "tenant_generation_id": generation_id,
        },
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "Type+exactly" in bad.headers["location"]

    suspended = client.post(
        "/admin/tenants/action-co/suspend",
        data={
            "confirmation": "suspend action-co",
            "tenant_generation_id": generation_id,
        },
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


def test_unpause_requires_confirmation_and_stays_paused_without_host_success(client, tmp_path):
    from app.channel_connections import current_tenant_generation_id

    generation_id = current_tenant_generation_id("action-co")
    client.post(
        "/admin/tenants/action-co/suspend",
        data={
            "confirmation": "suspend action-co",
            "tenant_generation_id": generation_id,
        },
        follow_redirects=False,
    )

    bad = client.post(
        "/admin/tenants/action-co/unpause",
        data={
            "confirmation": "wrong",
            "tenant_generation_id": generation_id,
        },
        follow_redirects=False,
    )
    assert bad.status_code == 303
    assert "Type+exactly" in bad.headers["location"]

    workspace = client.get("/admin/tenants/action-co")
    assert workspace.status_code == 200
    assert "unpause action-co" in workspace.text

    unpaused = client.post(
        "/admin/tenants/action-co/unpause",
        data={
            "confirmation": "unpause action-co",
            "tenant_generation_id": generation_id,
        },
        follow_redirects=False,
    )
    assert unpaused.status_code == 303
    assert unpaused.headers["location"].endswith("#danger-section")

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


def test_stale_workspace_cannot_reset_recreated_tenant_password(
    client, monkeypatch,
):
    from app.channel_connections import current_tenant_generation_id
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock

    old_generation = current_tenant_generation_id("action-co")
    operation = start_delete_operation(
        slug="action-co",
        tenant_generation_id=old_generation,
        generation_fingerprint="sha256:" + "9" * 64,
        account_ids=[],
        profile_ids=[],
    )
    update_delete_operation(
        slug="action-co",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    with tenant_creation_lock("action-co"):
        bind_tenant_generation_for_creation(
            slug="action-co",
            generation_id="replacement-generation",
            status="active",
        )
    queued: list[dict] = []
    monkeypatch.setattr(
        "app.routes.admin.queue_tenant_host_action",
        lambda **kwargs: queued.append(kwargs),
    )

    response = client.post(
        "/admin/tenants/action-co/password-reset/temp",
        data={
            "confirmation": "reset password action-co",
            "tenant_generation_id": old_generation,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "blocked" in response.headers["location"].lower()
    assert queued == []


def test_stale_workspace_cannot_suspend_or_unpause_recreated_tenant(
    client, monkeypatch,
):
    from app.channel_connections import current_tenant_generation_id
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock

    old_generation = current_tenant_generation_id("action-co")
    operation = start_delete_operation(
        slug="action-co",
        tenant_generation_id=old_generation,
        generation_fingerprint="sha256:" + "8" * 64,
        account_ids=[],
        profile_ids=[],
    )
    update_delete_operation(
        slug="action-co",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    with tenant_creation_lock("action-co"):
        bind_tenant_generation_for_creation(
            slug="action-co",
            generation_id="replacement-generation",
            status="active",
        )

    mutations: list[str] = []
    monkeypatch.setattr(
        "app.channel_state.set_all_channels",
        lambda *_args, **_kwargs: mutations.append("channels"),
    )
    monkeypatch.setattr(
        "app.icp_overrides.set_feature_toggle",
        lambda *_args, **_kwargs: mutations.append("override"),
    )
    monkeypatch.setattr(
        "app.routes.admin.queue_tenant_host_action",
        lambda **_kwargs: mutations.append("queue"),
    )
    monkeypatch.setattr(
        "app.routes.admin.update_tenant_status",
        lambda *_args, **_kwargs: mutations.append("status"),
    )

    suspended = client.post(
        "/admin/tenants/action-co/suspend",
        data={
            "confirmation": "suspend action-co",
            "tenant_generation_id": old_generation,
        },
        follow_redirects=False,
    )
    unpaused = client.post(
        "/admin/tenants/action-co/unpause",
        data={
            "confirmation": "unpause action-co",
            "tenant_generation_id": old_generation,
        },
        follow_redirects=False,
    )

    assert suspended.status_code == 303
    assert unpaused.status_code == 303
    assert "blocked" in suspended.headers["location"].lower()
    assert "blocked" in unpaused.headers["location"].lower()
    assert mutations == []
