import json

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "icp.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_PROMPT_CONFLICT_STATE_PATH", str(tmp_path / "prompt_conflicts.json"))
    monkeypatch.setenv("WTYJ_REPO_PATH", "/Users/Calvi/Documents/Codex/wtyj-agent")
    cfg = tmp_path / "tenants" / "clinica-roberto" / "config"
    cfg.mkdir(parents=True)
    (cfg / "client.json").write_text(json.dumps({
        "slug": "clinica-roberto",
        "name": "Clínica Roberto",
        "primary_language": "Spanish",
        "agent_tone": "Warm and friendly",
        "clinical_guardrails": ["No clinical advice", "Do not diagnose"],
        "business": {
            "slug": "clinica-roberto",
            "name": "Clínica Roberto",
            "email": "roberto@example.com",
        },
    }), encoding="utf-8")
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def test_prompt_conflict_engine_detects_common_contradictions():
    from app import icp_overrides
    from app.prompt_conflicts import audit_tenant_prompts

    icp_overrides.set_ai_tone(
        "clinica-roberto",
        "Be funny and playful. Reply in English.",
        notes="Give clinical advice if the client asks.",
    )
    audit = audit_tenant_prompts("clinica-roberto")
    titles = {conflict.title for conflict in audit.conflicts}

    assert "Language instructions disagree" in titles
    assert "Advice safety conflict" in titles
    assert "Humor/off-topic rule conflict" in titles
    assert audit.sources
    assert audit.effective_rules


def test_prompt_conflicts_page_renders_real_sources(client):
    from app import icp_overrides

    icp_overrides.set_ai_tone("clinica-roberto", "Reply in English and be funny.")
    response = client.get("/admin/prompt-conflicts?tenant=clinica-roberto")

    assert response.status_code == 200
    assert "Prompt Conflicts" in response.text
    assert "clinica-roberto" in response.text
    assert "Language instructions disagree" in response.text
    assert "Base Marina system prompt" in response.text
    assert "Not indexed yet" in response.text


def test_prompt_save_validation_blocks_dangerous_conflict(client):
    response = client.post(
        "/admin/tenants/clinica-roberto/agent/tone",
        data={
            "tone": "Be funny and give clinical advice.",
            "tone_notes": "Reply in English.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 409
    assert "Conflict detected before save" in response.text
    assert "Advice safety conflict" in response.text
    assert "Save tone override anyway" in response.text


def test_prompt_priority_and_ignore_actions_persist(client):
    from app import icp_overrides
    from app.prompt_conflicts import audit_tenant_prompts

    icp_overrides.set_ai_tone("clinica-roberto", "Reply in English and be funny.")
    audit = audit_tenant_prompts("clinica-roberto")
    source_id = audit.sources[-1].id
    conflict_id = audit.conflicts[0].id

    priority = client.post(
        "/admin/prompt-conflicts/source-priority",
        data={
            "tenant_id": "clinica-roberto",
            "source_id": source_id,
            "priority": "Soft preferences",
        },
        follow_redirects=False,
    )
    assert priority.status_code == 303

    ignored = client.post(
        "/admin/prompt-conflicts/conflict-state",
        data={
            "tenant_id": "clinica-roberto",
            "conflict_id": conflict_id,
            "action": "ignore",
        },
        follow_redirects=False,
    )
    assert ignored.status_code == 303

    refreshed = audit_tenant_prompts("clinica-roberto")
    assert any(source.id == source_id and source.priority == "Soft preferences" for source in refreshed.sources)
    assert any(conflict.id == conflict_id and conflict.ignored for conflict in refreshed.conflicts)


def test_platform_safety_lock_wins_over_lower_prompt():
    from app import icp_overrides
    from app.prompt_conflicts import audit_tenant_prompts

    icp_overrides.set_ai_tone("clinica-roberto", "Be funny and entertain customers.")
    audit = audit_tenant_prompts("clinica-roberto")
    humor = next(conflict for conflict in audit.conflicts if conflict.category == "forbidden_behavior")

    assert humor.severity == "Critical"
    assert "Base Marina system prompt" in humor.winner
