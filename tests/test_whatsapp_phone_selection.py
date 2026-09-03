import json

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.zernio import ZernioAccountSummary


def _write_tenant(root, slug="lawyer", name="Lawyer"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    return TestClient(app)


def _login(client: TestClient):
    response = client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def _account(
    account_id,
    phone_id,
    display_phone,
    *,
    profile_id="profile_lawyer",
    enabled=True,
    is_active=True,
    platform_status="active",
):
    return ZernioAccountSummary(
        id=account_id,
        platform="whatsapp",
        profile_id=profile_id,
        profile_name="Lawyer",
        display_name=display_phone,
        username=display_phone,
        enabled=enabled,
        is_active=is_active,
        platform_status=platform_status,
        display_phone_number=display_phone,
        phone_number_id=phone_id,
        waba_id=f"waba_{phone_id}",
    )


def _fake_zernio(monkeypatch, accounts):
    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            assert platform == "whatsapp"
            return accounts

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)


def _numbers(client: TestClient):
    return client.get("/internal/api/tenants/lawyer/channels/whatsapp/phone-numbers")


def _select(client: TestClient, payload):
    return client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/phone-numbers/select",
        json=payload,
    )


def test_whatsapp_phone_numbers_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = _numbers(client)

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin authentication required."


def test_whatsapp_phone_numbers_returns_single_phone(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(monkeypatch, [_account("account_1", "phone_1", "+599 1")])
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    response = _numbers(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "single_phone"
    assert payload["zernioProfileId"] == "profile_lawyer"
    assert payload["phoneNumbers"] == [
        {
            "accountId": "account_1",
            "profileId": "profile_lawyer",
            "displayName": "+599 1",
            "username": "+599 1",
            "displayPhoneNumber": "+599 1",
            "phoneNumberId": "phone_1",
            "wabaId": "waba_phone_1",
            "enabled": True,
            "isActive": True,
            "platformStatus": "active",
        }
    ]


def test_whatsapp_phone_numbers_returns_multiple_phones(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(
        monkeypatch,
        [
            _account("account_1", "phone_1", "+599 1"),
            _account("account_2", "phone_2", "+599 2"),
            _account("other", "phone_other", "+599 3", profile_id="other_profile"),
        ],
    )
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )

    response = _numbers(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "multiple_phone"
    assert [phone["phoneNumberId"] for phone in payload["phoneNumbers"]] == [
        "phone_1",
        "phone_2",
    ]


def test_whatsapp_phone_numbers_excludes_inactive_accounts(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(
        monkeypatch,
        [
            _account("active", "phone_active", "+599 1"),
            _account(
                "disabled",
                "phone_disabled",
                "+599 2",
                enabled=False,
            ),
            _account(
                "inactive",
                "phone_inactive",
                "+599 3",
                is_active=False,
            ),
        ],
    )
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )

    response = _numbers(client)

    assert response.status_code == 200
    assert [item["accountId"] for item in response.json()["phoneNumbers"]] == [
        "active"
    ]
    rejected = _select(
        client,
        {"phoneNumberId": "phone_disabled", "accountId": "disabled"},
    )
    assert rejected.status_code == 400


def test_whatsapp_phone_selection_connects_selected_phone(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(
        monkeypatch,
        [
            _account("account_1", "phone_1", "+599 1"),
            _account("account_2", "phone_2", "+599 2"),
        ],
    )
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_pending",
        status="pending_number",
    ).request
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="pending",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        last_request_id=created.id,
    )

    response = _select(
        client,
        {"phoneNumberId": "phone_2", "accountId": "account_2"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected"
    assert payload["connected"] is True
    assert payload["phoneNumberId"] == "phone_2"
    assert payload["displayPhoneNumber"] == "+599 2"
    assert payload["providerAccountId"] == "account_2"

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    assert stored.zernio_account_id == "account_2"
    assert stored.selected_phone_number_id == "phone_2"


def test_whatsapp_phone_selection_blocks_account_owned_by_another_tenant(
    monkeypatch, tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(monkeypatch, [_account("account_1", "phone_1", "+599 1")])
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="other-tenant",
        status="connected",
        zernio_profile_id="profile_other",
        zernio_account_id="account_1",
        zernio_account_verified=True,
    )

    response = _select(
        client,
        {"phoneNumberId": "phone_1", "accountId": "account_1"},
    )

    assert response.status_code == 409
    assert "already connected to another tenant" in response.json()["detail"]
    assert channel_connections.get_tenant_channel_connection("lawyer") is None
    client_data = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in client_data


def test_phone_selection_cannot_attach_after_tenant_generation_rotates(
    monkeypatch, tmp_path,
):
    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        start_delete_operation,
        update_delete_operation,
    )
    from app.provisioning import tenant_creation_lock

    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    from app.channel_connections import current_tenant_generation_id

    old_generation = current_tenant_generation_id("lawyer")

    class RotatingZernioService:
        def list_accounts(self, *, platform=None):
            operation = start_delete_operation(
                slug="lawyer",
                tenant_generation_id=old_generation,
                generation_fingerprint="sha256:" + "e" * 64,
                account_ids=[],
                profile_ids=["profile_lawyer"],
            )
            update_delete_operation(
                slug="lawyer",
                operation_id=operation["operation_id"],
                expected_phases={"preparing"},
                phase="deleted",
            )
            with tenant_creation_lock("lawyer"):
                bind_tenant_generation_for_creation(
                    slug="lawyer",
                    generation_id="replacement-generation",
                    status="active",
                )
            return [_account("stale_account", "stale_phone", "+599 0")]

    monkeypatch.setattr(
        "app.routes.connect.ZernioService", RotatingZernioService
    )

    response = _select(
        client,
        {"phoneNumberId": "stale_phone", "accountId": "stale_account"},
    )

    assert response.status_code == 409
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is None or connection.zernio_account_id != "stale_account"
    client_data = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in client_data


def test_whatsapp_phone_selection_rejects_invalid_selection(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _fake_zernio(monkeypatch, [_account("account_1", "phone_1", "+599 1")])
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )

    response = _select(client, {"phoneNumberId": "missing_phone"})

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid WhatsApp phone selection."
