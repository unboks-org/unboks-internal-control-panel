import json

from fastapi.testclient import TestClient

from app import audit_log, channel_connections
from app.main import app
from app.provisioning import AutoProvisionResult
from app.zernio import ZernioAccountSummary


def _client(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    tenants_root = tmp_path / "tenants"
    config_dir = tenants_root / "lawyer" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "client.json").write_text(
        json.dumps({
            "slug": "lawyer",
            "name": "Lawyer",
            "channel_account_allowlist": {
                "mode": "strict",
                "zernio_accounts": [],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenants_root))

    class VerifiedZernioService:
        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
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

    monkeypatch.setattr(
        "app.routes.connect.ZernioService", VerifiedZernioService
    )
    return TestClient(app)


def _connection_request(state_token="zernio_state_123"):
    return channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token=state_token,
        status="link_generated",
    ).request


def test_whatsapp_callback_marks_connection_connected(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request()

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "zernio_state_123",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "forged_phone",
            "displayPhoneNumber": "+599 9 000 0000",
            "wabaId": "forged_waba",
            "code": "do-not-store-this",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=success&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    assert stored.zernio_account_id == "account_1"
    assert stored.selected_phone_number_id == "phone_1"
    assert stored.display_phone_number == "+599 9 694 5527"
    assert stored.callback_payload_json is not None
    callback_payload = json.loads(stored.callback_payload_json)
    assert "state" not in callback_payload
    assert "zernio_state_123" not in stored.callback_payload_json
    assert "code" not in callback_payload

    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "connected"
    assert connection.zernio_profile_id == "profile_lawyer"
    assert connection.zernio_account_id == "account_1"
    assert connection.zernio_account_verified is True
    assert connection.phone_number_id == "phone_1"
    assert connection.display_phone_number == "+599 9 694 5527"
    assert connection.waba_id == "waba_1"


def test_whatsapp_callback_replay_is_read_only(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("one_time_state")

    first = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "one_time_state",
            "status": "success",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )
    replay = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "one_time_state",
            "status": "failed",
            "accountId": "different_account",
            "error": "forged downgrade",
        },
        follow_redirects=False,
    )

    assert first.headers["location"].endswith("status=success&tenantId=lawyer")
    assert replay.headers["location"].endswith("status=success&tenantId=lawyer")
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    assert stored.zernio_account_id == "account_1"
    assert any(
        event.action == "whatsapp.callback_replayed"
        and event.result == "ignored"
        for event in audit_log.list_events()
    )


def test_inflight_old_callback_cannot_beat_new_authorization_link(
    monkeypatch,
    tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    old_request = _connection_request("old_inflight_token")
    replacement = {}

    class SupersedingZernioService:
        def get_account(self, account_id):
            # Model the exact race: the old callback is already claimed and
            # waiting on Zernio when the operator generates a replacement.
            replacement["request"] = channel_connections.create_connection_request(
                tenant_id="lawyer",
                auth_url="https://facebook.com/connect/replacement",
                zernio_profile_id="profile_lawyer",
                state_token="replacement_token",
                status="link_generated",
            ).request
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_lawyer",
                profile_name="Lawyer",
                display_name="Old WhatsApp",
                username="+599 9 000 0000",
                enabled=True,
                is_active=True,
                platform_status="active",
                display_phone_number="+599 9 000 0000",
                phone_number_id="old_phone",
                waba_id="old_waba",
            )

    monkeypatch.setattr(
        "app.routes.connect.ZernioService",
        SupersedingZernioService,
    )

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "old_inflight_token",
            "connected": "whatsapp",
            "accountId": "old_account",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )
    cancelled = channel_connections.get_connection_request(old_request.id)
    assert cancelled is not None and cancelled.status == "cancelled"
    assert channel_connections.get_latest_connection_request_for_tenant(
        "lawyer"
    ).id == replacement["request"].id
    assert channel_connections.get_tenant_channel_connection("lawyer") is None
    client_data = json.loads(
        (tmp_path / "tenants" / "lawyer" / "config" / "client.json").read_text()
    )
    assert client_data["channel_account_allowlist"]["zernio_accounts"] == []


def test_whatsapp_callback_claim_is_atomic(monkeypatch, tmp_path):
    _client(monkeypatch, tmp_path)
    created = _connection_request("claim_once_state")

    assert channel_connections.claim_connection_request_callback(created.id) is True
    assert channel_connections.claim_connection_request_callback(created.id) is False
    assert channel_connections.get_connection_request(created.id).status == "callback_received"


def test_whatsapp_callback_rejects_expired_state(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://facebook.com/connect/lawyer",
        zernio_profile_id="profile_lawyer",
        state_token="expired_state",
        status="link_generated",
        expires_in_minutes=-1,
    ).request

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "expired_state",
            "status": "success",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "expired"
    config = json.loads(
        (tmp_path / "tenants" / "lawyer" / "config" / "client.json").read_text()
    )
    assert config["channel_account_allowlist"]["zernio_accounts"] == []


def test_whatsapp_callback_accepts_zernio_connect_token_and_username(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("connect_token_123")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "connect_token": "connect_token_123",
            "connected": "whatsapp",
            "accountId": "account_1",
            "profileId": "profile_lawyer",
            "username": "+599 9 694 5527",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=success&tenantId=lawyer"
    )
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"
    assert stored.zernio_account_id == "account_1"
    assert stored.display_phone_number == "+599 9 694 5527"

    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "connected"
    assert connection.zernio_account_id == "account_1"
    assert connection.display_phone_number == "+599 9 694 5527"


def test_whatsapp_callback_prefers_nr3_token_over_provider_state(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("nr3_owned_token")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "nr3_token": "nr3_owned_token",
            "state": "provider_internal_state",
            "connected": "whatsapp",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=success&tenantId=lawyer"
    )
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "connected"


def test_whatsapp_callback_queues_allowlist_repair_for_readonly_tenant_mount(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("readonly_state")
    seen = {}
    monkeypatch.setattr(
        "app.routes.connect.update_tenant_channel_account_allowlist",
        lambda *_args, **_kwargs: False,
    )

    def fake_queue(**kwargs):
        seen.update(kwargs)
        return AutoProvisionResult(
            status="queued",
            message="repair queued",
            job_id="repair-job",
        )

    monkeypatch.setattr("app.routes.connect.queue_tenant_host_action", fake_queue)

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "readonly_state",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "phone_1",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith(
        "status=pending-activation&tenantId=lawyer"
    )
    assert seen["slug"] == "lawyer"
    assert seen["action"] == "repair_whatsapp_allowlist"
    assert seen["zernio_account_id"] == "account_1"
    assert channel_connections.get_connection_request(created.id).status == "callback_received"
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "pending"
    event = audit_log.list_events()[0]
    assert event.action == "whatsapp.callback_activation_pending"
    assert event.result == "pending"


def test_whatsapp_callback_fails_closed_when_allowlist_repair_cannot_queue(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("repair_failure_state")
    monkeypatch.setattr(
        "app.routes.connect.update_tenant_channel_account_allowlist",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.routes.connect.queue_tenant_host_action",
        lambda **_kwargs: AutoProvisionResult(
            status="failed",
            message="queue unavailable",
        ),
    )

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "repair_failure_state",
            "status": "success",
            "accountId": "account_1",
            "phoneNumberId": "phone_1",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.zernio_account_id == "account_1"
    assert stored.zernio_account_verified is True
    assert "strict tenant allowlist" in str(stored.error_summary)
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "failed"


def test_whatsapp_callback_rejects_account_from_different_profile(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("wrong_profile_state")

    class WrongProfileService:
        def get_account(self, account_id):
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_other_tenant",
                profile_name="Other tenant",
                display_name="Other WhatsApp",
                username="+599 9 000 0000",
                enabled=True,
                is_active=True,
                platform_status="active",
                display_phone_number="+599 9 000 0000",
                phone_number_id="phone_other",
                waba_id="waba_other",
            )

    monkeypatch.setattr("app.routes.connect.ZernioService", WrongProfileService)
    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "wrong_profile_state",
            "status": "success",
            "accountId": "account_other",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "failed"
    # Retain the callback's account only as an unverified recovery candidate;
    # status refresh may promote it solely after an exact account/profile
    # provider lookup succeeds.
    assert stored.zernio_account_id == "account_other"
    assert stored.zernio_account_verified is False
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.zernio_account_id == "account_other"
    assert connection.zernio_account_verified is False
    assert "account_other" not in channel_connections.list_tenant_zernio_ids(
        "lawyer"
    )["account_ids"]
    config = json.loads(
        (tmp_path / "tenants" / "lawyer" / "config" / "client.json").read_text()
    )
    assert config["channel_account_allowlist"]["zernio_accounts"] == []


def test_whatsapp_callback_rejects_unverifiable_account(monkeypatch, tmp_path):
    from app.zernio import ZernioNotConfigured

    client = _client(monkeypatch, tmp_path)
    created = _connection_request("unverified_state")

    class UnavailableZernioService:
        def get_account(self, _account_id):
            raise ZernioNotConfigured("missing key")

    monkeypatch.setattr(
        "app.routes.connect.ZernioService", UnavailableZernioService
    )
    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "unverified_state",
            "status": "success",
            "accountId": "unverified_account",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    assert channel_connections.get_connection_request(created.id).status == "failed"


def test_whatsapp_callback_blocks_account_already_verified_for_another_tenant(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="other-tenant",
        status="connected",
        zernio_profile_id="profile_other",
        zernio_account_id="account_1",
        zernio_account_verified=True,
    )
    created = _connection_request("cross_tenant_state")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "cross_tenant_state",
            "status": "success",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )

    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.zernio_account_verified is False
    assert channel_connections.get_tenant_channel_connection(
        "other-tenant"
    ).zernio_account_verified is True
    lawyer_connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert lawyer_connection is not None
    assert lawyer_connection.zernio_account_id is None
    assert lawyer_connection.zernio_account_verified is False
    client_config = json.loads(
        (tmp_path / "tenants" / "lawyer" / "config" / "client.json").read_text()
    )
    assert client_config["channel_account_allowlist"]["zernio_accounts"] == []
    assert any(
        event.action == "whatsapp.callback_provider_ownership_conflict"
        and event.result == "blocked"
        for event in audit_log.list_events()
    )


def test_whatsapp_callback_rotation_is_rejected_without_stale_request_write(
    monkeypatch, tmp_path,
):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("rotating_generation_state")
    old_generation = created.tenant_generation_id

    class RotatingGenerationService:
        def get_account(self, account_id):
            from app.delete_operations import (
                bind_tenant_generation_for_creation,
                start_delete_operation,
                update_delete_operation,
            )
            from app.provisioning import tenant_creation_lock

            operation = start_delete_operation(
                slug="lawyer",
                tenant_generation_id=old_generation,
                generation_fingerprint="sha256:" + "7" * 64,
                account_ids=[],
                profile_ids=["profile_lawyer"],
            )
            update_delete_operation(
                slug="lawyer",
                operation_id=operation["operation_id"],
                expected_phases={"preparing"},
                phase="deleted",
            )
            channel_connections.forget_tenant("lawyer")
            with tenant_creation_lock("lawyer"):
                bind_tenant_generation_for_creation(
                    slug="lawyer",
                    generation_id="replacement-generation",
                    status="active",
                )
            return ZernioAccountSummary(
                id=account_id,
                platform="whatsapp",
                profile_id="profile_lawyer",
                profile_name="Old Lawyer",
                display_name="Old Lawyer WhatsApp",
                username="+599 9 000 0000",
                enabled=True,
                is_active=True,
                platform_status="active",
                display_phone_number="+599 9 000 0000",
                phone_number_id="stale_phone",
                waba_id="stale_waba",
            )

    monkeypatch.setattr(
        "app.routes.connect.ZernioService", RotatingGenerationService
    )

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "rotating_generation_state",
            "status": "success",
            "accountId": "stale_account",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].endswith("status=failed&tenantId=lawyer")
    assert channel_connections.get_connection_request(created.id) is None
    replacement = channel_connections.get_tenant_channel_connection("lawyer")
    assert replacement is None or replacement.zernio_account_id != "stale_account"
    config = json.loads(
        (tmp_path / "tenants" / "lawyer" / "config" / "client.json").read_text()
    )
    assert config["channel_account_allowlist"]["zernio_accounts"] == []


def test_whatsapp_callback_marks_pending_when_number_is_missing(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("state_pending")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "state_pending",
            "status": "pending-number",
            "accountId": "account_1",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=pending-number&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "pending_number"
    assert stored.zernio_account_id == "account_1"
    assert stored.zernio_account_verified is False
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "pending"
    assert connection.zernio_account_id == "account_1"
    assert connection.zernio_account_verified is False


def test_whatsapp_callback_marks_failed_on_error(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    created = _connection_request("state_failed")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={
            "state": "state_failed",
            "status": "failed",
            "error": "Client denied authorization.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/connect/whatsapp/result?status=failed&tenantId=lawyer"
    )

    stored = channel_connections.get_connection_request(created.id)
    assert stored is not None
    assert stored.status == "failed"
    assert stored.error_summary == "Client denied authorization."
    connection = channel_connections.get_tenant_channel_connection("lawyer")
    assert connection is not None
    assert connection.status == "failed"
    assert connection.last_error == "Client denied authorization."


def test_whatsapp_callback_rejects_invalid_state_safely(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)
    _connection_request("state_real")

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={"state": "wrong_state", "status": "success"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connect/whatsapp/result?status=failed"


def test_whatsapp_callback_rejects_missing_state_safely(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    response = client.get(
        "/internal/api/connect/whatsapp/callback",
        params={"status": "success"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/connect/whatsapp/result?status=failed"


def test_whatsapp_result_pages_render_safe_copy(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path)

    success = client.get("/connect/whatsapp/result?status=success")
    pending = client.get("/connect/whatsapp/result?status=pending-number")
    activating = client.get("/connect/whatsapp/result?status=pending-activation")
    failed = client.get("/connect/whatsapp/result?status=failed")
    unknown = client.get("/connect/whatsapp/result?status=<script>")

    assert success.status_code == 200
    assert "Connection received" in success.text
    assert pending.status_code == 200
    assert "Phone number needs review" in pending.text
    assert activating.status_code == 200
    assert "Activation in progress" in activating.text
    assert failed.status_code == 200
    assert "Connection not completed" in failed.text
    assert unknown.status_code == 200
    assert "Connection not completed" in unknown.text
    assert "<script>" not in unknown.text
