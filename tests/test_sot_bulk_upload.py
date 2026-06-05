import pytest
from fastapi.testclient import TestClient

from app import icp_overrides
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
    client = TestClient(app)
    client.post("/login", data={"password": "test-password"})
    return client


def test_bulk_extract_txt_returns_structured_preview(client):
    response = client.post(
        "/admin/source-of-truth/bulk-extract",
        data={"tenant_id": "unboks"},
        files={
            "file": (
                "business.txt",
                b"Payment method: cash only\nDelivery: delivery is available in Willemstad\n",
                "text/plain",
            )
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["modelUsed"] == "deterministic-sot-parser-v1"
    assert {entry["category"] for entry in body["entries"]} >= {"payment", "delivery"}
    assert all(entry["sourceExcerpt"] for entry in body["entries"])


def test_bulk_extract_rejects_non_txt_file(client):
    response = client.post(
        "/admin/source-of-truth/bulk-extract",
        data={"tenant_id": "unboks"},
        files={"file": ("business.pdf", b"Payment method: cash only", "application/pdf")},
    )

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert ".txt" in response.json()["error"]


def test_bulk_save_adds_selected_entries(client):
    response = client.post(
        "/admin/source-of-truth/bulk-save",
        json={
            "tenantId": "unboks",
            "entries": [
                {
                    "selected": True,
                    "title": "Payment Method",
                    "category": "payment",
                    "fact": "Payment is cash only.",
                    "confidence": 0.96,
                    "sourceExcerpt": "Payment method: cash only",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["savedCount"] == 1
    entries = icp_overrides.sot_entries_for_tenant("unboks")
    assert entries[0]["title"] == "Payment Method"
    assert entries[0]["content"] == "Payment is cash only."
    assert entries[0]["updated_by"] == "nr3-bulk-upload"


def test_bulk_save_skips_possible_duplicate(client):
    icp_overrides.add_sot_entry(
        "unboks",
        title="Payment Method",
        category="payment",
        content="Payment is cash only.",
    )

    response = client.post(
        "/admin/source-of-truth/bulk-save",
        json={
            "tenantId": "unboks",
            "entries": [
                {
                    "selected": True,
                    "title": "Payment Method",
                    "category": "payment",
                    "fact": "Payment is cash only.",
                    "confidence": 0.96,
                    "sourceExcerpt": "Payment method: cash only",
                }
            ],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["savedCount"] == 0
    assert "duplicate" in body["skipped"][0]["reason"]
    assert len(icp_overrides.sot_entries_for_tenant("unboks")) == 1
