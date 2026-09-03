import json
import hashlib
import hmac
import threading

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


def _set_allowlist(root, slug="test", account_id="account_test"):
    path = root / slug / "config" / "client.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["channel_account_allowlist"] = {
        "mode": "strict",
        "zernio_accounts": [account_id],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


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
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        zernio_account_verified=True,
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


def test_zernio_webhook_router_rejects_connected_account_without_allowlist(
    monkeypatch,
    tmp_path,
):
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

    async def fake_forward(*, tenant_id, body, signature, content_type):
        raise AssertionError("Webhook must not forward without strict allowlist")

    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant",
        fake_forward,
    )
    body = json.dumps(
        {"event": "message.received", "data": {"accountId": "account_test"}}
    ).encode("utf-8")

    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 202


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


def test_zernio_webhook_router_rejects_non_strict_fallback_allowlist(
    monkeypatch, tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client_path = tenants_root / "test" / "config" / "client.json"
    data = json.loads(client_path.read_text(encoding="utf-8"))
    data["channel_account_allowlist"] = {
        "mode": "permissive",
        "zernio_accounts": ["account_test"],
    }
    client_path.write_text(json.dumps(data), encoding="utf-8")
    client = _client(monkeypatch, tmp_path)

    async def fake_forward(**_kwargs):
        raise AssertionError("Non-strict allowlist must never route a webhook")

    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant",
        fake_forward,
    )
    body = json.dumps(
        {"event": "message.received", "data": {"accountId": "account_test"}}
    ).encode("utf-8")

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


def test_zernio_webhook_router_rejects_ambiguous_profile_ownership(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root, slug="tenant-one", name="Tenant One")
    _write_tenant(tenants_root, slug="tenant-two", name="Tenant Two")
    client = _client(monkeypatch, tmp_path)

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            assert platform == "whatsapp"
            return [
                ZernioAccountSummary(
                    id="account_shared",
                    platform="whatsapp",
                    profile_id="profile_shared",
                    profile_name="Ambiguous profile",
                    display_name="Shared WhatsApp",
                    username="+599 9 000 0000",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 000 0000",
                    phone_number_id="phone_shared",
                    waba_id="waba_shared",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)
    monkeypatch.setattr(
        "app.routes.connect._tenant_zernio_profile_id",
        lambda _tenant_id: "profile_shared",
    )

    async def fake_forward(**_kwargs):
        raise AssertionError("An ambiguously owned account must never be routed")

    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant",
        fake_forward,
    )
    body = json.dumps(
        {"event": "message.received", "data": {"accountId": "account_shared"}}
    ).encode("utf-8")

    response = client.post(
        "/internal/api/zernio/webhook-router",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Zernio-Signature": _signature(body),
        },
    )

    assert response.status_code == 202
    assert channel_connections.get_tenant_channel_connection("tenant-one") is None
    assert channel_connections.get_tenant_channel_connection("tenant-two") is None


def test_zernio_webhook_router_never_forwards_old_owner_data_to_recreated_slug(
    monkeypatch, tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        zernio_account_verified=True,
    )
    from app.channel_connections import current_tenant_generation_id
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock
    from app.routes import connect as connect_routes

    old_generation = current_tenant_generation_id("test")
    real_resolve = connect_routes._tenant_id_for_zernio_account
    resolved = threading.Event()
    resume = threading.Event()
    forwarded: list[str] = []

    def paused_resolve(account_id):
        owner = real_resolve(account_id)
        resolved.set()
        assert resume.wait(timeout=2)
        return owner

    async def fake_forward(**_kwargs):
        forwarded.append("forwarded")
        return 200, "OK"

    monkeypatch.setattr(
        "app.routes.connect._tenant_id_for_zernio_account", paused_resolve
    )
    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant", fake_forward
    )
    body = json.dumps(
        {"event": "message.received", "data": {"accountId": "account_test"}}
    ).encode("utf-8")
    response_holder: list[object] = []

    def post_webhook():
        response_holder.append(
            client.post(
                "/internal/api/zernio/webhook-router",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Zernio-Signature": _signature(body),
                },
            )
        )

    request_thread = threading.Thread(target=post_webhook)
    request_thread.start()
    assert resolved.wait(timeout=2)

    operation = start_delete_operation(
        slug="test",
        tenant_generation_id=old_generation,
        generation_fingerprint="sha256:" + "5" * 64,
        account_ids=["account_test"],
        profile_ids=["profile_test"],
    )
    update_delete_operation(
        slug="test",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    channel_connections.forget_tenant("test")
    with tenant_creation_lock("test"):
        bind_tenant_generation_for_creation(
            slug="test",
            generation_id="replacement-generation",
            status="active",
        )
    _set_allowlist(tenants_root, account_id="replacement_account")
    resume.set()
    request_thread.join(timeout=3)

    assert not request_thread.is_alive()
    assert len(response_holder) == 1
    assert response_holder[0].status_code == 202
    assert forwarded == []


def test_zernio_webhook_resolver_rejects_recreated_row_for_same_account(
    monkeypatch, tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    original = channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        zernio_account_verified=True,
    )
    from app.channel_connections import current_tenant_generation_id
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock

    old_generation = current_tenant_generation_id("test")
    real_lookup = channel_connections.get_tenant_channel_connection_by_account_id
    first_lookup_done = threading.Event()
    resume_lookup = threading.Event()
    lookup_count = 0
    forwarded: list[str] = []

    def paused_first_lookup(account_id):
        nonlocal lookup_count
        connection = real_lookup(account_id)
        lookup_count += 1
        if lookup_count == 1:
            first_lookup_done.set()
            assert resume_lookup.wait(timeout=2)
        return connection

    async def fake_forward(**_kwargs):
        forwarded.append("forwarded")
        return 200, "OK"

    monkeypatch.setattr(
        "app.channel_connections.get_tenant_channel_connection_by_account_id",
        paused_first_lookup,
    )
    monkeypatch.setattr(
        "app.routes.connect._forward_zernio_webhook_to_tenant", fake_forward
    )
    body = json.dumps(
        {"event": "message.received", "data": {"accountId": "account_test"}}
    ).encode("utf-8")
    response_holder: list[object] = []

    def post_webhook():
        response_holder.append(
            client.post(
                "/internal/api/zernio/webhook-router",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Zernio-Signature": _signature(body),
                },
            )
        )

    request_thread = threading.Thread(target=post_webhook)
    request_thread.start()
    assert first_lookup_done.wait(timeout=2)

    operation = start_delete_operation(
        slug="test",
        tenant_generation_id=old_generation,
        generation_fingerprint="sha256:" + "4" * 64,
        account_ids=["account_test"],
        profile_ids=["profile_test"],
    )
    update_delete_operation(
        slug="test",
        operation_id=operation["operation_id"],
        expected_phases={"preparing"},
        phase="deleted",
    )
    channel_connections.forget_tenant("test")
    with tenant_creation_lock("test"):
        bind_tenant_generation_for_creation(
            slug="test",
            generation_id="replacement-generation",
            status="active",
        )
    replacement = channel_connections.upsert_tenant_channel_connection(
        tenant_id="test",
        status="connected",
        zernio_profile_id="profile_test",
        zernio_account_id="account_test",
        zernio_account_verified=True,
    )
    assert replacement.id != original.id
    resume_lookup.set()
    request_thread.join(timeout=3)

    assert not request_thread.is_alive()
    assert len(response_holder) == 1
    assert response_holder[0].status_code == 202
    assert forwarded == []


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
