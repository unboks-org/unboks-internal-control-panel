import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_INTERNAL_API_TOKEN", "bridge-token")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    token_dir = tmp_path / "bridge_tokens"
    token_dir.mkdir()
    (token_dir / "unboks").write_text("tenant-unboks-token-32-bytes-long", encoding="utf-8")
    monkeypatch.setenv("NR3_TENANT_BRIDGE_TOKEN_DIR", str(token_dir))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def _bridge_headers(tenant: str = "unboks") -> dict[str, str]:
    return {
        "Authorization": "Bearer tenant-unboks-token-32-bytes-long",
        "X-Tenant-Identity": tenant,
    }


def test_internal_overrides_requires_bridge_token(client):
    r = client.get("/internal/tenants/unboks/overrides")
    assert r.status_code == 403

    r2 = client.get(
        "/internal/tenants/unboks/overrides",
        headers={
            "Authorization": "Bearer wrong",
            "X-Tenant-Identity": "unboks",
        },
    )
    assert r2.status_code == 401


def test_internal_overrides_rejects_shared_token_when_tenant_token_exists(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers={
            "Authorization": "Bearer bridge-token",
            "X-Tenant-Identity": "unboks",
        },
    )
    assert r.status_code == 401


def test_internal_overrides_requires_tenant_identity_header(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers={"Authorization": "Bearer tenant-unboks-token-32-bytes-long"},
    )
    assert r.status_code == 403


def test_internal_overrides_rejects_tenant_identity_mismatch(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers("pepe"),
    )
    assert r.status_code == 403


def test_internal_overrides_reports_empty_available_envelope(client):
    r = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["tenant_id"] == "unboks"
    assert body["feature_toggles"] == {}
    assert body["channel_connections"] == {}
    assert body["display_metadata"] == {}
    assert body["sot_entries"] == []
    assert body["ai_agent_settings"] == {
        "tone": None,
        "escalation_rules": None,
        "agent_name": None,
    }
    assert body["response_timing"] is None


def test_channel_toggle_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/channels/whatsapp/toggle",
        follow_redirects=False,
    )
    assert r.status_code == 303

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    toggles = bridge.json()["feature_toggles"]
    assert toggles["whatsapp_inbox"]["value"] is True
    assert toggles["whatsapp_inbox"]["source"] == "icp_override"
    assert toggles["whatsapp_inbox"]["wired"] is True
    assert "whatsapp" not in toggles


def test_channel_toggle_off_reflects_false_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    client.post("/admin/tenants/unboks/channels/tiktok/toggle", follow_redirects=False)
    client.post("/admin/tenants/unboks/channels/tiktok/toggle", follow_redirects=False)

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    assert bridge.json()["feature_toggles"]["tiktok_dms"]["value"] is False


def test_agent_tone_override_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/agent/tone",
        data={
            "tone": "Calm, concise, professional",
            "tone_notes": "Use plain language and avoid legal promises.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("#agent-section")

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    tone = bridge.json()["ai_agent_settings"]["tone"]
    assert tone["tone"] == "Calm, concise, professional"
    assert tone["notes"] == "Use plain language and avoid legal promises."
    assert tone["source"] == "icp_override"


def test_agent_name_override_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/agent/name",
        data={"agent_name": "Sofia"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("#agent-section")

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    agent_name = bridge.json()["ai_agent_settings"]["agent_name"]
    assert agent_name["name"] == "Sofia"
    assert agent_name["source"] == "icp_override"


def test_response_timing_override_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/agent/response-timing",
        data={
            "preset": "patient",
            "delay_seconds": "15",
            "max_wait_seconds": "30",
            "batching_enabled": "on",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    timing = bridge.json()["response_timing"]
    assert timing["source"] == "icp_override"
    assert timing["settings"]["preset"] == "patient"
    assert timing["settings"]["delay_seconds"] == 15.0
    assert timing["settings"]["max_wait_seconds"] == 30.0


def test_agent_escalation_rules_override_is_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    r = client.post(
        "/admin/tenants/unboks/agent/escalation-rules",
        data={
            "soft_escalation_when": "Marina is unsure or needs Calvin to choose a time.",
            "hard_escalation_when": "Legal risk, angry customer, or refund dispute.",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("#agent-section")

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    rules = bridge.json()["ai_agent_settings"]["escalation_rules"]
    assert rules["soft_escalation"] == {
        "enabled": True,
        "when": "Marina is unsure or needs Calvin to choose a time.",
    }
    assert rules["hard_escalation"] == {
        "enabled": True,
        "when": "Legal risk, angry customer, or refund dispute.",
    }
    assert rules["source"] == "icp_override"


def test_agent_escalation_rules_blank_submit_clears_override(client):
    client.post("/login", data={"password": "test-password"})
    client.post(
        "/admin/tenants/unboks/agent/escalation-rules",
        data={
            "soft_escalation_when": "Needs a decision.",
            "hard_escalation_when": "Stop replying.",
        },
        follow_redirects=False,
    )
    cleared = client.post(
        "/admin/tenants/unboks/agent/escalation-rules",
        data={
            "soft_escalation_when": "",
            "hard_escalation_when": "",
        },
        follow_redirects=False,
    )
    assert cleared.status_code == 303

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    assert bridge.json()["ai_agent_settings"]["escalation_rules"] is None


def test_sot_entry_add_and_delete_are_visible_to_nr2_bridge(client):
    client.post("/login", data={"password": "test-password"})
    added = client.post(
        "/admin/tenants/unboks/sot",
        data={
            "title": "Consultation pricing",
            "category": "pricing",
            "content": "First consultation is free for up to 15 minutes.",
        },
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert added.headers["location"].endswith("#agent-section")

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge.status_code == 200
    entries = bridge.json()["sot_entries"]
    assert len(entries) == 1
    assert entries[0]["title"] == "Consultation pricing"
    assert entries[0]["category"] == "pricing"
    assert entries[0]["content"] == "First consultation is free for up to 15 minutes."
    assert entries[0]["source"] == "icp_override"

    deleted = client.post(
        f"/admin/tenants/unboks/sot/{entries[0]['id']}/delete",
        follow_redirects=False,
    )
    assert deleted.status_code == 303

    bridge_after_delete = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )
    assert bridge_after_delete.status_code == 200
    assert bridge_after_delete.json()["sot_entries"] == []


def test_whatsapp_connection_is_visible_to_nr2_bridge(client):
    from app import channel_connections

    channel_connections.upsert_tenant_channel_connection(
        tenant_id="unboks",
        status="connected",
        zernio_profile_id="profile_unboks",
        zernio_account_id="account_unboks",
        phone_number_id="phone_unboks",
        display_phone_number="+599 9 688 1585",
        last_request_id="cr_unboks",
    )

    bridge = client.get(
        "/internal/tenants/unboks/overrides",
        headers=_bridge_headers(),
    )

    assert bridge.status_code == 200
    whatsapp = bridge.json()["channel_connections"]["whatsapp"]
    assert whatsapp["provider"] == "zernio"
    assert whatsapp["status"] == "connected"
    assert whatsapp["connected"] is True
    assert whatsapp["display_phone_number"] == "+599 9 688 1585"
    assert whatsapp["zernio_account_id"] == "account_unboks"
