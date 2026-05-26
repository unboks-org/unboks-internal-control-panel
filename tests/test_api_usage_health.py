import json
import sqlite3
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.main import app


def _write_tenant(root, slug="clinica-roberto"):
    cfg = root / slug / "config"
    data = root / slug / "data"
    cfg.mkdir(parents=True)
    data.mkdir(parents=True)
    (cfg / "client.json").write_text(json.dumps({
        "slug": slug,
        "name": "Clínica Roberto",
        "primary_language": "Spanish",
        "languages": ["Spanish"],
        "agent_name": "Marina",
        "whatsapp": "+5999000000",
        "website": "https://clinica.example",
        "clinical_guardrails": ["No diagnosis"],
    }), encoding="utf-8")
    return data / "state_registry.db"


def test_api_usage_health_reads_tenant_runtime_db(monkeypatch, tmp_path):
    tenant_root = tmp_path / "tenants"
    db_path = _write_tenant(tenant_root)
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenant_root))
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE api_usage_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT, client_slug TEXT, "
        "provider TEXT, model TEXT, feature_path TEXT, channel TEXT, timestamp TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, "
        "estimated_cost REAL, latency_ms INTEGER, success INTEGER, "
        "error_category TEXT, error_message TEXT, fallback_used INTEGER)"
    )
    conn.execute(
        "INSERT INTO api_usage_events VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "clinica-roberto", "clinica-roberto", "anthropic", "claude-sonnet-4-6",
            "WhatsApp Marina", "whatsapp", datetime.now(timezone.utc).isoformat(),
            10, 5, 15, 0.001, 123, 0, "billing_quota", "insufficient credits", 1,
        ),
    )
    conn.execute(
        "CREATE TABLE api_usage_alerts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, alert_key TEXT UNIQUE, tenant_id TEXT, "
        "provider TEXT, severity TEXT, category TEXT, message TEXT, details_json TEXT, "
        "created_at TEXT, updated_at TEXT, active INTEGER)"
    )
    conn.execute(
        "INSERT INTO api_usage_alerts VALUES (NULL,?,?,?,?,?,?,?,?,?,?)",
        (
            "clinica-roberto:anthropic:billing_quota",
            "clinica-roberto",
            "anthropic",
            "critical",
            "billing_quota",
            "Provider returned a billing, quota, or authentication error.",
            "{}",
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            1,
        ),
    )
    conn.commit()
    conn.close()

    from app.api_usage import tenant_api_health, platform_api_health
    tenant = tenant_api_health("clinica-roberto")
    assert tenant.tracked is True
    assert tenant.thirty_days.calls == 1
    assert tenant.error_count == 1
    assert tenant.fallback_count == 1
    assert tenant.status == "critical"
    assert tenant.alerts
    platform = platform_api_health()
    assert platform["totals"]["api_errors"] == 1
    assert platform["provider_health"] == "critical"
    assert platform["alerts"]


def test_workspace_renders_api_usage_section(monkeypatch, tmp_path):
    tenant_root = tmp_path / "tenants"
    _write_tenant(tenant_root)
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenant_root))
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})
    response = client.get("/admin/tenants/clinica-roberto")
    assert response.status_code == 200
    assert "API Usage &amp; Health" in response.text
    assert "Not tracked yet" in response.text
    assert "Clínica Roberto" in response.text
