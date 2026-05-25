import json
import hashlib
import hmac

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.zernio import ZernioAccountSummary


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
    monkeypatch.setenv("ZERNIO_WEBHOOK_SECRET", "test-webhook-secret")
    return TestClient(app)


def _signature(body: bytes, secret: str = "test-webhook-secret") -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


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
    body = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 200
    assert seen["tenant_id"] == "test"
    assert json.loads(seen["body"]) == payload
    assert seen["signature"] == _signature(body)


def test_zernio_webhook_router_accepts_unmapped_account(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    body = json.dumps({"event": "message.received", "data": {"accountId": "unknown"}}).encode(
        "utf-8"
    )
    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 202


def test_zernio_webhook_router_reconciles_connected_account(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="test",
        name="Test",
        zernio_profile_id="profile_test",
        status="active",
    )
    seen = {}

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return [
                ZernioAccountSummary(
                    id="account_test",
                    platform="whatsapp",
                    profile_id="profile_test",
                    profile_name="Test",
                    display_name="Test WhatsApp",
                    username="+599 9 694 5527",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 694 5527",
                    phone_number_id="phone_test",
                    waba_id="waba_test",
                )
            ]

    async def fake_forward(*, tenant_id, body, signature, content_type):
        seen["tenant_id"] = tenant_id
        seen["body"] = body
        return 200, "OK"

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)
    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant",
        fake_forward,
    )

    payload = {
        "event": "message.received",
        "data": {"accountId": "account_test", "text": "hello"},
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 200
    assert seen["tenant_id"] == "test"
    assert json.loads(seen["body"]) == payload

    connection = channel_connections.get_tenant_channel_connection("test")
    assert connection is not None
    assert connection.status == "connected"
    assert connection.zernio_account_id == "account_test"
    assert connection.phone_number_id == "phone_test"


def test_zernio_webhook_router_rejects_missing_signature(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/internal/api/zernio/webhook-router",
        json={"event": "message.received", "data": {"accountId": "unknown"}},
    )

    assert response.status_code == 401


def test_zernio_webhook_router_rejects_invalid_signature(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.post(
        "/internal/api/zernio/webhook-router",
        json={"event": "message.received", "data": {"accountId": "unknown"}},
        headers={"X-Zernio-Signature": "sha256=bad"},
    )

    assert response.status_code == 401


def test_zernio_webhook_router_rejects_when_secret_missing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    monkeypatch.delenv("ZERNIO_WEBHOOK_SECRET", raising=False)
    body = json.dumps({"event": "message.received", "data": {"accountId": "unknown"}}).encode(
        "utf-8"
    )

    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 503
