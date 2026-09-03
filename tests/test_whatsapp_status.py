import json
import sqlite3

from fastapi.testclient import TestClient

from app import channel_connections
from app.main import app
from app.provisioning import AutoProvisionResult
from app.zernio import ZernioAccountSummary


def _write_tenant(root, slug="lawyer", name="Lawyer"):
    config_dir = root / slug / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({"slug": slug, "name": name, "status": "active"}),
        encoding="utf-8",
    )


def _set_allowlist(root, slug="lawyer", account_id="account_1"):
    path = root / slug / "config" / "client.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["channel_account_allowlist"] = {
        "mode": "strict",
        "zernio_accounts": [account_id],
    }
    path.write_text(json.dumps(data), encoding="utf-8")


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


def _status(client: TestClient):
    return client.get("/internal/api/tenants/lawyer/channels/whatsapp/status")


def test_whatsapp_status_requires_admin(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)

    response = _status(client)

    assert response.status_code == 401
    assert response.json()["detail"] == "Admin authentication required."


def test_whatsapp_status_returns_404_for_unknown_tenant(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _status(client)

    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant not found."


def test_whatsapp_status_returns_not_connected(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload == {
        "success": True,
        "tenantId": "lawyer",
        "channel": "whatsapp",
        "provider": "zernio",
        "status": "not_connected",
        "label": "Not connected",
        "connected": False,
        "providerConnected": False,
        "displayPhoneNumber": None,
        "phoneNumberId": None,
        "providerAccountId": None,
        "zernioProfileId": None,
        "connectedAt": None,
        "lastUpdatedAt": None,
        "lastError": None,
        "allowlist": {
            "ok": False,
            "label": "Missing strict allowlist",
            "summary": "No channel_account_allowlist found in client.json.",
            "accounts": [],
        },
        "repairAvailable": False,
        "actionLabel": "Generate new WhatsApp connection link",
        "summary": "Generate a secure link when the client is ready to authorize WhatsApp.",
    }


def test_whatsapp_status_returns_pending(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="pending",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connection_pending"
    assert payload["connected"] is False
    assert payload["providerAccountId"] == "account_1"
    assert payload["zernioProfileId"] == "profile_lawyer"
    assert payload["lastUpdatedAt"]
    assert payload["lastError"] is None


def test_whatsapp_status_returns_connected(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        phone_number_id="phone_1",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_1",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected_healthy"
    assert payload["connected"] is True
    assert payload["providerConnected"] is True
    assert payload["displayPhoneNumber"] == "+599 9 694 5527"
    assert payload["phoneNumberId"] == "phone_1"
    assert payload["providerAccountId"] == "account_1"
    assert payload["zernioProfileId"] == "profile_lawyer"
    assert payload["connectedAt"]
    assert payload["lastUpdatedAt"]


def test_whatsapp_status_revalidates_exact_legacy_connected_account(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    historical_link = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="expired_historical_token",
        status="link_generated",
        expires_in_minutes=-1,
    ).request
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=False,
        phone_number_id="legacy_phone",
        display_phone_number="+599 9 000 0000",
        waba_id="legacy_waba",
    )
    calls = []

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            calls.append(platform)
            return [
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Lawyer WhatsApp",
                    username="+599 9 694 5527",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 694 5527",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected_healthy"
    assert payload["providerAccountId"] == "account_1"
    assert payload["phoneNumberId"] == "phone_1"
    assert calls == ["whatsapp"]
    repaired = channel_connections.get_tenant_channel_connection("lawyer")
    assert repaired is not None
    assert repaired.zernio_account_verified is True
    assert repaired.zernio_profile_id == "profile_lawyer"
    assert repaired.zernio_account_id == "account_1"
    assert repaired.last_request_id is None
    unchanged_historical_link = channel_connections.get_connection_request(
        historical_link.id
    )
    assert unchanged_historical_link is not None
    assert unchanged_historical_link.status == "link_generated"


def test_whatsapp_status_never_substitutes_different_account_for_legacy_owner(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="legacy_account",
        zernio_account_verified=False,
    )

    class DifferentAccountService:
        def list_accounts(self, *, platform=None):
            return [
                ZernioAccountSummary(
                    id="different_account",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Different WhatsApp",
                    username="+599 9 000 0001",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 000 0001",
                    phone_number_id="different_phone",
                    waba_id="different_waba",
                )
            ]

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        DifferentAccountService,
    )

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "needs_reconnect_unverified_account"
    unchanged = channel_connections.get_tenant_channel_connection("lawyer")
    assert unchanged is not None
    assert unchanged.zernio_account_verified is False
    assert unchanged.zernio_account_id == "legacy_account"


def test_new_link_blocks_recovery_candidate_from_older_pending_request(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    old = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="old_pending_token",
        status="link_generated",
    ).request
    channel_connections.update_connection_request(
        old.id,
        status="pending_number",
        zernio_account_id="old_account",
        zernio_account_verified=False,
        error_summary=None,
    )
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="pending",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="old_account",
        zernio_account_verified=False,
        last_request_id=old.id,
    )
    replacement = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="replacement_token",
        status="link_generated",
    ).request

    class ProviderMustNotBeQueried:
        def list_accounts(self, *, platform=None):
            raise AssertionError(
                "Status must not recover an account from a superseded request"
            )

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        ProviderMustNotBeQueried,
    )

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "connection_pending"
    assert channel_connections.get_latest_connection_request_for_tenant(
        "lawyer"
    ).id == replacement.id
    unchanged = channel_connections.get_tenant_channel_connection("lawyer")
    assert unchanged is not None
    assert unchanged.status == "pending"
    assert unchanged.zernio_account_id == "old_account"
    assert unchanged.zernio_account_verified is False


def test_whatsapp_status_requires_allowlist_for_healthy_connected(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        phone_number_id="phone_1",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_1",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_repair_missing_allowlist"
    assert payload["connected"] is False
    assert payload["providerConnected"] is True
    assert payload["repairAvailable"] is True
    assert payload["allowlist"]["ok"] is False


def test_repair_whatsapp_allowlist_from_verified_connection(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        phone_number_id="phone_1",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_1",
    )

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/repair-allowlist"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected_healthy"
    assert payload["connected"] is True
    client_json = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text(
            encoding="utf-8"
        )
    )
    assert client_json["channel_account_allowlist"]["mode"] == "strict"
    assert client_json["channel_account_allowlist"]["zernio_accounts"] == ["account_1"]


def test_allowlist_update_never_overwrites_malformed_tenant_config(
    monkeypatch, tmp_path,
):
    from app.tenants import update_tenant_channel_account_allowlist

    client_path = tmp_path / "tenants" / "lawyer" / "config" / "client.json"
    client_path.parent.mkdir(parents=True)
    client_path.write_text("{malformed", encoding="utf-8")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))

    assert update_tenant_channel_account_allowlist(
        "lawyer", zernio_account_id="account_1", note="verified"
    ) is False
    assert client_path.read_text(encoding="utf-8") == "{malformed"


def test_allowlist_update_accepts_already_strict_readonly_mapping(
    monkeypatch, tmp_path,
):
    from app.tenants import update_tenant_channel_account_allowlist

    client_path = tmp_path / "tenants" / "lawyer" / "config" / "client.json"
    client_path.parent.mkdir(parents=True)
    client_path.write_text(
        json.dumps({
            "slug": "lawyer",
            "password": "do-not-rewrite",
            "channel_account_allowlist": {
                "mode": "strict",
                "zernio_accounts": ["account_1"],
            },
        }),
        encoding="utf-8",
    )
    client_path.chmod(0o400)
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))

    assert update_tenant_channel_account_allowlist(
        "lawyer", zernio_account_id="account_1", note="verified"
    ) is True
    assert json.loads(client_path.read_text())["password"] == "do-not-rewrite"


def test_allowlist_update_replaces_inherited_accounts_with_exact_verified_owner(
    monkeypatch, tmp_path,
):
    from app.tenants import update_tenant_channel_account_allowlist

    client_path = tmp_path / "tenants" / "lawyer" / "config" / "client.json"
    client_path.parent.mkdir(parents=True)
    client_path.write_text(
        json.dumps({
            "slug": "lawyer",
            "password": "keep-this-secret",
            "channel_account_allowlist": {
                "mode": "permissive",
                "zernio_accounts": ["attacker_account", "account_1"],
                "notes": "client-controlled legacy mapping",
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))

    assert update_tenant_channel_account_allowlist(
        "lawyer",
        zernio_account_id="account_1",
        note="provider-verified owner",
    ) is True
    repaired = json.loads(client_path.read_text(encoding="utf-8"))
    assert repaired["password"] == "keep-this-secret"
    assert repaired["channel_account_allowlist"] == {
        "mode": "strict",
        "zernio_accounts": ["account_1"],
        "notes": "provider-verified owner",
    }


def test_repair_whatsapp_allowlist_queues_host_action_when_client_root_readonly(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        phone_number_id="phone_1",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_1",
    )
    seen = {}

    def fake_write(*args, **kwargs):
        return False

    def fake_queue(**kwargs):
        seen.update(kwargs)
        _set_allowlist(tenants_root)
        return AutoProvisionResult(status="succeeded", message="allowlist repaired")

    monkeypatch.setattr("app.routes.connect.update_tenant_channel_account_allowlist", fake_write)
    monkeypatch.setattr("app.routes.connect.queue_tenant_host_action", fake_queue)

    response = client.post(
        "/internal/api/tenants/lawyer/channels/whatsapp/repair-allowlist"
    )

    assert response.status_code == 200
    assert response.json()["status"] == "connected_healthy"
    assert seen["action"] == "repair_whatsapp_allowlist"
    assert seen["slug"] == "lawyer"
    assert seen["zernio_account_id"] == "account_1"


def test_whatsapp_status_returns_failed(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        last_error="Client denied authorization.",
    )

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "needs_reconnect_authorization_failed"
    assert payload["connected"] is False
    assert payload["lastError"] == "Client denied authorization."


def test_whatsapp_status_retries_exact_verified_allowlist_failure(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    _set_allowlist(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    historical_link = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="historical_allowlist_failure",
        status="link_generated",
    ).request
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        phone_number_id="phone_1",
        display_phone_number="+1 223 276 0075",
        waba_id="waba_1",
        last_error=(
            "Provider authorization succeeded, but strict tenant routing "
            "could not be secured."
        ),
    )
    with sqlite3.connect(tmp_path / "nr3.db") as conn:
        conn.execute(
            "UPDATE connection_requests SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00+00:00", historical_link.id),
        )
        conn.execute(
            "UPDATE tenant_channel_connections SET updated_at = ? WHERE tenant_id = ?",
            ("2026-01-01T00:00:01+00:00", "lawyer"),
        )
    calls = []

    class ExactAccountService:
        def list_accounts(self, *, platform=None):
            calls.append(platform)
            return [
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Mermaid WhatsApp",
                    username="+1 223 276 0075",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+1 223 276 0075",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", ExactAccountService)

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "connected_healthy"
    assert calls == ["whatsapp"]
    repaired = channel_connections.get_tenant_channel_connection("lawyer")
    assert repaired is not None
    assert repaired.status == "connected"
    assert repaired.zernio_account_verified is True
    assert repaired.zernio_account_id == "account_1"
    assert repaired.last_request_id is None
    unchanged_link = channel_connections.get_connection_request(historical_link.id)
    assert unchanged_link is not None
    assert unchanged_link.status == "link_generated"
    assert unchanged_link.zernio_account_verified is False


def test_whatsapp_status_does_not_retry_unrelated_verified_failure(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        last_error="Provider account was disabled.",
    )

    class ProviderMustNotBeQueried:
        def list_accounts(self, *, platform=None):
            raise AssertionError("An unrelated failure must not be self-healed")

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        ProviderMustNotBeQueried,
    )

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "needs_reconnect_authorization_failed"
    unchanged = channel_connections.get_tenant_channel_connection("lawyer")
    assert unchanged is not None
    assert unchanged.status == "failed"
    assert unchanged.last_error == "Provider account was disabled."


def test_new_link_blocks_verified_allowlist_failure_recovery(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        last_error=(
            "Provider authorization succeeded, but strict tenant routing "
            "could not be secured."
        ),
    )
    replacement = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="replacement_after_allowlist_failure",
        status="link_generated",
    ).request
    with sqlite3.connect(tmp_path / "nr3.db") as conn:
        conn.execute(
            "UPDATE tenant_channel_connections SET updated_at = ? WHERE tenant_id = ?",
            ("2026-01-01T00:00:00+00:00", "lawyer"),
        )
        conn.execute(
            "UPDATE connection_requests SET created_at = ? WHERE id = ?",
            ("2026-01-01T00:00:01+00:00", replacement.id),
        )

    class ProviderMustNotBeQueried:
        def list_accounts(self, *, platform=None):
            raise AssertionError("A newer authorization request must win")

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        ProviderMustNotBeQueried,
    )

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "connection_pending"
    assert channel_connections.get_latest_connection_request_for_tenant(
        "lawyer"
    ).id == replacement.id
    unchanged = channel_connections.get_tenant_channel_connection("lawyer")
    assert unchanged is not None
    assert unchanged.status == "failed"
    assert unchanged.zernio_account_id == "account_1"


def test_provider_io_race_cannot_cross_new_authorization_request(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        last_error=(
            "Provider authorization succeeded, but strict tenant routing "
            "could not be secured."
        ),
    )
    replacement_ids = []

    class RacingAccountService:
        def list_accounts(self, *, platform=None):
            replacement = channel_connections.create_connection_request(
                tenant_id="lawyer",
                zernio_profile_id="profile_lawyer",
                state_token="replacement_created_during_provider_io",
                status="link_generated",
            ).request
            replacement_ids.append(replacement.id)
            return [
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Old WhatsApp",
                    username="+1 223 276 0075",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+1 223 276 0075",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", RacingAccountService)

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "connection_pending"
    assert len(replacement_ids) == 1
    assert channel_connections.get_latest_connection_request_for_tenant(
        "lawyer"
    ).id == replacement_ids[0]
    unchanged = channel_connections.get_tenant_channel_connection("lawyer")
    assert unchanged is not None
    assert unchanged.status == "failed"
    assert unchanged.zernio_account_id == "account_1"
    assert unchanged.last_error == (
        "Provider authorization succeeded, but strict tenant routing "
        "could not be secured."
    )
    client_json = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in client_json


def test_provider_io_race_cannot_cross_same_request_state_change(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    authorization = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="claimed_during_provider_io",
        status="link_generated",
    ).request

    class RacingAccountService:
        def list_accounts(self, *, platform=None):
            assert channel_connections.claim_connection_request_callback(
                authorization.id
            ) is True
            return [
                ZernioAccountSummary(
                    id="profile_first_account",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="First profile account",
                    username="+1 555 000 0000",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+1 555 000 0000",
                    phone_number_id="first_phone",
                    waba_id="first_waba",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", RacingAccountService)

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["status"] == "connection_pending"
    assert channel_connections.get_tenant_channel_connection("lawyer") is None
    claimed = channel_connections.get_connection_request(authorization.id)
    assert claimed is not None
    assert claimed.status == "callback_received"
    assert claimed.zernio_account_verified is False
    client_json = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in client_json


def test_queued_allowlist_retry_keeps_exact_verified_account_fence(
    monkeypatch,
    tmp_path,
):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        zernio_profile_id="profile_lawyer",
        zernio_account_id="account_1",
        zernio_account_verified=True,
        last_error=(
            "Provider authorization succeeded, but strict tenant routing "
            "could not be secured."
        ),
    )
    provider_calls = []
    queue_calls = []

    class ReorderedAccountService:
        def list_accounts(self, *, platform=None):
            provider_calls.append(platform)
            return [
                ZernioAccountSummary(
                    id="different_account",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Different WhatsApp",
                    username="+1 555 000 0000",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+1 555 000 0000",
                    phone_number_id="different_phone",
                    waba_id="different_waba",
                ),
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Expected WhatsApp",
                    username="+1 223 276 0075",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+1 223 276 0075",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                ),
            ]

    def read_only_client_root(*args, **kwargs):
        return False

    def queued_then_repaired(**kwargs):
        queue_calls.append(kwargs)
        if len(queue_calls) == 1:
            return AutoProvisionResult(status="queued", message="repair queued")
        _set_allowlist(tenants_root, account_id=kwargs["zernio_account_id"])
        return AutoProvisionResult(status="succeeded", message="allowlist repaired")

    monkeypatch.setattr("app.routes.connect.ZernioService", ReorderedAccountService)
    monkeypatch.setattr(
        "app.routes.connect.update_tenant_channel_account_allowlist",
        read_only_client_root,
    )
    monkeypatch.setattr(
        "app.routes.connect.queue_tenant_host_action",
        queued_then_repaired,
    )

    queued = _status(client)
    pending = channel_connections.get_tenant_channel_connection("lawyer")
    repaired = _status(client)

    assert queued.status_code == 200
    assert queued.json()["status"] == "connection_pending"
    assert pending is not None
    assert pending.status == "pending"
    assert pending.zernio_account_id == "account_1"
    assert pending.last_error == "Strict tenant allowlist repair is queued."
    assert repaired.status_code == 200
    assert repaired.json()["status"] == "connected_healthy"
    assert provider_calls == ["whatsapp", "whatsapp"]
    assert [call["zernio_account_id"] for call in queue_calls] == [
        "account_1",
        "account_1",
    ]
    connected = channel_connections.get_tenant_channel_connection("lawyer")
    assert connected is not None
    assert connected.status == "connected"
    assert connected.zernio_account_id == "account_1"
    assert connected.phone_number_id == "phone_1"
    client_json = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert client_json["channel_account_allowlist"]["zernio_accounts"] == [
        "account_1"
    ]


def test_whatsapp_status_reconciles_connected_zernio_account(monkeypatch, tmp_path):
    tenants_root = tmp_path / "tenants"
    _write_tenant(tenants_root)
    client = _client(monkeypatch, tmp_path)
    _login(client)
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_missed_callback",
        status="link_generated",
    ).request

    class FakeZernioService:
        def list_accounts(self, *, platform=None):
            return [
                ZernioAccountSummary(
                    id="account_1",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Lawyer WhatsApp",
                    username="+599 9 694 5527",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 694 5527",
                    phone_number_id="phone_1",
                    waba_id="waba_1",
                )
            ]

    monkeypatch.setattr("app.routes.connect.ZernioService", FakeZernioService)

    response = _status(client)

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "connected_healthy"
    assert payload["connected"] is True
    assert payload["providerAccountId"] == "account_1"
    assert payload["displayPhoneNumber"] == "+599 9 694 5527"
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    client_json = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text(
            encoding="utf-8"
        )
    )
    assert client_json["channel_account_allowlist"] == {
        "mode": "strict",
        "zernio_accounts": ["account_1"],
        "notes": "Nr3 WhatsApp connection: strict Zernio account allowlist for +599 9 694 5527.",
    }


def test_status_reconcile_cannot_attach_provider_data_after_generation_rotates(
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
        zernio_profile_id="profile_lawyer",
        name="Lawyer",
    )
    request_row = channel_connections.create_connection_request(
        tenant_id="lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="state_old_generation",
        status="link_generated",
    ).request
    old_generation = request_row.tenant_generation_id

    class RotatingZernioService:
        def list_accounts(self, *, platform=None):
            operation = start_delete_operation(
                slug="lawyer",
                tenant_generation_id=old_generation,
                generation_fingerprint="sha256:" + "d" * 64,
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
            return [
                ZernioAccountSummary(
                    id="stale_account",
                    platform="whatsapp",
                    profile_id="profile_lawyer",
                    profile_name="Lawyer",
                    display_name="Old Lawyer WhatsApp",
                    username="+599 9 000 0000",
                    enabled=True,
                    is_active=True,
                    platform_status="active",
                    display_phone_number="+599 9 000 0000",
                    phone_number_id="stale_phone",
                    waba_id="stale_waba",
                )
            ]

    monkeypatch.setattr(
        "app.routes.connect.ZernioService", RotatingZernioService
    )

    response = _status(client)

    assert response.status_code == 200
    assert response.json()["connected"] is False
    assert channel_connections.get_connection_request(request_row.id).status == (
        "link_generated"
    )
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is None or connection.zernio_account_id != "stale_account"
    client_data = json.loads(
        (tenants_root / "lawyer" / "config" / "client.json").read_text()
    )
    assert "channel_account_allowlist" not in client_data
