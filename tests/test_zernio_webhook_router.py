import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app


def _write_tenant(root, slug="test", name="Test"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    return TestClient(app)


def test_zernio_webhook_router_forwards_to_connected_tenant(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        display_phone_number="+599 9 694 5527",
    )
    seen = {}

    async def fake_forward(*, tenant_id, body, signature, content_type):
        seen["tenant_id"] = tenant_id
        seen["body"] = body
        seen["signature"] = signature
        seen["content_type"] = content_type
        return 200, "OK"

    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant",
        fake_forward,
    )

    payload = {
        "event": "message.received",
        "data": {
            "accountId": "account_test",
            "conversationId": "conversation_1",
            "id": "message_1",
            "platform": "whatsapp",
            "text": "hello",
        },
    }
    response = client.post(
        "/internal/api/zernio/webhook-router",
        json=payload,
        headers={"X-Zernio-Signature": "sig_123"},
    )

    assert response.status_code == 200
    assert seen["tenant_id"] == "test"
    assert json.loads(seen["body"]) == payload
    assert seen["signature"] == "sig_123"


def test_zernio_webhook_router_accepts_unmapped_account(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/internal/api/zernio/webhook-router",
        json={"event": "message.received", "data": {"accountId": "unknown"}},
    )

    assert response.status_code == 202
