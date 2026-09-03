import json
import re
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import audit_log
from app.password_recovery import validate_new_password
from app.provisioning import AutoProvisionResult


def _seed_tenant(root, slug="acme", email="owner@example.com"):
    tenant_dir = root / slug / "config"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "client.json").write_text(
        json.dumps({
            "slug": slug,
            "name": "Acme",
            "email": email,
            "password": "old-password",
            "status": "active",
        }),
        encoding="utf-8",
    )


def test_forgot_password_response_is_generic_for_unknown_email(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    calls = []
    monkeypatch.setattr("app.password_recovery.send_email", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    _seed_tenant(tmp_path / "clients")

    client = TestClient(app)
    response = client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "unknown@example.com"},
    )

    assert response.status_code == 200
    assert "If this email exists" in response.text
    assert calls == []


def test_password_reset_token_is_hashed_and_single_use(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")

    sent = []

    def fake_send(to_email, subject, body, settings):
        sent.append({"to": to_email, "subject": subject, "body": body})

    def fake_queue(**kwargs):
        assert kwargs["slug"] == "acme"
        assert kwargs["action"] == "reset_dashboard_password"
        assert kwargs["new_password"] == "Better-Password-123"
        return AutoProvisionResult(
            status="succeeded",
            message="Password reset.",
            job_id="job-reset",
        )

    monkeypatch.setattr("app.password_recovery.send_email", fake_send)
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    monkeypatch.setattr("app.password_recovery.queue_tenant_host_action", fake_queue)

    client = TestClient(app)
    response = client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "OWNER@example.com"},
    )
    assert response.status_code == 200
    assert len(sent) == 1
    match = re.search(r"https://icp\.example\.test/password/reset/([A-Za-z0-9_\-]+)", sent[0]["body"])
    assert match, sent[0]["body"]
    raw_token = match.group(1)
    assert raw_token not in (tmp_path / "nr3.db").read_bytes().decode("latin1", errors="ignore")

    reset = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Better-Password-123",
            "confirm_password": "Better-Password-123",
        },
    )
    assert reset.status_code == 200
    assert "Password reset complete" in reset.text

    reused = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Another-Password-123",
            "confirm_password": "Another-Password-123",
        },
    )
    assert reused.status_code == 200
    assert "invalid or expired" in reused.text


def test_password_reset_token_is_bound_to_exact_tenant_generation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")
    sent = []
    monkeypatch.setattr(
        "app.password_recovery.send_email",
        lambda _to, _subject, body, _settings: sent.append(body),
    )
    monkeypatch.setattr(
        "app.password_recovery.smtp_is_configured", lambda _settings: True
    )
    monkeypatch.setattr(
        "app.password_recovery.queue_tenant_host_action",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("old-generation token must not queue a host action")
        ),
    )

    client = TestClient(app)
    client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "owner@example.com"},
    )
    raw_token = re.search(r"/password/reset/([A-Za-z0-9_\-]+)", sent[0]).group(1)

    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        retire_tenant_generation,
    )
    from app.provisioning import tenant_creation_lock

    with tenant_creation_lock("acme"):
        retire_tenant_generation(slug="acme")
        bind_tenant_generation_for_creation(
            slug="acme",
            generation_id="replacement-generation-0001",
            status="active",
        )

    response = client.post(
        f"/password/reset/{raw_token}",
        data={
            "password": "Replacement-Password-123",
            "confirm_password": "Replacement-Password-123",
        },
    )

    assert response.status_code == 200
    assert "invalid or expired" in response.text


def test_legacy_unbound_reset_token_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    _seed_tenant(tmp_path / "clients")
    from app.password_recovery import _hash_token, get_valid_token, init_db

    raw_token = "legacy-token-that-has-no-generation-binding"
    init_db()
    with sqlite3.connect(tmp_path / "nr3.db") as conn:
        conn.execute(
            """
            INSERT INTO password_reset_tokens (
                id, tenant_id, tenant_generation_id, email, email_key,
                token_hash, requested_ip, created_at, expires_at, used_at,
                reset_job_id
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, datetime('now'),
                      datetime('now', '+1 hour'), NULL, NULL)
            """,
            (
                "pr_legacy_unbound",
                "acme",
                "owner@example.com",
                "owner@example.com",
                _hash_token(raw_token),
                "127.0.0.1",
            ),
        )

    assert get_valid_token(raw_token) is None


def test_failed_reset_queue_still_consumes_token_once(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")
    sent = []
    calls = []
    monkeypatch.setattr(
        "app.password_recovery.send_email",
        lambda _to, _subject, body, _settings: sent.append(body),
    )
    monkeypatch.setattr(
        "app.password_recovery.smtp_is_configured", lambda _settings: True
    )

    def fail_queue(**kwargs):
        calls.append(kwargs)
        return AutoProvisionResult(
            status="failed", message="Host action rejected.", job_id=kwargs["requested_job_id"]
        )

    monkeypatch.setattr("app.password_recovery.queue_tenant_host_action", fail_queue)
    client = TestClient(app)
    client.post(
        "/password/forgot",
        data={"workspace": "acme", "email": "owner@example.com"},
    )
    raw_token = re.search(r"/password/reset/([A-Za-z0-9_\-]+)", sent[0]).group(1)
    payload = {
        "password": "Better-Password-123",
        "confirm_password": "Better-Password-123",
    }

    first = client.post(f"/password/reset/{raw_token}", data=payload)
    second = client.post(f"/password/reset/{raw_token}", data=payload)

    assert first.status_code == 200
    assert "Host action rejected" in first.text
    assert second.status_code == 200
    assert "invalid or expired" in second.text
    assert len(calls) == 1
    assert calls[0]["generation_fingerprint"].startswith("sha256:")
    assert calls[0]["requested_job_id"].startswith("password-reset-pr_")


def test_strict_tenant_cleanup_removes_all_password_reset_tokens(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "overrides.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    _seed_tenant(tmp_path / "clients")
    monkeypatch.setattr(
        "app.password_recovery.send_email", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "app.password_recovery.smtp_is_configured", lambda _settings: True
    )

    from app.password_recovery import request_reset, tenant_state_exists
    from app.tenants import forget_tenant_state_strict

    request_reset(
        tenant_id="acme",
        email="owner@example.com",
        ip_address="127.0.0.1",
    )
    assert tenant_state_exists("acme") is True

    forget_tenant_state_strict("acme")

    assert tenant_state_exists("acme") is False


def test_reset_rejects_weak_password_before_queue(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_BASE_URL", "https://icp.example.test")
    _seed_tenant(tmp_path / "clients")
    sent = []
    monkeypatch.setattr("app.password_recovery.send_email", lambda to, subject, body, settings: sent.append(body))
    monkeypatch.setattr("app.password_recovery.smtp_is_configured", lambda settings: True)
    monkeypatch.setattr(
        "app.password_recovery.queue_tenant_host_action",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("queue should not run")),
    )

    client = TestClient(app)
    client.post("/password/forgot", data={"workspace": "acme", "email": "owner@example.com"})
    raw_token = re.search(r"/password/reset/([A-Za-z0-9_\\-]+)", sent[0]).group(1)

    response = client.post(
        f"/password/reset/{raw_token}",
        data={"password": "short", "confirm_password": "short"},
    )

    assert response.status_code == 200
    assert "Use at least 12 characters" in response.text


@pytest.mark.parametrize(
    ("password", "confirm"),
    [
        ("Strong-Password-123\nNR3_QUEUE_TOKEN=stolen",) * 2,
        ("Strong-Password-123\rsecond-line",) * 2,
        ("Strong-Password-123\x00hidden",) * 2,
        ("Strong-Password-123", "Strong-Password-123\t"),
        ("Strong-Password-123\x7f",) * 2,
    ],
)
def test_password_validation_rejects_control_characters(password, confirm):
    with pytest.raises(ValueError, match="control characters"):
        validate_new_password(password, confirm)


def test_admin_can_generate_temporary_dashboard_password_once(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    _seed_tenant(tmp_path / "clients")
    queued = {}

    def fake_queue(**kwargs):
        queued.update(kwargs)
        return AutoProvisionResult(
            status="succeeded",
            message="Password reset.",
            job_id="job-admin-reset",
        )

    monkeypatch.setattr("app.routes.admin.queue_tenant_host_action", fake_queue)
    monkeypatch.setattr(
        "app.routes.admin._generate_temporary_dashboard_password",
        lambda: "Temp-Password-123!",
    )

    client = TestClient(app)
    assert client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    ).status_code == 303

    page = client.get("/admin/tenants/acme")
    assert page.status_code == 200
    assert "Admin temporary password reset" in page.text
    assert "Generate temporary password" in page.text
    assert "Temp-Password-123!" not in page.text
    generation_match = re.search(
        r'name="tenant_generation_id" value="([^"]+)"', page.text
    )
    assert generation_match
    tenant_generation_id = generation_match.group(1)

    bad = client.post(
        "/admin/tenants/acme/password-reset/temp",
        data={"confirmation": "reset password Acme"},
        follow_redirects=True,
    )
    assert bad.status_code == 200
    assert "Type exactly" in bad.text
    assert "reset password acme" in bad.text
    assert queued == {}

    response = client.post(
        "/admin/tenants/acme/password-reset/temp",
        data={
            "confirmation": "reset password acme",
            "tenant_generation_id": tenant_generation_id,
        },
    )

    assert response.status_code == 200
    assert "Temporary password generated" in response.text
    assert "Temp-Password-123!" in response.text
    assert queued["slug"] == "acme"
    assert queued["action"] == "reset_dashboard_password"
    assert queued["new_password"] == "Temp-Password-123!"
    events = audit_log.list_events()
    event_blob = "\n".join(
        f"{event.action} {event.safe_summary} {event.metadata_json}"
        for event in events
    )
    assert "tenant.dashboard_password_admin_reset" in event_blob
    assert "Temp-Password-123!" not in event_blob


def test_admin_temporary_password_rejects_stale_rendered_generation(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    _seed_tenant(tmp_path / "clients")
    queued = []
    monkeypatch.setattr(
        "app.routes.admin.queue_tenant_host_action",
        lambda **kwargs: queued.append(kwargs),
    )

    client = TestClient(app)
    client.post(
        "/login",
        data={"password": "test-password"},
        follow_redirects=False,
    )
    page = client.get("/admin/tenants/acme")
    generation_match = re.search(
        r'name="tenant_generation_id" value="([^"]+)"', page.text
    )
    assert generation_match

    from app.delete_operations import (
        bind_tenant_generation_for_creation,
        retire_tenant_generation,
    )
    from app.provisioning import tenant_creation_lock

    with tenant_creation_lock("acme"):
        retire_tenant_generation(slug="acme")
        bind_tenant_generation_for_creation(
            slug="acme",
            generation_id="replacement-generation-0002",
            status="active",
        )

    response = client.post(
        "/admin/tenants/acme/password-reset/temp",
        data={
            "confirmation": "reset password acme",
            "tenant_generation_id": generation_match.group(1),
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "Temporary password reset was blocked" in response.text
    assert queued == []
