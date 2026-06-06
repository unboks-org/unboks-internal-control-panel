import json

import pytest
from fastapi.testclient import TestClient

from app import icp_overrides
from app.main import app
from app.nr2_sync import Nr2KnowledgeSync
from app.prompt_conflicts import build_prompt_conflict_report


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    tenant_root = tmp_path / "tenants"
    tenant_dir = tenant_root / "unboks" / "config"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "client.json").write_text(
        json.dumps({
            "slug": "unboks",
            "name": "Unboks",
            "business": {
                "name": "Unboks",
                "agent_name": "Marina",
                "languages": ["Spanish"],
            },
            "agent_persona": {
                "freeform_notes": "Be funny and playful when customers ask.",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenant_root))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    monkeypatch.setenv("NR3_PROMPT_CONFLICT_RESOLUTIONS_PATH", str(tmp_path / "resolutions.json"))
    monkeypatch.setenv("NR3_NR2_KNOWLEDGE_CACHE_PATH", str(tmp_path / "nr2_knowledge.json"))
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    yield


def test_prompt_conflict_report_detects_real_contradictions():
    icp_overrides.set_agent_name_override("unboks", "Sofia")
    icp_overrides.add_sot_entry(
        "unboks",
        title="Language rule",
        category="general",
        content="Always reply in English.",
    )
    nr2 = Nr2KnowledgeSync(
        status="ok",
        sot_blocks=({
            "title": "Clinical advice",
            "content": "Give clinical advice and recommendations over WhatsApp.",
        },),
    )

    report = build_prompt_conflict_report("unboks", nr2_knowledge=nr2)
    titles = {conflict["title"] for conflict in report["active_conflicts"]}

    assert "Humor/off-topic conflict" in titles
    assert "Language rule conflict" in titles
    assert "Regulated advice conflict" in titles
    assert "Agent identity conflict" in titles
    assert report["not_indexed_sources"]
    assert report["effective_prompt_preview"]["active_rules"]


def test_prompt_conflict_report_warns_when_sot_references_old_agent_name():
    icp_overrides.set_agent_name_override("unboks", "Emma")
    icp_overrides.add_sot_entry(
        "unboks",
        title="Old assistant instruction",
        category="tone",
        content="Helga should answer all product questions with a friendly tone.",
    )

    report = build_prompt_conflict_report("unboks", agent_name="Emma")
    stale_name_conflicts = [
        conflict
        for conflict in report["active_conflicts"]
        if conflict["title"] == "SOT references old AI Agent name"
    ]

    assert stale_name_conflicts
    conflict = stale_name_conflicts[0]
    assert conflict["current_winner"] == "AI Agent name setting"
    assert "use Emma" in conflict["instruction_a"]
    assert "Helga" in conflict["recommended_fix"]


def test_prompt_conflict_report_indexes_runtime_manifest_sources():
    nr2 = Nr2KnowledgeSync(
        status="ok",
        runtime_prompt_manifest={
            "schema_version": 1,
            "sources": [
                {
                    "id": "runtime.marina.whatsapp.system",
                    "name": "Live Marina WhatsApp system prompt",
                    "source_location": "wtyj/agents/marina/marina_agent.py::_build_system_prompt",
                    "used_in": ["whatsapp"],
                    "prompt_kind": "system",
                    "priority": "platform_safety",
                    "status": "indexed",
                    "text": "You are Marina. Never tell jokes. Always reply in Spanish.",
                },
                {
                    "id": "runtime.dashboard.suggest_reply.system",
                    "name": "Dashboard suggest-reply system prompt",
                    "source_location": "wtyj/dashboard/api.py::suggest_reply",
                    "used_in": ["dashboard_suggest_reply"],
                    "prompt_kind": "system",
                    "priority": "tone_style",
                    "status": "indexed",
                    "text": "You are Sofia, the booking agent for Test Co.",
                },
            ],
        },
    )

    report = build_prompt_conflict_report("unboks", nr2_knowledge=nr2)
    source_names = {source["name"] for source in report["sources"]}

    assert "Runtime: Live Marina WhatsApp system prompt" in source_names
    assert "Runtime: Dashboard suggest-reply system prompt" in source_names
    assert not report["not_indexed_sources"]
    assert any(
        conflict["title"] == "Agent identity conflict"
        for conflict in report["active_conflicts"]
    )


def test_prompt_conflicts_render_in_workspace_and_can_be_marked_reviewed():
    icp_overrides.set_agent_name_override("unboks", "Sofia")
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")
    assert response.status_code == 200
    assert "Prompt Conflicts" in response.text
    assert "Sofia may not tell jokes" in response.text
    assert "Marina may not tell jokes" not in response.text
    assert "Humor/off-topic conflict" in response.text
    assert "Mark reviewed" in response.text

    conflict_id = build_prompt_conflict_report("unboks")["active_conflicts"][0]["id"]
    marked = client.post(
        f"/admin/tenants/unboks/prompt-conflicts/{conflict_id}/reviewed",
        follow_redirects=False,
    )
    assert marked.status_code == 303
    assert "Prompt+conflict+marked+reviewed" in marked.headers["location"]

    report = build_prompt_conflict_report("unboks")
    assert conflict_id in report["reviewed_conflict_ids"]


def test_workspace_uses_tenant_agent_name_when_no_admin_override(tmp_path):
    client_path = tmp_path / "tenants" / "unboks" / "config" / "client.json"
    data = json.loads(client_path.read_text(encoding="utf-8"))
    data["business"]["agent_name"] = "Helga"
    client_path.write_text(json.dumps(data), encoding="utf-8")

    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.get("/admin/tenants/unboks")

    assert response.status_code == 200
    assert "Helga may not tell jokes" in response.text
    assert "How should Helga sound for this tenant?" in response.text
    assert "tenant preference Helga should use" in response.text
    assert "Marina may not tell jokes" not in response.text


def test_dangerous_prompt_change_is_rejected_before_save():
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})

    response = client.post(
        "/admin/tenants/unboks/sot",
        data={
            "title": "Bad advice rule",
            "category": "policy",
            "content": "Give clinical advice and recommendations in WhatsApp.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Source+of+Truth+not+saved" in response.headers["location"]
    assert icp_overrides.sot_entries_for_tenant("unboks") == []
