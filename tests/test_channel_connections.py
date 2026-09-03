import json
import sqlite3

import pytest

from app import channel_connections


def _table_columns(db_path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {
            str(row[1])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }


def test_channel_connection_migration_creates_required_tables(monkeypatch, tmp_path):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))

    channel_connections.init_db()
    channel_connections.init_db()

    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert "connection_requests" in tables
    assert "tenant_channel_connections" in tables
    assert "tenants" in tables
    assert "zernio_profile_id" in _table_columns(db_path, "tenants")


def test_channel_connection_migration_adds_zernio_profile_to_legacy_tenants(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE tenants (
                slug TEXT PRIMARY KEY,
                name TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO tenants (slug, name) VALUES ('lawyer', 'Lawyer')"
        )

    channel_connections.init_db()

    assert "zernio_profile_id" in _table_columns(db_path, "tenants")
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT slug, name FROM tenants WHERE slug = 'lawyer'"
        ).fetchone()
    assert row == ("lawyer", "Lawyer")


def test_migration_quarantines_legacy_duplicate_verified_account_owners(
    monkeypatch,
    tmp_path,
):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))
    channel_connections.init_db()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX uq_verified_zernio_account_owner")
        for tenant_id, profile_id in (
            ("tenant-one", "profile_one"),
            ("tenant-two", "profile_two"),
        ):
            conn.execute(
                """
                INSERT INTO tenant_channel_connections (
                    tenant_id, channel, provider, status,
                    zernio_profile_id, zernio_account_id,
                    zernio_account_verified, metadata_json,
                    created_at, updated_at
                )
                VALUES (?, 'whatsapp', 'zernio', 'connected', ?,
                        'account_shared', 1, '{}', ?, ?)
                """,
                (
                    tenant_id,
                    profile_id,
                    "2026-09-02T12:00:00+00:00",
                    "2026-09-02T12:00:00+00:00",
                ),
            )

    channel_connections.init_db()

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT tenant_id, status, zernio_account_verified, last_error
            FROM tenant_channel_connections
            ORDER BY tenant_id
            """
        ).fetchall()
        index = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'index' AND name = 'uq_verified_zernio_account_owner'
            """
        ).fetchone()
    assert rows == [
        (
            "tenant-one",
            "failed",
            0,
            "Provider ownership is ambiguous; reconnect this tenant before routing.",
        ),
        (
            "tenant-two",
            "failed",
            0,
            "Provider ownership is ambiguous; reconnect this tenant before routing.",
        ),
    ]
    assert index == ("uq_verified_zernio_account_owner",)
    assert channel_connections.get_tenant_channel_connection_by_account_id(
        "account_shared"
    ) is None


def test_create_connection_request_stores_only_state_hash(monkeypatch, tmp_path):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))

    created = channel_connections.create_connection_request(
        tenant_id="lawyer",
        auth_url="https://zernio.example/connect",
        zernio_profile_id="profile_123",
    )

    assert created.request.tenant_id == "lawyer"
    assert created.request.status == "pending"
    assert created.request.auth_url == "https://zernio.example/connect"
    assert created.request.zernio_profile_id == "profile_123"
    assert created.state_token
    assert created.state_token not in created.request.state_token_hash
    assert (
        created.request.state_token_hash
        == channel_connections.hash_state_token(created.state_token)
    )

    found = channel_connections.find_connection_request_by_state_token(
        created.state_token
    )
    assert found is not None
    assert found.id == created.request.id


def test_update_connection_request_and_connection_upsert(monkeypatch, tmp_path):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))
    created = channel_connections.create_connection_request(tenant_id="lawyer")

    updated = channel_connections.update_connection_request(
        created.request.id,
        status="connected",
        zernio_account_id="account_123",
        selected_phone_number_id="phone_123",
        display_phone_number="+599 9 694 5527",
        callback_payload={"accountId": "account_123"},
    )

    assert updated.status == "connected"
    assert updated.zernio_account_id == "account_123"
    assert updated.selected_phone_number_id == "phone_123"
    assert updated.display_phone_number == "+599 9 694 5527"
    assert json.loads(updated.callback_payload_json or "{}") == {
        "accountId": "account_123"
    }

    connection = channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="connected",
        zernio_profile_id="profile_123",
        zernio_account_id="account_123",
        phone_number_id="phone_123",
        display_phone_number="+599 9 694 5527",
        waba_id="waba_123",
        metadata={"qualityRating": "GREEN"},
        last_request_id=created.request.id,
    )

    assert connection.tenant_id == "lawyer"
    assert connection.channel == "whatsapp"
    assert connection.provider == "zernio"
    assert connection.status == "connected"
    assert connection.zernio_profile_id == "profile_123"
    assert connection.zernio_account_id == "account_123"
    assert connection.phone_number_id == "phone_123"
    assert connection.display_phone_number == "+599 9 694 5527"
    assert connection.waba_id == "waba_123"
    assert connection.last_request_id == created.request.id
    assert connection.connected_at
    assert json.loads(connection.metadata_json) == {"qualityRating": "GREEN"}

    fetched = channel_connections.get_tenant_channel_connection("lawyer")
    assert fetched is not None
    assert fetched.id == connection.id

    failed = channel_connections.upsert_tenant_channel_connection(
        tenant_id="lawyer",
        status="failed",
        last_error="Authorization failed.",
    )
    assert failed.id == connection.id
    assert failed.status == "failed"
    assert failed.last_error == "Authorization failed."


def test_tenant_zernio_profile_helpers(monkeypatch, tmp_path):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))

    assert channel_connections.get_tenant_zernio_profile_id("lawyer") is None

    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_123",
    )

    assert (
        channel_connections.get_tenant_zernio_profile_id("lawyer")
        == "profile_123"
    )


def test_verified_zernio_account_cannot_be_owned_by_two_tenants(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    first = channel_connections.upsert_tenant_channel_connection(
        tenant_id="tenant-one",
        status="connected",
        zernio_profile_id="profile_one",
        zernio_account_id="account_shared",
        zernio_account_verified=True,
    )

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="account is already verified",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="tenant-two",
            status="connected",
            zernio_profile_id="profile_two",
            zernio_account_id="account_shared",
            zernio_account_verified=True,
        )

    assert first.zernio_account_verified is True
    assert channel_connections.get_tenant_channel_connection("tenant-two") is None
    owner = channel_connections.get_tenant_channel_connection_by_account_id(
        "account_shared"
    )
    assert owner is not None
    assert owner.tenant_id == "tenant-one"


def test_verified_provider_ids_are_trimmed_before_ownership_is_claimed(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    first = channel_connections.upsert_tenant_channel_connection(
        tenant_id="tenant-one",
        status="connected",
        zernio_profile_id="  profile_one  ",
        zernio_account_id="  account_shared  ",
        zernio_account_verified=True,
    )

    assert first.zernio_profile_id == "profile_one"
    assert first.zernio_account_id == "account_shared"
    with pytest.raises(channel_connections.ProviderOwnershipConflict):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="tenant-two",
            status="connected",
            zernio_profile_id="profile_two",
            zernio_account_id="account_shared",
            zernio_account_verified=True,
        )


def test_legacy_whitespace_duplicate_accounts_are_quarantined_on_migration(
    monkeypatch, tmp_path,
):
    db_path = tmp_path / "nr3.db"
    monkeypatch.setenv("NR3_DB_PATH", str(db_path))
    channel_connections.init_db()
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="tenant-one",
        status="connected",
        zernio_profile_id="profile_one",
        zernio_account_id="account_shared",
        zernio_account_verified=True,
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX uq_verified_zernio_account_owner")
        conn.execute("DROP INDEX uq_verified_zernio_profile_owner")
        conn.execute(
            """
            INSERT INTO tenant_channel_connections (
                tenant_id, channel, provider, status, zernio_profile_id,
                zernio_account_id, zernio_account_verified, metadata_json,
                created_at, updated_at
            ) VALUES (?, 'whatsapp', 'zernio', 'connected', ?, ?, 1, '{}', ?, ?)
            """,
            (
                "tenant-two",
                "profile_two",
                " account_shared ",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
            ),
        )

    channel_connections.init_db()

    first = channel_connections.get_tenant_channel_connection("tenant-one")
    second = channel_connections.get_tenant_channel_connection("tenant-two")
    assert first is not None and second is not None
    assert first.zernio_account_verified is False
    assert second.zernio_account_verified is False
    assert first.status == second.status == "failed"


def test_verified_connection_requires_provider_account_id(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="requires an account id",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="tenant-one",
            status="connected",
            zernio_profile_id="profile_one",
            zernio_account_verified=True,
        )


def test_verified_zernio_profile_cannot_be_owned_by_two_tenants(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="tenant-one",
        status="connected",
        zernio_profile_id="profile_shared",
        zernio_account_id="account_one",
        zernio_account_verified=True,
    )

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="profile is already verified",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="tenant-two",
            status="connected",
            zernio_profile_id="profile_shared",
            zernio_account_id="account_two",
            zernio_account_verified=True,
        )

    assert channel_connections.get_tenant_channel_connection("tenant-two") is None


def test_zernio_profile_registry_rejects_cross_tenant_connection_owner(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="tenant-one",
        name="Tenant One",
        zernio_profile_id="profile_shared",
    )

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="profile is already assigned",
    ):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="tenant-two",
            status="connected",
            zernio_profile_id="profile_shared",
            zernio_account_id="account_two",
            zernio_account_verified=True,
        )


def test_provider_ownership_helper_ignores_history_but_blocks_current_other_owner(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    historical = channel_connections.create_connection_request(
        tenant_id="former-tenant",
        zernio_profile_id="historical_profile",
    ).request
    channel_connections.update_connection_request(
        historical.id,
        status="connected",
        zernio_account_id="historical_account",
        zernio_account_verified=True,
    )

    assert channel_connections.provider_id_owned_by_other_tenant(
        tenant_id="deleting-tenant",
        zernio_account_id="historical_account",
    ) is False

    channel_connections.upsert_tenant_channel_connection(
        tenant_id="current-owner",
        status="connected",
        zernio_profile_id="current_profile",
        zernio_account_id="historical_account",
        zernio_account_verified=True,
    )

    assert channel_connections.provider_id_owned_by_other_tenant(
        tenant_id="deleting-tenant",
        zernio_account_id=" historical_account ",
    ) is True
    assert channel_connections.provider_id_owned_by_other_tenant(
        tenant_id="current-owner",
        zernio_account_id="historical_account",
    ) is False


def test_provider_ownership_helper_checks_profile_registry(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="current-owner",
        name="Current Owner",
        zernio_profile_id="current_profile",
    )

    assert channel_connections.provider_id_owned_by_other_tenant(
        tenant_id="deleting-tenant",
        zernio_profile_id=" current_profile ",
    ) is True
    assert channel_connections.provider_id_owned_by_other_tenant(
        tenant_id="current-owner",
        zernio_profile_id="current_profile",
    ) is False


def test_invalid_connection_status_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))

    with pytest.raises(ValueError):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="lawyer",
            status="secretly_connected",
        )


def test_new_profile_is_compensated_when_local_persistence_fails(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    deleted: list[str] = []
    monkeypatch.setattr(
        channel_connections,
        "_set_tenant_zernio_profile_id_unlocked",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="deleted safely",
    ):
        channel_connections.ensure_tenant_zernio_profile(
            tenant_id="lawyer",
            create_profile=lambda: "profile_new",
            delete_profile=lambda profile_id: deleted.append(profile_id),
        )

    assert deleted == ["profile_new"]
    assert channel_connections.list_tenant_orphan_profile_ids("lawyer") == []


def test_failed_profile_compensation_is_durably_discoverable_for_delete(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setattr(
        channel_connections,
        "_set_tenant_zernio_profile_id_unlocked",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )

    def fail_delete(_profile_id: str) -> None:
        raise RuntimeError("provider unavailable")

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="recorded for deletion retry",
    ):
        channel_connections.ensure_tenant_zernio_profile(
            tenant_id="lawyer",
            create_profile=lambda: "profile_orphan",
            delete_profile=fail_delete,
        )

    assert channel_connections.list_tenant_orphan_profile_ids("lawyer") == [
        "profile_orphan"
    ]
    assert "profile_orphan" in channel_connections.list_tenant_zernio_ids(
        "lawyer"
    )["profile_ids"]


def test_profile_collision_never_compensates_another_tenants_profile(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="owner-tenant",
        zernio_profile_id="profile_shared",
    )
    deleted: list[str] = []

    with pytest.raises(
        channel_connections.ProviderOwnershipConflict,
        match="not deleted",
    ):
        channel_connections.ensure_tenant_zernio_profile(
            tenant_id="new-tenant",
            create_profile=lambda: "profile_shared",
            delete_profile=lambda profile_id: deleted.append(profile_id),
        )

    assert deleted == []
    assert channel_connections.list_tenant_orphan_profile_ids("new-tenant") == []
