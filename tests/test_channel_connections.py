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


def test_invalid_connection_status_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))

    with pytest.raises(ValueError):
        channel_connections.upsert_tenant_channel_connection(
            tenant_id="lawyer",
            status="secretly_connected",
        )
