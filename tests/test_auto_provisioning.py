import hashlib
import json
import sqlite3
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from app.provisioning import (
    auto_provision_tenant,
    create_tenant_provision_claim,
    queue_tenant_host_action,
    reconcile_host_action_results,
    tenant_provision_claim,
    update_tenant_provision_claim_job,
)
from host.nr3_provision_worker import atomic_write


DELETE_OPERATION_ID = "a" * 32
DELETE_GENERATION = "sha256:" + "b" * 64
DELETE_BACKUP_DIGEST = "sha256:" + "c" * 64


def _seed_control_panel_runtime(monkeypatch, tmp_path, slug: str = "acme") -> str:
    """Mount one exact existing generation for guarded host-action tests."""
    from app.delete_operations import read_tenant_generation

    clients = tmp_path / "clients"
    config = clients / slug / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "client.json").write_text(
        json.dumps({
            "slug": slug,
            "creation_id": f"creation-{slug}",
            "status": "active",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(clients))
    return read_tenant_generation(slug)[1]


def _seed_delete_generation(worker, tenant_dir: Path, slug: str = "acme") -> str:
    config = tenant_dir / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "client.json").write_text(
        json.dumps({"slug": slug, "creation_id": f"creation-{slug}"}),
        encoding="utf-8",
    )
    return worker.tenant_generation_fingerprint(slug, tenant_dir)


def test_host_worker_atomic_write_can_enforce_private_mode(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    target = tmp_path / "config" / "platform.env"
    fsynced = []
    monkeypatch.setattr(
        worker,
        "_fsync_directory_required",
        lambda path: fsynced.append(Path(path)),
    )

    atomic_write(target, "SECRET=value\n", mode=0o600)

    assert target.read_text(encoding="utf-8") == "SECRET=value\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert fsynced == [target.parent]


def test_host_worker_provisions_private_fail_closed_tenant_files(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    clients = tmp_path / "clients"
    results = tmp_path / "results"
    failed = tmp_path / "failed"
    nginx_site = tmp_path / "api-unboks"
    nginx_site.write_text("server_name api.unboks.org;\n", encoding="utf-8")
    monkeypatch.setattr(worker, "CLIENTS_ROOT", clients)
    monkeypatch.setattr(worker, "RESULT_DIR", results)
    monkeypatch.setattr(worker, "FAILED_DIR", failed)
    monkeypatch.setattr(worker, "NGINX_SITE", nginx_site)
    monkeypatch.setattr(
        worker,
        "rotate_tenant_bridge_token",
        lambda _slug: "tenant-bridge-token-at-least-32-bytes-long",
    )
    monkeypatch.setattr(worker, "insert_nginx_block", lambda _slug, _block: None)
    monkeypatch.setattr(worker, "wait_for_health", lambda _port: "health ok")
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, ""),
    )

    job_path = tmp_path / "job-acme.json"
    job_path.write_text(
        json.dumps({
            "job_id": "job-acme",
            "job_type": "tenant_provision",
            "creation_id": "creation-acme",
            "slug": "acme",
            "host_port": 8123,
            "client_data": {
                "slug": "acme",
                "password": "temporary-password",
                "channel_account_allowlist": {
                    "mode": "permissive",
                    "zernio_accounts": ["caller-supplied-account"],
                },
            },
            "docker_compose_text": worker.canonical_docker_compose_text(
                "acme", 8123
            ),
            "managed_nginx_block_text": (
                worker.canonical_managed_nginx_block_text("acme", 8123)
            ),
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    client_path = clients / "acme" / "config" / "client.json"
    env_path = clients / "acme" / "config" / "platform.env"
    client_data = json.loads(client_path.read_text(encoding="utf-8"))
    assert client_data["channel_account_allowlist"]["mode"] == "strict"
    assert client_data["channel_account_allowlist"]["zernio_accounts"] == []
    assert stat.S_IMODE(client_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    result_path = results / "job-acme.json"
    assert json.loads(result_path.read_text())["status"] == "succeeded"
    assert stat.S_IMODE(result_path.stat().st_mode) == 0o600


def test_host_worker_rejects_provision_without_canonical_tenant_headers(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "CLIENTS_ROOT", tmp_path / "clients")
    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    job_path = tmp_path / "job-unsafe.json"
    job_path.write_text(
        json.dumps({
            "job_id": "job-unsafe",
            "job_type": "tenant_provision",
            "creation_id": "creation-unsafe",
            "slug": "unsafe",
            "host_port": 8123,
            "client_data": {"slug": "unsafe", "password": "temporary-password"},
            "docker_compose_text": worker.canonical_docker_compose_text(
                "unsafe", 8123
            ),
            "managed_nginx_block_text": "location ^~ /api/unsafe/ {}",
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    result = json.loads((tmp_path / "results" / "job-unsafe.json").read_text())
    assert result["status"] == "failed"
    assert result["creation_id"] == "creation-unsafe"
    assert "canonical marker pair" in result["message"]
    assert not (tmp_path / "clients" / "unsafe").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda block: block.replace(
            "proxy_pass http://127.0.0.1:8123/;",
            "proxy_pass http://127.0.0.1:8999/;",
        ),
        lambda block: block.replace(
            'add_header X-Unboks-Tenant "acme" always;',
            'add_header X-Unboks-Tenant "acme" always;\n'
            'add_header X-Unboks-Tenant "other" always;',
        ),
        lambda block: block.replace("# END UNBOKS TENANT acme", ""),
    ],
)
def test_host_worker_rejects_noncanonical_or_nonremovable_nginx_blocks(mutate):
    from app.signup_service import _managed_nginx_block_text
    from host.nr3_provision_worker import validate_managed_nginx_block

    block = _managed_nginx_block_text("acme", 8123)
    with pytest.raises(RuntimeError):
        validate_managed_nginx_block("acme", 8123, mutate(block))


def test_host_worker_accepts_generated_canonical_nginx_block():
    from app.signup_service import _managed_nginx_block_text
    from host.nr3_provision_worker import validate_managed_nginx_block

    validate_managed_nginx_block(
        "acme",
        8123,
        _managed_nginx_block_text("acme", 8123),
    )


def test_nginx_edit_is_atomic_and_preserves_resolved_site_symlink(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    site_target = tmp_path / "sites-available" / "api-unboks"
    site_target.parent.mkdir()
    original = (
        "server {\n    server_name api.unboks.org;\n}\n"
        + worker.canonical_managed_nginx_block_text("acme", 8123)
    )
    site_target.write_text(original, encoding="utf-8")
    site_target.chmod(0o640)
    site_link = tmp_path / "sites-enabled" / "api-unboks"
    site_link.parent.mkdir()
    site_link.symlink_to(site_target)
    monkeypatch.setattr(worker, "NGINX_SITE", site_link)
    monkeypatch.setattr(worker, "NGINX_BACKUP_DIR", tmp_path / "backups")
    real_replace = worker.os.replace

    def interrupt_publish(source, destination):
        if Path(destination) == site_target:
            raise OSError("simulated interruption before atomic publish")
        return real_replace(source, destination)

    monkeypatch.setattr(worker.os, "replace", interrupt_publish)
    with pytest.raises(OSError, match="simulated interruption"):
        worker.remove_nginx_block("acme")

    assert site_link.is_symlink()
    assert site_target.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(site_target.stat().st_mode) == 0o640
    assert not list(site_target.parent.glob(".api-unboks.nr3-*.tmp"))


def test_host_worker_rejects_reserved_tenant_provision(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "CLIENTS_ROOT", tmp_path / "clients")
    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    job_path = tmp_path / "job-unboks.json"
    job_path.write_text(
        json.dumps({
            "job_id": "job-unboks",
            "job_type": "tenant_provision",
            "creation_id": "creation-unboks",
            "slug": "unboks",
            "host_port": 8123,
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    result = json.loads((tmp_path / "results" / "job-unboks.json").read_text())
    assert result["status"] == "failed"
    assert result["creation_id"] == "creation-unboks"
    assert "reserved" in result["message"]
    assert not (tmp_path / "clients" / "unboks").exists()


def test_host_worker_rejects_noncanonical_compose(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "CLIENTS_ROOT", tmp_path / "clients")
    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    job_path = tmp_path / "job-compose.json"
    job_path.write_text(
        json.dumps({
            "job_id": "job-compose",
            "job_type": "tenant_provision",
            "creation_id": "creation-compose",
            "slug": "acme",
            "host_port": 8123,
            "client_data": {
                "slug": "acme",
                "password": "temporary-password",
            },
            "docker_compose_text": (
                worker.canonical_docker_compose_text("acme", 8123)
                + "    privileged: true\n"
            ),
            "managed_nginx_block_text": (
                worker.canonical_managed_nginx_block_text("acme", 8123)
            ),
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    result = json.loads((tmp_path / "results" / "job-compose.json").read_text())
    assert result["status"] == "failed"
    assert "not the canonical tenant runtime" in result["message"]
    assert not (tmp_path / "clients" / "acme").exists()


def test_host_worker_rejects_payload_job_id_that_differs_from_filename(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    job_path = tmp_path / "owned-job.json"
    job_path.write_text(
        json.dumps({
            "job_id": "other-job",
            "job_type": "tenant_action",
            "action": "restart_tenant",
            "slug": "acme",
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    assert (tmp_path / "results" / "owned-job.json").exists()
    assert not (tmp_path / "results" / "other-job.json").exists()
    result = json.loads((tmp_path / "results" / "owned-job.json").read_text())
    assert result["status"] == "failed"
    assert "does not match its queue filename" in result["message"]


def test_host_worker_preserves_tenant_action_failure_correlation(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "CLIENTS_ROOT", tmp_path / "clients")
    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    job_path = tmp_path / "restart-acme.json"
    job_path.write_text(
        json.dumps({
            "job_id": "restart-acme",
            "job_type": "tenant_action",
            "action": "restart_tenant",
            "slug": "acme",
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    result = json.loads((tmp_path / "results" / "restart-acme.json").read_text())
    assert result["status"] == "failed"
    assert result["job_type"] == "tenant_action"
    assert result["action"] == "restart_tenant"
    assert result["slug"] == "acme"
    assert "Tenant directory not found" in result["message"]


def test_host_worker_executes_strict_allowlist_repair(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    tenant_dir = tmp_path / "clients" / "acme"
    client_path = tenant_dir / "config" / "client.json"
    client_path.parent.mkdir(parents=True)
    client_path.write_text(
        json.dumps({
            "slug": "acme",
            "channel_account_allowlist": {
                "mode": "permissive",
                "zernio_accounts": ["existing-account"],
            },
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "CLIENTS_ROOT", tmp_path / "clients")
    monkeypatch.setattr(worker, "RESULT_DIR", tmp_path / "results")
    monkeypatch.setattr(worker, "FAILED_DIR", tmp_path / "failed")
    generation = worker.tenant_generation_fingerprint("acme", tenant_dir)
    job_path = tmp_path / "repair-acme.json"
    job_path.write_text(
        json.dumps({
            "job_id": "repair-acme",
            "job_type": "tenant_action",
            "action": "repair_whatsapp_allowlist",
            "slug": "acme",
            "zernio_account_id": "verified-account",
            "allowlist_note": "Verified by provider callback.",
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.process_job(job_path)

    client = json.loads(client_path.read_text(encoding="utf-8"))
    assert client["channel_account_allowlist"] == {
        "mode": "strict",
        "zernio_accounts": ["verified-account"],
        "notes": "Verified by provider callback.",
    }
    assert stat.S_IMODE(client_path.stat().st_mode) == 0o600
    result = json.loads((tmp_path / "results" / "repair-acme.json").read_text())
    assert result["status"] == "succeeded"
    assert result["job_type"] == "tenant_action"
    assert result["action"] == "repair_whatsapp_allowlist"


def test_host_health_check_rejects_non_success_response(monkeypatch):
    from host import nr3_provision_worker as worker

    class FakeResponse:
        def __init__(self, status, body):
            self.status = status
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return self.body

    responses = iter([
        FakeResponse(404, b"missing"),
        FakeResponse(204, b"ready"),
    ])
    monkeypatch.setattr(worker.urllib.request, "urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(worker.time, "sleep", lambda _seconds: None)

    result = worker.wait_for_health(8123, timeout=1)

    assert "HTTP 204" in result


def test_auto_provision_disabled_by_default(monkeypatch):
    monkeypatch.delenv("NR3_AUTO_PROVISION", raising=False)

    result = auto_provision_tenant(
        slug="acme",
        host_port=8123,
        client_data={"slug": "acme", "password": "temporary-password"},
        docker_compose_text="services: {}",
        managed_nginx_block_text="# BEGIN UNBOKS TENANT acme",
        dashboard_url="https://dashboard.unboks.org/acme",
    )

    assert result.status == "disabled"
    assert "disabled" in result.message


def test_auto_provision_writes_queue_job_without_waiting(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = auto_provision_tenant(
        slug="acme",
        host_port=8123,
        client_data={"slug": "acme", "password": "temporary-password"},
        docker_compose_text="container_name: wtyj-acme\n",
        managed_nginx_block_text="# BEGIN UNBOKS TENANT acme\nlocation ^~ /api/acme/ {}",
        dashboard_url="https://dashboard.unboks.org/acme",
    )

    assert result.status == "queued"
    assert result.job_id
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_id"] == result.job_id
    assert payload["slug"] == "acme"
    assert payload["host_port"] == 8123
    assert payload["dashboard_url"] == "https://dashboard.unboks.org/acme"
    assert stat.S_IMODE(job_files[0].stat().st_mode) == 0o600


def test_auto_provision_does_not_duplicate_active_slug_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    jobs.mkdir(parents=True)
    existing = jobs / "existing.json"
    existing.write_text(
        json.dumps({
            "job_id": "existing-job",
            "job_type": "tenant_provision",
            "slug": "acme",
            "creation_id": "creation-acme",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = auto_provision_tenant(
        slug="acme",
        host_port=8123,
        client_data={"slug": "acme", "password": "temporary-password"},
        docker_compose_text="container_name: wtyj-acme\n",
        managed_nginx_block_text="# BEGIN UNBOKS TENANT acme\nlocation ^~ /api/acme/ {}",
        dashboard_url="https://dashboard.unboks.org/acme",
        creation_id="creation-acme",
    )

    assert result.status == "queued"
    assert result.job_id == "existing-job"
    assert "already active" in result.message
    assert len(list(jobs.glob("*.json"))) == 1


def test_auto_provision_rejects_active_job_owned_by_other_creation(
    monkeypatch, tmp_path,
):
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True)
    (jobs / "existing.json").write_text(
        json.dumps({
            "job_id": "existing-job",
            "job_type": "tenant_provision",
            "slug": "acme",
            "creation_id": "creation-old",
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = auto_provision_tenant(
        slug="acme",
        host_port=8123,
        client_data={"slug": "acme", "password": "temporary-password"},
        docker_compose_text="container_name: wtyj-acme\n",
        managed_nginx_block_text="# BEGIN UNBOKS TENANT acme",
        dashboard_url="https://dashboard.unboks.org/acme",
        creation_id="creation-new",
    )

    assert result.status == "failed"
    assert result.job_id == "existing-job"
    assert "different or unreadable job" in result.message
    assert len(list(jobs.glob("*.json"))) == 1


def test_corrupt_provision_claims_fail_closed_without_overwrite(monkeypatch, tmp_path):
    claims = tmp_path / "claims.json"
    claims.write_text("{broken", encoding="utf-8")
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(claims))

    with pytest.raises(RuntimeError, match="claims are unreadable"):
        create_tenant_provision_claim("acme", "creation-acme")

    assert claims.read_text(encoding="utf-8") == "{broken"


def test_host_action_queue_writes_suspend_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="suspend_tenant",
        dashboard_url="https://dashboard.unboks.org/acme",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "suspend_tenant"
    assert payload["slug"] == "acme"
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_unpause_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="unpause_tenant",
        dashboard_url="https://dashboard.unboks.org/acme",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "unpause_tenant"
    assert payload["slug"] == "acme"
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_password_reset_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="reset_dashboard_password",
        dashboard_url="https://dashboard.unboks.org/acme",
        new_password="Better-Password-123",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "reset_dashboard_password"
    assert payload["slug"] == "acme"
    assert payload["new_password"] == "Better-Password-123"
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_restart_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="restart_tenant",
        dashboard_url="https://dashboard.unboks.org/acme",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "restart_tenant"
    assert payload["slug"] == "acme"
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_allowlist_repair_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="repair_whatsapp_allowlist",
        zernio_account_id="account_acme",
        allowlist_note="Repair from verified account.",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "repair_whatsapp_allowlist"
    assert payload["slug"] == "acme"
    assert payload["zernio_account_id"] == "account_acme"
    assert payload["allowlist_note"] == "Repair from verified account."
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_restore_runtime_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)

    result = queue_tenant_host_action(
        slug="acme",
        action="restore_tenant_runtime",
        dashboard_url="https://dashboard.unboks.org/acme",
        backup_package_path="/root/unboks-internal-control-panel/data/tenant_import_payloads/acme.unboksbackup",
        preserve_provider_connection=False,
        host_port=8123,
        zernio_account_id="verified-account",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "restore_tenant_runtime"
    assert payload["slug"] == "acme"
    assert payload["backup_package_path"].endswith("acme.unboksbackup")
    assert payload["preserve_provider_connection"] is False
    assert payload["host_port"] == 8123
    assert payload["zernio_account_id"] == "verified-account"
    assert payload["generation_fingerprint"] == generation


def test_host_action_queue_writes_delete_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = queue_tenant_host_action(
        slug="acme",
        action="delete_tenant",
        typed_slug="acme",
        final_confirmation="DELETE FOREVER",
        requested_job_id="delete-acme-operation-1",
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=DELETE_GENERATION,
        prepared_backup_path="/root/_deleted_tenants/acme-prepared",
        prepared_backup_digest=DELETE_BACKUP_DIGEST,
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "delete_tenant"
    assert payload["slug"] == "acme"
    assert payload["typed_slug"] == "acme"
    assert payload["final_confirmation"] == "DELETE FOREVER"
    assert payload["delete_operation_id"] == DELETE_OPERATION_ID
    assert payload["generation_fingerprint"] == DELETE_GENERATION
    assert payload["prepared_backup_digest"] == DELETE_BACKUP_DIGEST


def test_host_action_queue_does_not_duplicate_same_active_action(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    jobs.mkdir(parents=True)
    (jobs / "existing-delete.processing").write_text(
        json.dumps({
            "job_id": "existing-delete",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": DELETE_GENERATION,
            "prepared_backup_path": "/root/_deleted_tenants/acme-prepared",
            "prepared_backup_digest": DELETE_BACKUP_DIGEST,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = queue_tenant_host_action(
        slug="acme",
        action="delete_tenant",
        typed_slug="acme",
        final_confirmation="DELETE FOREVER",
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=DELETE_GENERATION,
        prepared_backup_path="/root/_deleted_tenants/acme-prepared",
        prepared_backup_digest=DELETE_BACKUP_DIGEST,
    )

    assert result.status == "queued"
    assert result.job_id == "existing-delete"
    assert "already active" in result.message
    assert len(list(jobs.glob("*.json"))) == 0


def test_host_action_queue_rejects_conflicting_active_action(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True)
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)
    (jobs / "existing-restart.processing").write_text(
        json.dumps({
            "job_id": "existing-restart",
            "job_type": "tenant_action",
            "action": "restart_tenant",
            "slug": "acme",
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    pre_queue_calls = []
    result = queue_tenant_host_action(
        slug="acme",
        action="repair_whatsapp_allowlist",
        zernio_account_id="account-acme",
        allowlist_note="verified",
        before_queue=lambda: pre_queue_calls.append("called"),
    )

    assert result.status == "failed"
    assert "different host job" in result.message
    assert len(list(jobs.glob("*.json"))) == 0
    assert pre_queue_calls == []


def test_host_action_queue_rejects_same_action_with_different_account(
    monkeypatch, tmp_path,
):
    jobs = tmp_path / "jobs"
    jobs.mkdir(parents=True)
    generation = _seed_control_panel_runtime(monkeypatch, tmp_path)
    (jobs / "existing-repair.processing").write_text(
        json.dumps({
            "job_id": "existing-repair",
            "job_type": "tenant_action",
            "action": "repair_whatsapp_allowlist",
            "slug": "acme",
            "zernio_account_id": "account-old",
            "allowlist_note": "verified",
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = queue_tenant_host_action(
        slug="acme",
        action="repair_whatsapp_allowlist",
        zernio_account_id="account-new",
        allowlist_note="verified",
    )

    assert result.status == "failed"
    assert result.job_id == "existing-repair"
    assert "different host job" in result.message


def test_host_action_before_queue_failure_publishes_no_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    def fail_before_publish():
        raise RuntimeError("provider cleanup failed")

    with pytest.raises(RuntimeError, match="provider cleanup failed"):
        queue_tenant_host_action(
            slug="acme",
            action="delete_tenant",
            typed_slug="acme",
            final_confirmation="DELETE FOREVER",
            requested_job_id="delete-acme-operation-2",
            delete_operation_id=DELETE_OPERATION_ID,
            generation_fingerprint=DELETE_GENERATION,
            prepared_backup_path="/root/_deleted_tenants/acme-prepared",
            prepared_backup_digest=DELETE_BACKUP_DIGEST,
            before_queue=fail_before_publish,
        )

    assert list(jobs.iterdir()) == []


def test_awaited_host_action_handles_non_object_result(monkeypatch, tmp_path):
    from app import provisioning

    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "1")
    _seed_control_panel_runtime(monkeypatch, tmp_path)
    original_write = provisioning._write_private_json

    def write_job_and_result(path, payload):
        original_write(path, payload)
        if payload.get("job_type") == "tenant_action":
            results.mkdir(parents=True, exist_ok=True)
            (results / f"{payload['job_id']}.json").write_text(
                "[]\n", encoding="utf-8"
            )

    monkeypatch.setattr(provisioning, "_write_private_json", write_job_and_result)

    result = queue_tenant_host_action(slug="acme", action="restart_tenant")

    assert result.status == "failed"
    assert result.job_id
    assert "malformed result" in result.message


def test_host_worker_keeps_nginx_backups_outside_sites_enabled():
    worker_source = Path("host/nr3_provision_worker.py").read_text()
    service_source = Path("host/nr3-provision-worker.service").read_text()

    assert "NGINX_BACKUP_DIR" in worker_source
    assert "Never place backups inside sites-enabled" in worker_source
    assert "NGINX_SITE.with_name" not in worker_source
    assert "job_type" in worker_source
    assert "suspend_tenant" in worker_source
    assert "unpause_tenant" in worker_source
    assert "restart_tenant" in worker_source
    assert "restore_tenant_runtime" in worker_source
    assert "delete_tenant" in worker_source
    assert "tenant folder was already missing" in worker_source
    assert "client-missing.txt" in worker_source
    assert "DELETED_TENANTS_ROOT" in worker_source
    assert "remove_nginx_block" in worker_source
    assert "rollback_failed_provision" in worker_source
    assert '"job_type": "tenant_action"' in worker_source
    assert '"docker-compose.yml"' in worker_source
    assert '"--remove-orphans"' in worker_source
    assert "NR3_PROVISION_NGINX_BACKUP_DIR=/root/nginx-sites-enabled-backups" in service_source
    assert "mode=0o600" in worker_source
    assert "os.fsync" in worker_source


def test_reconcile_host_action_results_cleans_async_delete_state(monkeypatch, tmp_path):
    from app import channel_connections
    from app.tenants import register_tenant

    registry = tmp_path / "registry.json"
    results = tmp_path / "results"
    reconciled = tmp_path / "reconciled"
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(reconciled))
    monkeypatch.setenv(
        "NR3_DELETE_OPERATIONS_DIR", str(tmp_path / "delete-operations")
    )
    tenant_root = tmp_path / "tenant-root"
    tenant_root.mkdir()
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenant_root))

    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    channel_connections.set_tenant_zernio_profile_id(
        tenant_id="lawyer",
        name="Lawyer",
        zernio_profile_id="profile_lawyer",
    )
    results.mkdir(parents=True)
    (results / "job_delete.json").write_text(
        json.dumps(
            {
                "job_id": "job_delete",
                "status": "succeeded",
                "job_type": "tenant_action",
                "action": "delete_tenant",
                "slug": "lawyer",
            }
        ),
        encoding="utf-8",
    )

    # An unbound legacy result cannot authorize control-plane cleanup.
    assert reconcile_host_action_results() == 0
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") == "profile_lawyer"
    assert not (reconciled / "job_delete.done").exists()
    assert reconcile_host_action_results() == 0


def test_reconcile_failed_async_provision_cleans_only_matching_claim(
    monkeypatch, tmp_path,
):
    from app import icp_overrides
    from app.port_registry import read_port_registry, reserve_tenant_port
    from app.tenants import register_tenant

    registry = tmp_path / "registry.json"
    results = tmp_path / "results"
    reconciled = tmp_path / "reconciled"
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "overrides.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(reconciled))
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(tmp_path / "claims.json"))
    monkeypatch.setenv("NR3_TENANT_CREATE_LOCK_DIR", str(tmp_path / "create-locks"))

    register_tenant({"slug": "lawyer", "name": "Lawyer", "status": "active"})
    reserve_tenant_port("lawyer")
    icp_overrides.initialize_new_tenant_fail_closed("lawyer")
    assert create_tenant_provision_claim("lawyer", "creation-current") is True
    results.mkdir(parents=True)
    (results / "job_failed.json").write_text(
        json.dumps({
            "job_id": "job_failed",
            "status": "failed",
            "job_type": "tenant_provision",
            "slug": "lawyer",
            "creation_id": "creation-current",
            "safe_to_release": True,
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    stored = json.loads(registry.read_text())
    assert "lawyer" not in stored.get("tenants", {})
    assert "lawyer" not in read_port_registry()
    assert tenant_provision_claim("lawyer") is None
    assert reconcile_host_action_results() == 0


def test_reconcile_stale_provision_result_cannot_delete_new_owner(
    monkeypatch, tmp_path,
):
    from app.port_registry import read_port_registry, reserve_tenant_port
    from app.tenants import register_tenant

    registry = tmp_path / "registry.json"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(tmp_path / "reconciled"))
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(tmp_path / "claims.json"))
    monkeypatch.setenv("NR3_TENANT_CREATE_LOCK_DIR", str(tmp_path / "create-locks"))

    register_tenant({"slug": "lawyer", "name": "New Owner", "status": "active"})
    reserve_tenant_port("lawyer")
    assert create_tenant_provision_claim("lawyer", "creation-new") is True
    results.mkdir(parents=True)
    (results / "job_stale.json").write_text(
        json.dumps({
            "job_id": "job_stale",
            "status": "failed",
            "job_type": "tenant_provision",
            "slug": "lawyer",
            "creation_id": "creation-old",
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    assert "lawyer" in json.loads(registry.read_text())["tenants"]
    assert "lawyer" in read_port_registry()
    assert tenant_provision_claim("lawyer")["creation_id"] == "creation-new"


def _seed_restore_reconcile_claim(monkeypatch, tmp_path, *, creation_id, job_id):
    from app.port_registry import reserve_tenant_port
    from app.tenants import register_tenant

    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "registry.json"))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(tmp_path / "reconciled"))
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(tmp_path / "claims.json"))
    monkeypatch.setenv("NR3_TENANT_CREATE_LOCK_DIR", str(tmp_path / "create-locks"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    register_tenant({"slug": "clone", "name": "Clone", "status": "active"})
    reserve_tenant_port("clone")
    assert create_tenant_provision_claim("clone", creation_id) is True
    assert update_tenant_provision_claim_job("clone", creation_id, job_id) is True
    (tmp_path / "results").mkdir()


def test_reconcile_async_restore_success_clears_matching_clone_claim(
    monkeypatch, tmp_path,
):
    creation_id = "clone-creation-success"
    job_id = "restore-clone-success"
    _seed_restore_reconcile_claim(
        monkeypatch,
        tmp_path,
        creation_id=creation_id,
        job_id=job_id,
    )
    (tmp_path / "results" / f"{job_id}.json").write_text(
        json.dumps({
            "job_id": job_id,
            "status": "succeeded",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "clone",
            "creation_id": creation_id,
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    assert tenant_provision_claim("clone") is None


def test_reconcile_async_restore_unsafe_failure_retains_clone_claim(
    monkeypatch, tmp_path,
):
    creation_id = "clone-creation-unsafe"
    job_id = "restore-clone-unsafe"
    _seed_restore_reconcile_claim(
        monkeypatch,
        tmp_path,
        creation_id=creation_id,
        job_id=job_id,
    )
    (tmp_path / "results" / f"{job_id}.json").write_text(
        json.dumps({
            "job_id": job_id,
            "status": "failed",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "clone",
            "creation_id": creation_id,
            "safe_to_release": False,
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    claim = tenant_provision_claim("clone")
    assert claim is not None
    assert claim["creation_id"] == creation_id
    assert claim["job_id"] == job_id


def test_reconcile_async_restore_stale_identity_cannot_clear_new_clone_claim(
    monkeypatch, tmp_path,
):
    _seed_restore_reconcile_claim(
        monkeypatch,
        tmp_path,
        creation_id="clone-creation-new",
        job_id="restore-clone-new",
    )
    (tmp_path / "results" / "restore-clone-old.json").write_text(
        json.dumps({
            "job_id": "restore-clone-old",
            "status": "succeeded",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "clone",
            "creation_id": "clone-creation-old",
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    claim = tenant_provision_claim("clone")
    assert claim is not None
    assert claim["creation_id"] == "clone-creation-new"
    assert claim["job_id"] == "restore-clone-new"


def _configure_worker_sandbox(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    paths = {
        "queue": tmp_path / "jobs",
        "results": tmp_path / "results",
        "failed": tmp_path / "failed",
        "clients": tmp_path / "clients",
        "deleted": tmp_path / "deleted",
        "tokens": tmp_path / "tokens",
        "nginx_backups": tmp_path / "nginx-backups",
        "nginx": tmp_path / "api-unboks",
        "icp": tmp_path / "missing-icp-data",
    }
    paths["queue"].mkdir()
    paths["nginx"].write_text(
        "server {\n    server_name api.unboks.org;\n}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(worker, "QUEUE_DIR", paths["queue"])
    monkeypatch.setattr(worker, "RESULT_DIR", paths["results"])
    monkeypatch.setattr(worker, "FAILED_DIR", paths["failed"])
    monkeypatch.setattr(worker, "CLIENTS_ROOT", paths["clients"])
    monkeypatch.setattr(worker, "DELETED_TENANTS_ROOT", paths["deleted"])
    monkeypatch.setattr(worker, "BRIDGE_TOKEN_DIR", paths["tokens"])
    monkeypatch.setattr(worker, "NGINX_BACKUP_DIR", paths["nginx_backups"])
    monkeypatch.setattr(worker, "NGINX_SITE", paths["nginx"])
    monkeypatch.setattr(worker, "ICP_DATA_DIR", paths["icp"])
    return worker, paths


def _completed(cmd, returncode=0, stdout=""):
    return subprocess.CompletedProcess(cmd, returncode, stdout)


def test_failed_provision_releases_only_with_exact_absence_proof(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    monkeypatch.setattr(
        worker,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, stdout="")
        if cmd[:3] == ["docker", "ps", "-a"]
        else _completed(cmd),
    )
    job = paths["queue"] / "invalid-route.json"
    job.write_text(
        json.dumps({
            "job_id": "invalid-route",
            "job_type": "tenant_provision",
            "creation_id": "creation-acme",
            "slug": "acme",
            "host_port": 8123,
            "client_data": {"slug": "acme", "password": "temporary-password"},
            "docker_compose_text": worker.canonical_docker_compose_text(
                "acme", 8123
            ),
            "managed_nginx_block_text": "not a managed route",
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "invalid-route.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is True
    assert any("docker ps -a confirmed exact container" in item for item in result["details"])
    assert any("nginx tenant route markers are absent" in item for item in result["details"])


def test_existing_directory_collision_is_never_safe_to_release(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    (paths["clients"] / "acme").mkdir(parents=True)
    monkeypatch.setattr(
        worker,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, stdout=""),
    )
    monkeypatch.setattr(
        worker,
        "rotate_tenant_bridge_token",
        lambda _slug: (_ for _ in ()).throw(AssertionError("must not rotate")),
    )
    job = paths["queue"] / "collision.json"
    job.write_text(
        json.dumps({
            "job_id": "collision",
            "job_type": "tenant_provision",
            "creation_id": "creation-acme",
            "slug": "acme",
            "host_port": 8123,
            "client_data": {"slug": "acme", "password": "temporary-password"},
            "docker_compose_text": worker.canonical_docker_compose_text(
                "acme", 8123
            ),
            "managed_nginx_block_text": (
                worker.canonical_managed_nginx_block_text("acme", 8123)
            ),
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "collision.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert (paths["failed"] / "collision.processing").exists()


def test_rollback_preserves_everything_when_container_absence_is_uncertain(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "docker-compose.yml").write_text("services: {}\n")
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48)
    route = worker.canonical_managed_nginx_block_text("acme", 8123)
    paths["nginx"].write_text(paths["nginx"].read_text() + route)

    def uncertain_run(cmd, **_kwargs):
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, returncode=1, stdout="daemon unavailable")
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", uncertain_run)
    details = []

    assert worker.rollback_failed_provision("acme", tenant_dir, details) is False
    assert tenant_dir.exists()
    assert token_path.exists()
    assert "# BEGIN UNBOKS TENANT acme" in paths["nginx"].read_text()


def test_rollback_returns_proof_and_removes_token_after_teardown(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "docker-compose.yml").write_text("services: {}\n")
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48)
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123)
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda cmd, **_kwargs: _completed(cmd, stdout=""),
    )
    details = []

    assert worker.rollback_failed_provision("acme", tenant_dir, details) is True
    assert not tenant_dir.exists()
    assert not token_path.exists()
    assert "# BEGIN UNBOKS TENANT acme" not in paths["nginx"].read_text()


def test_rollback_retry_requires_successful_nginx_reload_before_safe_release(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48, encoding="utf-8")
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123),
        encoding="utf-8",
    )
    reload_calls = 0

    def fail_first_reload(cmd, **_kwargs):
        nonlocal reload_calls
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="")
        if cmd == ["systemctl", "reload", "nginx"]:
            reload_calls += 1
            if reload_calls == 1:
                raise subprocess.CalledProcessError(1, cmd, output="reload failed")
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", fail_first_reload)
    first_details = []

    assert worker.rollback_failed_provision("acme", tenant_dir, first_details) is False
    assert reload_calls == 1
    assert not tenant_dir.exists()
    assert token_path.exists()
    assert "# BEGIN UNBOKS TENANT acme" not in paths["nginx"].read_text()

    second_details = []
    assert worker.rollback_failed_provision("acme", tenant_dir, second_details) is True
    assert reload_calls == 2
    assert not token_path.exists()
    assert any("validated and reloaded" in item for item in second_details)


def test_delete_keeps_files_route_and_token_without_container_proof(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "docker-compose.yml").write_text("services: {}\n")
    generation = _seed_delete_generation(worker, tenant_dir)
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48)
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123)
    )

    def uncertain_run(cmd, **_kwargs):
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, returncode=1)
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", uncertain_run)
    job = paths["queue"] / "delete-acme.json"
    job.write_text(
        json.dumps({
            "job_id": "delete-acme",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "delete-acme.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert tenant_dir.exists()
    assert token_path.exists()
    assert "# BEGIN UNBOKS TENANT acme" in paths["nginx"].read_text()


def test_prepare_delete_only_creates_and_verifies_backup(monkeypatch, tmp_path):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "runtime.txt").write_text("live\n")
    generation = _seed_delete_generation(worker, tenant_dir)
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48)
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123)
    )
    commands = []

    def prove_absent(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] != ["docker", "ps", "-a"]:
            raise AssertionError(f"unexpected preparation command: {cmd}")
        return _completed(cmd, stdout="")

    monkeypatch.setattr(worker, "run", prove_absent)
    job = paths["queue"] / "prepare-acme.json"
    job.write_text(
        json.dumps({
            "job_id": "prepare-acme",
            "job_type": "tenant_action",
            "action": "prepare_delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "prepare-acme.json").read_text())
    assert result["status"] == "succeeded"
    assert result["delete_operation_id"] == DELETE_OPERATION_ID
    assert result["generation_fingerprint"] == generation
    assert result["backup_digest"].startswith("sha256:")
    assert result["safe_to_release"] is False
    backup_path = Path(result["backup_path"])
    assert (backup_path / "client" / "runtime.txt").read_text() == "live\n"
    assert tenant_dir.exists()
    assert token_path.exists()
    assert "# BEGIN UNBOKS TENANT acme" in paths["nginx"].read_text()
    assert commands
    assert all(cmd[:3] == ["docker", "ps", "-a"] for cmd in commands)


def test_prepare_delete_fails_closed_when_snapshot_file_fsync_fails(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "runtime.txt").write_text("must remain live\n", encoding="utf-8")
    generation = _seed_delete_generation(worker, tenant_dir)
    commands = []

    def absent_container(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] != ["docker", "ps", "-a"]:
            raise AssertionError(f"backup durability failure mutated Docker: {cmd}")
        return _completed(cmd, stdout="")

    original_fsync_file = worker._fsync_regular_file_required

    def fail_runtime_fsync(path):
        if Path(path).name == "runtime.txt":
            raise OSError("forced snapshot file fsync failure")
        original_fsync_file(path)

    monkeypatch.setattr(worker, "run", absent_container)
    monkeypatch.setattr(worker, "_fsync_regular_file_required", fail_runtime_fsync)
    (paths["queue"] / "prepare-fsync-failure.json").write_text(
        json.dumps({
            "job_id": "prepare-fsync-failure",
            "job_type": "tenant_action",
            "action": "prepare_delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / "prepare-fsync-failure.json").read_text()
    )
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert "forced snapshot file fsync failure" in result["message"]
    assert (tenant_dir / "runtime.txt").read_text() == "must remain live\n"
    assert not [
        path for path in paths["deleted"].iterdir()
        if path.is_dir() and not path.name.startswith(".")
    ]
    assert commands
    assert all(cmd[:3] == ["docker", "ps", "-a"] for cmd in commands)


def test_prepare_delete_quiesces_sqlite_without_copying_global_icp_data(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    tenant_db = tenant_dir / "data" / "runtime.db"
    tenant_db.parent.mkdir(parents=True)
    tenant_connection = sqlite3.connect(tenant_db)
    tenant_connection.execute("PRAGMA journal_mode=WAL")
    tenant_connection.execute("PRAGMA wal_autocheckpoint=0")
    tenant_connection.execute("CREATE TABLE messages (body TEXT)")
    tenant_connection.execute("INSERT INTO messages VALUES ('recover me')")
    tenant_connection.commit()
    paths["icp"].mkdir()
    with sqlite3.connect(paths["icp"] / "control.db") as control_db:
        control_db.execute("CREATE TABLE tenants (slug TEXT, secret TEXT)")
        control_db.execute(
            "INSERT INTO tenants VALUES ('roberto', 'other-customer-secret')"
        )
        control_db.execute(
            "INSERT INTO tenants VALUES ('ali', 'another-customer-secret')"
        )
    (paths["icp"] / "global.json").write_text(
        json.dumps({"roberto": "private", "ali": "private"}),
        encoding="utf-8",
    )

    container = {"running": True}
    commands = []

    def docker_state(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="wtyj-acme\n")
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return _completed(cmd, stdout="true\n" if container["running"] else "false\n")
        if cmd[:2] == ["docker", "stop"]:
            container["running"] = False
            return _completed(cmd)
        if cmd[:2] == ["docker", "start"]:
            container["running"] = True
            return _completed(cmd)
        raise AssertionError(f"unexpected preparation command: {cmd}")

    monkeypatch.setattr(worker, "run", docker_state)
    job = paths["queue"] / "prepare-sqlite.json"
    job.write_text(
        json.dumps({
            "job_id": "prepare-sqlite",
            "job_type": "tenant_action",
            "action": "prepare_delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    try:
        worker.run_once()
    finally:
        tenant_connection.close()

    result = json.loads((paths["results"] / "prepare-sqlite.json").read_text())
    assert result["status"] == "succeeded", json.dumps(result, indent=2)
    assert container["running"] is True
    assert [cmd[:2] for cmd in commands].count(["docker", "stop"]) == 1
    assert [cmd[:2] for cmd in commands].count(["docker", "start"]) == 1
    backup = Path(result["backup_path"])
    with sqlite3.connect(backup / "client" / "data" / "runtime.db") as copied:
        assert copied.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert copied.execute("SELECT body FROM messages").fetchone() == ("recover me",)
    assert not (backup / "icp-data").exists()
    assert not list(backup.rglob("control.db"))
    assert not list(backup.rglob("global.json"))
    assert not list(backup.rglob("*-wal"))
    assert not list(backup.rglob("*-shm"))
    manifest = json.loads((backup / "DELETE_MANIFEST.json").read_text())
    assert manifest["recovery_scope"] == "tenant_runtime_only"
    assert manifest["control_panel_data_included"] is False
    assert manifest["inventory"]
    assert manifest["inventory_digest"].startswith("sha256:")


def test_delete_backup_verifier_rejects_embedded_global_control_panel_data(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    backup = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    (backup / "icp-data").mkdir()
    (backup / "icp-data" / "other-tenant.json").write_text(
        json.dumps({"slug": "roberto", "secret": "must-not-be-bundled"}),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="global control-panel data"):
        worker.verify_delete_backup(
            "acme",
            tenant_dir,
            backup,
            delete_operation_id=DELETE_OPERATION_ID,
            generation_fingerprint=generation,
        )


def test_prepare_delete_restarts_container_when_critical_json_is_invalid(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "data").mkdir()
    (tenant_dir / "data" / "broken.json").write_text("{not-json")
    container = {"running": True}
    commands = []

    def docker_state(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="wtyj-acme\n")
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return _completed(cmd, stdout="true\n" if container["running"] else "false\n")
        if cmd[:2] == ["docker", "stop"]:
            container["running"] = False
            return _completed(cmd)
        if cmd[:2] == ["docker", "start"]:
            container["running"] = True
            return _completed(cmd)
        raise AssertionError(f"unexpected preparation command: {cmd}")

    monkeypatch.setattr(worker, "run", docker_state)
    job = paths["queue"] / "prepare-invalid-json.json"
    job.write_text(
        json.dumps({
            "job_id": "prepare-invalid-json",
            "job_type": "tenant_action",
            "action": "prepare_delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / "prepare-invalid-json.json").read_text()
    )
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert "Critical JSON" in result["message"]
    assert container["running"] is True
    assert [cmd[:2] for cmd in commands].count(["docker", "stop"]) == 1
    assert [cmd[:2] for cmd in commands].count(["docker", "start"]) == 1
    assert not list(paths["deleted"].glob(".nr3-delete-prepare-acme-*.json"))


def test_prepare_delete_new_attempt_recovers_prior_transient_restart_failure(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "runtime.txt").write_text("recoverable\n", encoding="utf-8")
    container = {"running": True}
    start_calls = 0

    def docker_state(cmd, **_kwargs):
        nonlocal start_calls
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="wtyj-acme\n")
        if cmd[:3] == ["docker", "inspect", "--format"]:
            return _completed(
                cmd,
                stdout="true\n" if container["running"] else "false\n",
            )
        if cmd[:2] == ["docker", "stop"]:
            container["running"] = False
            return _completed(cmd)
        if cmd[:2] == ["docker", "start"]:
            start_calls += 1
            # The first preparation cannot restore its originally running
            # container. The next job must adopt that recovery state first.
            if start_calls > 1:
                container["running"] = True
            return _completed(cmd, returncode=0 if start_calls > 1 else 1)
        raise AssertionError(f"unexpected preparation command: {cmd}")

    monkeypatch.setattr(worker, "run", docker_state)

    def queue_prepare(job_id):
        (paths["queue"] / f"{job_id}.json").write_text(
            json.dumps({
                "job_id": job_id,
                "job_type": "tenant_action",
                "action": "prepare_delete_tenant",
                "slug": "acme",
                "typed_slug": "acme",
                "final_confirmation": "DELETE FOREVER",
                "delete_operation_id": DELETE_OPERATION_ID,
                "generation_fingerprint": generation,
            }),
            encoding="utf-8",
        )

    queue_prepare("prepare-first")
    worker.run_once()
    first = json.loads((paths["results"] / "prepare-first.json").read_text())
    assert first["status"] == "failed"
    assert first["safe_to_release"] is False
    assert container["running"] is False
    assert len(list(paths["deleted"].glob(".nr3-delete-prepare-acme-*.json"))) == 1

    queue_prepare("prepare-second")
    worker.run_once()
    second = json.loads((paths["results"] / "prepare-second.json").read_text())
    assert second["status"] == "succeeded", json.dumps(second, indent=2)
    assert container["running"] is True
    assert start_calls == 3
    assert not list(paths["deleted"].glob(".nr3-delete-prepare-acme-*.json"))
    assert any("recovered and retired" in item for item in second["details"])


def test_final_delete_rejects_tampered_prepared_backup_before_host_mutation(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "runtime.txt").write_text("live\n")
    generation = _seed_delete_generation(worker, tenant_dir)
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    (prepared / "client" / "runtime.txt").write_text("tampered\n")
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("tampered preparation must fail before host commands")
        ),
    )
    job = paths["queue"] / "delete-tampered.json"
    job.write_text(
        json.dumps({
            "job_id": "delete-tampered",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "delete-tampered.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert result["delete_operation_id"] == DELETE_OPERATION_ID
    assert result["generation_fingerprint"] == generation
    assert result["prepared_backup_digest"] == prepared_digest
    assert tenant_dir.is_dir()


def test_final_delete_uses_bound_backup_then_removes_runtime_and_token(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "docker-compose.yml").write_text(
        worker.canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    (tenant_dir / "runtime.txt").write_text("prepared version\n", encoding="utf-8")
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    # The tenant is live again after preparation and can change before the
    # provider cleanup completes. Final deletion must capture this newer data.
    (tenant_dir / "runtime.txt").write_text("final version\n", encoding="utf-8")
    paths["tokens"].mkdir()
    (paths["tokens"] / "acme").write_text("t" * 48)
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123)
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda cmd, **_kwargs: _completed(
            cmd,
            stdout="" if cmd[:3] == ["docker", "ps", "-a"] else "",
        ),
    )
    fsynced_directories = []
    original_fsync_directory = worker._fsync_directory_required

    def record_directory_fsync(path):
        fsynced_directories.append(Path(path))
        original_fsync_directory(path)

    monkeypatch.setattr(
        worker,
        "_fsync_directory_required",
        record_directory_fsync,
    )
    job = paths["queue"] / "delete-success.json"
    job.write_text(
        json.dumps({
            "job_id": "delete-success",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "delete-success.json").read_text())
    assert result["status"] == "succeeded"
    assert result["safe_to_release"] is True
    assert result["prepared_backup_digest"] == prepared_digest
    assert result["prepared_backup_path"] == str(prepared)
    assert result["backup_digest"].startswith("sha256:")
    assert result["backup_path"] != str(prepared)
    defensive = Path(result["backup_path"])
    assert (prepared / "client" / "runtime.txt").read_text() == "prepared version\n"
    assert (defensive / "client" / "runtime.txt").read_text() == "final version\n"
    manifest = json.loads((defensive / "DELETE_MANIFEST.json").read_text())
    assert manifest["backup_role"] == "defensive"
    assert not tenant_dir.exists()
    assert paths["clients"] in fsynced_directories
    assert not (paths["tokens"] / "acme").exists()
    assert "# BEGIN UNBOKS TENANT acme" not in paths["nginx"].read_text()


def test_final_delete_fails_closed_when_backup_publication_fsync_fails(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "runtime.txt").write_text("latest durable data\n", encoding="utf-8")
    generation = _seed_delete_generation(worker, tenant_dir)
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    published_before = {
        path.name
        for path in paths["deleted"].iterdir()
        if path.is_dir() and not path.name.startswith(".")
    }
    commands = []

    def absent_container(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] != ["docker", "ps", "-a"]:
            raise AssertionError(f"backup publication failure mutated Docker: {cmd}")
        return _completed(cmd, stdout="")

    original_fsync_directory = worker._fsync_directory_required
    publication_failure_injected = False

    def fail_new_backup_publication(path):
        nonlocal publication_failure_injected
        public_now = {
            item.name
            for item in paths["deleted"].iterdir()
            if item.is_dir() and not item.name.startswith(".")
        }
        if (
            Path(path) == paths["deleted"]
            and public_now != published_before
            and not publication_failure_injected
        ):
            publication_failure_injected = True
            raise OSError("forced backup root fsync failure")
        original_fsync_directory(path)

    monkeypatch.setattr(worker, "run", absent_container)
    monkeypatch.setattr(
        worker,
        "_fsync_directory_required",
        fail_new_backup_publication,
    )
    (paths["queue"] / "delete-fsync-failure.json").write_text(
        json.dumps({
            "job_id": "delete-fsync-failure",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / "delete-fsync-failure.json").read_text()
    )
    assert publication_failure_injected is True
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert "forced backup root fsync failure" in result["message"]
    assert (tenant_dir / "runtime.txt").read_text() == "latest durable data\n"
    assert not any(cmd[:2] == ["docker", "rm"] for cmd in commands)


def test_final_delete_retry_reloads_nginx_after_first_reload_failure(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "runtime.txt").write_text("latest data\n", encoding="utf-8")
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    paths["tokens"].mkdir()
    token_path = paths["tokens"] / "acme"
    token_path.write_text("t" * 48, encoding="utf-8")
    paths["nginx"].write_text(
        paths["nginx"].read_text()
        + worker.canonical_managed_nginx_block_text("acme", 8123),
        encoding="utf-8",
    )
    reload_calls = 0

    def fail_first_reload(cmd, **_kwargs):
        nonlocal reload_calls
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="")
        if cmd == ["systemctl", "reload", "nginx"]:
            reload_calls += 1
            if reload_calls == 1:
                raise subprocess.CalledProcessError(1, cmd, output="reload failed")
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", fail_first_reload)

    def queue_final(job_id):
        (paths["queue"] / f"{job_id}.json").write_text(
            json.dumps({
                "job_id": job_id,
                "job_type": "tenant_action",
                "action": "delete_tenant",
                "slug": "acme",
                "typed_slug": "acme",
                "final_confirmation": "DELETE FOREVER",
                "delete_operation_id": DELETE_OPERATION_ID,
                "generation_fingerprint": generation,
                "prepared_backup_path": str(prepared),
                "prepared_backup_digest": prepared_digest,
            }),
            encoding="utf-8",
        )

    queue_final("delete-first")
    worker.run_once()
    first = json.loads((paths["results"] / "delete-first.json").read_text())
    assert first["status"] == "failed"
    assert first["safe_to_release"] is False
    assert reload_calls == 1
    assert "# BEGIN UNBOKS TENANT acme" not in paths["nginx"].read_text()
    assert token_path.exists()
    assert list(paths["deleted"].glob(".nr3-delete-final-acme-*.json"))

    queue_final("delete-second")
    worker.run_once()
    second = json.loads((paths["results"] / "delete-second.json").read_text())
    assert second["status"] == "succeeded", json.dumps(second, indent=2)
    assert second["safe_to_release"] is True
    assert second["prepared_backup_digest"] == prepared_digest
    assert reload_calls == 2
    assert not token_path.exists()
    assert not list(paths["deleted"].glob(".nr3-delete-final-acme-*.json"))


def test_final_delete_retry_never_removes_recreated_tenant_generation(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    old_generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "runtime.txt").write_text("old generation\n", encoding="utf-8")
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=old_generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    defensive = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=old_generation,
        backup_role="defensive",
    )
    defensive_digest = worker.delete_backup_digest(defensive)
    paths["deleted"].mkdir(exist_ok=True)
    final_state_path = worker._delete_final_state_path("acme", DELETE_OPERATION_ID)
    worker._write_delete_final_state(final_state_path, {
        "version": 1,
        "job_id": "delete-old",
        "slug": "acme",
        "delete_operation_id": DELETE_OPERATION_ID,
        "generation_fingerprint": old_generation,
        "prepared_backup_path": str(prepared),
        "prepared_backup_digest": prepared_digest,
        "original_container_state": "absent",
        "defensive_backup_path": str(defensive),
        "defensive_backup_digest": defensive_digest,
        "teardown_proven": True,
        "created_at": worker.utc_now(),
    })
    (tenant_dir / "config" / "client.json").write_text(
        json.dumps({"slug": "acme", "creation_id": "creation-new"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("recreated generation must fail before host commands")
        ),
    )
    (paths["queue"] / "delete-stale-retry.json").write_text(
        json.dumps({
            "job_id": "delete-stale-retry",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": old_generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / "delete-stale-retry.json").read_text()
    )
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert "generation changed" in result["message"]
    assert (tenant_dir / "runtime.txt").read_text() == "old generation\n"


def test_final_delete_recovery_verifies_generation_before_docker_quiesce(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    old_generation = _seed_delete_generation(worker, tenant_dir)
    (tenant_dir / "runtime.txt").write_text("replacement data\n", encoding="utf-8")
    prepared = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=old_generation,
    )
    prepared_digest = worker.delete_backup_digest(prepared)
    defensive = worker.backup_tenant_before_delete(
        "acme",
        tenant_dir,
        delete_operation_id=DELETE_OPERATION_ID,
        generation_fingerprint=old_generation,
        backup_role="defensive",
    )
    defensive_digest = worker.delete_backup_digest(defensive)
    final_state_path = worker._delete_final_state_path("acme", DELETE_OPERATION_ID)
    worker._write_delete_final_state(final_state_path, {
        "version": 1,
        "job_id": "delete-old",
        "slug": "acme",
        "delete_operation_id": DELETE_OPERATION_ID,
        "generation_fingerprint": old_generation,
        "prepared_backup_path": str(prepared),
        "prepared_backup_digest": prepared_digest,
        "original_container_state": "running",
        "defensive_backup_path": str(defensive),
        "defensive_backup_digest": defensive_digest,
        "teardown_proven": False,
        "created_at": worker.utc_now(),
    })
    (tenant_dir / "config" / "client.json").write_text(
        json.dumps({"slug": "acme", "creation_id": "creation-new"}),
        encoding="utf-8",
    )
    commands = []

    def reject_any_docker(cmd, **_kwargs):
        commands.append(cmd)
        raise AssertionError(f"stale final recovery touched Docker: {cmd}")

    monkeypatch.setattr(worker, "run", reject_any_docker)
    (paths["queue"] / "delete-stale-quiesce.json").write_text(
        json.dumps({
            "job_id": "delete-stale-quiesce",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "typed_slug": "acme",
            "final_confirmation": "DELETE FOREVER",
            "delete_operation_id": DELETE_OPERATION_ID,
            "generation_fingerprint": old_generation,
            "prepared_backup_path": str(prepared),
            "prepared_backup_digest": prepared_digest,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / "delete-stale-quiesce.json").read_text()
    )
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert "generation changed" in result["message"]
    assert commands == []
    assert (tenant_dir / "runtime.txt").read_text() == "replacement data\n"
    assert final_state_path.exists()


def test_orphan_processing_job_with_matching_terminal_result_is_discarded(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    job = {
        "job_id": "restart-acme",
        "job_type": "tenant_action",
        "action": "restart_tenant",
        "slug": "acme",
    }
    processing = paths["queue"] / "restart-acme.processing"
    processing.write_text(json.dumps(job), encoding="utf-8")
    paths["results"].mkdir()
    terminal = {
        **job,
        "status": "succeeded",
        "message": "already complete",
        "job_payload_digest": worker.job_payload_digest(job),
    }
    (paths["results"] / "restart-acme.json").write_text(json.dumps(terminal))
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal orphan must not be reprocessed")
        ),
    )

    worker.run_once()

    assert not processing.exists()
    assert json.loads((paths["results"] / "restart-acme.json").read_text()) == terminal


def test_worker_singleton_lock_blocks_duplicate_processing(monkeypatch, tmp_path):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    processing = paths["queue"] / "duplicate.processing"
    processing.write_text(
        json.dumps({
            "job_id": "duplicate",
            "job_type": "tenant_action",
            "action": "restart_tenant",
            "slug": "acme",
            "generation_fingerprint": DELETE_GENERATION,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        worker,
        "process_job",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("second worker must not process the claimed job")
        ),
    )

    with worker.worker_execution_lock():
        with pytest.raises(worker.HostActionFailure, match="already owns queue"):
            worker.run_once()

    assert processing.exists()


@pytest.mark.parametrize(
    "action",
    ["restart_tenant", "suspend_tenant", "unpause_tenant"],
)
def test_host_worker_rejects_stale_generation_lifecycle_action_before_mutation(
    monkeypatch, tmp_path, action,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    _seed_delete_generation(worker, tenant_dir)
    original_client = (tenant_dir / "config" / "client.json").read_text()
    stale_dir = tmp_path / "stale-acme"
    stale_dir.mkdir()
    stale_generation = _seed_delete_generation(worker, stale_dir)
    stale_client_path = stale_dir / "config" / "client.json"
    stale_client = json.loads(stale_client_path.read_text())
    stale_client["creation_id"] = "creation-prior"
    stale_client_path.write_text(json.dumps(stale_client), encoding="utf-8")
    stale_generation = worker.tenant_generation_fingerprint("acme", stale_dir)
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale lifecycle job must not invoke Docker")
        ),
    )
    job_id = f"stale-{action}"
    (paths["queue"] / f"{job_id}.json").write_text(
        json.dumps({
            "job_id": job_id,
            "job_type": "tenant_action",
            "action": action,
            "slug": "acme",
            "generation_fingerprint": stale_generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / f"{job_id}.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert result["generation_fingerprint"] == stale_generation
    assert "generation changed" in result["message"]
    assert (tenant_dir / "config" / "client.json").read_text() == original_client


def test_host_worker_requires_and_echoes_generation_for_restart(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    generation = _seed_delete_generation(worker, tenant_dir)
    commands = []
    monkeypatch.setattr(
        worker,
        "run",
        lambda cmd, **_kwargs: commands.append(cmd) or _completed(cmd),
    )
    missing = {
        "job_id": "restart-missing-generation",
        "job_type": "tenant_action",
        "action": "restart_tenant",
        "slug": "acme",
    }
    (paths["queue"] / "restart-missing-generation.json").write_text(
        json.dumps(missing), encoding="utf-8"
    )

    worker.run_once()

    rejected = json.loads(
        (paths["results"] / "restart-missing-generation.json").read_text()
    )
    assert rejected["status"] == "failed"
    assert rejected["safe_to_release"] is False
    assert "generation fingerprint is missing" in rejected["message"]
    assert commands == []

    valid = {
        **missing,
        "job_id": "restart-current-generation",
        "generation_fingerprint": generation,
    }
    (paths["queue"] / "restart-current-generation.json").write_text(
        json.dumps(valid), encoding="utf-8"
    )
    worker.run_once()

    succeeded = json.loads(
        (paths["results"] / "restart-current-generation.json").read_text()
    )
    assert succeeded["status"] == "succeeded"
    assert succeeded["generation_fingerprint"] == generation
    assert commands == [["docker", "compose", "up", "-d", "--force-recreate"]]


def test_orphan_action_with_mismatched_result_is_reprocessed(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme"
    tenant_dir.mkdir(parents=True)
    (tenant_dir / "runtime.txt").write_text("live\n")
    generation = _seed_delete_generation(worker, tenant_dir)
    job = {
        "job_id": "prepare-acme",
        "job_type": "tenant_action",
        "action": "prepare_delete_tenant",
        "slug": "acme",
        "typed_slug": "acme",
        "final_confirmation": "DELETE FOREVER",
        "delete_operation_id": DELETE_OPERATION_ID,
        "generation_fingerprint": generation,
    }
    processing = paths["queue"] / "prepare-acme.processing"
    processing.write_text(json.dumps(job), encoding="utf-8")
    paths["results"].mkdir()
    (paths["results"] / "prepare-acme.json").write_text(
        json.dumps({
            "job_id": "prepare-acme",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
            "status": "succeeded",
        })
    )

    worker.run_once()

    result = json.loads((paths["results"] / "prepare-acme.json").read_text())
    assert result["status"] == "succeeded"
    assert result["action"] == "prepare_delete_tenant"
    assert not processing.exists()


def test_new_provision_rotates_existing_tenant_bridge_token(monkeypatch, tmp_path):
    from host import nr3_provision_worker as worker

    monkeypatch.setattr(worker, "BRIDGE_TOKEN_DIR", tmp_path / "tokens")
    generated = iter(["a" * 48, "b" * 48])
    monkeypatch.setattr(worker.secrets, "token_urlsafe", lambda _size: next(generated))

    assert worker.rotate_tenant_bridge_token("acme") == "a" * 48
    assert worker.rotate_tenant_bridge_token("acme") == "b" * 48
    assert (tmp_path / "tokens" / "acme").read_text().strip() == "b" * 48


def test_bridge_token_removal_requires_durable_directory_fsync(
    monkeypatch, tmp_path,
):
    from host import nr3_provision_worker as worker

    token_dir = tmp_path / "tokens"
    token_dir.mkdir()
    token = token_dir / "acme"
    token.write_text("t" * 48, encoding="utf-8")
    monkeypatch.setattr(worker, "BRIDGE_TOKEN_DIR", token_dir)
    fsynced = []
    monkeypatch.setattr(
        worker,
        "_fsync_directory_required",
        lambda path: fsynced.append(Path(path)),
    )
    details = []

    assert worker.remove_tenant_bridge_token("acme", details) is True
    assert fsynced == [token_dir]
    assert not token.exists()


def test_host_restore_injects_target_bridge_token(tmp_path):
    from host import nr3_provision_worker as worker

    source = tmp_path / "source"
    target = tmp_path / "target"
    previous = tmp_path / "previous"
    for root, slug, token in (
        (source, "donor", "donor-secret"),
        (target, "donor", "donor-secret"),
        (previous, "acme", "old-file-secret"),
    ):
        (root / "config").mkdir(parents=True)
        (root / "config" / "client.json").write_text(
            json.dumps({"slug": slug}), encoding="utf-8"
        )
        (root / "config" / "platform.env").write_text(
            f"TENANT_ID={slug}\nNR3_INTERNAL_API_TOKEN={token}\n",
            encoding="utf-8",
        )
    target_token = "target-token-" + "x" * 40

    worker.rewrite_restored_runtime_identity(
        "acme",
        source,
        target,
        previous,
        preserve_provider_connection=False,
        target_bridge_token=target_token,
        target_host_port=8123,
    )

    env_text = (target / "config" / "platform.env").read_text()
    assert f"NR3_INTERNAL_API_TOKEN={target_token}" in env_text
    assert "donor-secret" not in env_text
    assert env_text.count("NR3_INTERNAL_API_TOKEN=") == 1


def test_app_runtime_restore_preserves_target_token_and_clone_strips_donor(
    monkeypatch, tmp_path,
):
    from app import tenant_backup

    clients = tmp_path / "clients"
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(clients))
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config" / "client.json").write_text(
        json.dumps({
            "slug": "donor",
            "zernio_account_id": "attacker-account",
            "zernio_account_verified": True,
            "channel_account_allowlist": {
                "mode": "permissive",
                "zernio_accounts": ["attacker-account"],
            },
        }),
        encoding="utf-8",
    )
    (source / "config" / "platform.env").write_text(
        "TENANT_ID=donor\nNR3_INTERNAL_API_TOKEN=donor-token\n",
        encoding="utf-8",
    )

    target = clients / "acme"
    (target / "config").mkdir(parents=True)
    trusted_allowlist = {
        "mode": "strict",
        "zernio_accounts": ["trusted-account"],
    }
    (target / "config" / "client.json").write_text(
        json.dumps({
            "slug": "acme",
            "channel_account_allowlist": trusted_allowlist,
        }),
        encoding="utf-8",
    )
    (target / "config" / "platform.env").write_text(
        "TENANT_ID=acme\nNR3_INTERNAL_API_TOKEN=target-token\n",
        encoding="utf-8",
    )
    (target / "docker-compose.yml").write_text(
        tenant_backup._canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    tenant_backup._restore_client_tree(
        "acme",
        source,
        target,
        trusted_channel_allowlist=trusted_allowlist,
    )
    restored_env = (target / "config" / "platform.env").read_text()
    restored_client = json.loads((target / "config" / "client.json").read_text())
    assert "NR3_INTERNAL_API_TOKEN=target-token" in restored_env
    assert "donor-token" not in restored_env
    assert restored_client["channel_account_allowlist"] == trusted_allowlist
    assert restored_client["zernio_account_id"] == ""
    assert restored_client["zernio_account_verified"] == ""

    clone = clients / "clone"
    tenant_backup._restore_client_tree(
        "clone", source, clone, trusted_host_port=8124
    )
    clone_env = (clone / "config" / "platform.env").read_text()
    clone_client = json.loads((clone / "config" / "client.json").read_text())
    assert "NR3_INTERNAL_API_TOKEN=" not in clone_env
    assert "donor-token" not in clone_env
    assert clone_client["channel_account_allowlist"] == {}
    assert clone_client["zernio_account_id"] == ""
    assert clone_client["zernio_account_verified"] == ""


@pytest.mark.parametrize("orphan_kind", ["container", "nginx"])
def test_provision_preflight_never_removes_orphan_host_artifacts(
    monkeypatch, tmp_path, orphan_kind,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    if orphan_kind == "nginx":
        paths["nginx"].write_text(
            paths["nginx"].read_text()
            + worker.canonical_managed_nginx_block_text("acme", 8123)
        )
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[:3] == ["docker", "ps", "-a"]:
            names = "wtyj-acme\n" if orphan_kind == "container" else ""
            return _completed(cmd, stdout=names)
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(
        worker,
        "rotate_tenant_bridge_token",
        lambda _slug: (_ for _ in ()).throw(AssertionError("must not rotate")),
    )
    job = paths["queue"] / f"orphan-{orphan_kind}.json"
    job.write_text(
        json.dumps({
            "job_id": f"orphan-{orphan_kind}",
            "job_type": "tenant_provision",
            "creation_id": "creation-acme",
            "slug": "acme",
            "host_port": 8123,
            "client_data": {"slug": "acme", "password": "temporary-password"},
            "docker_compose_text": worker.canonical_docker_compose_text(
                "acme", 8123
            ),
            "managed_nginx_block_text": (
                worker.canonical_managed_nginx_block_text("acme", 8123)
            ),
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads(
        (paths["results"] / f"orphan-{orphan_kind}.json").read_text()
    )
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert not any(cmd[:3] == ["docker", "rm", "-f"] for cmd in commands)
    if orphan_kind == "nginx":
        assert "# BEGIN UNBOKS TENANT acme" in paths["nginx"].read_text()


def test_host_restore_never_executes_archive_compose(monkeypatch, tmp_path):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    tenant_dir = paths["clients"] / "acme"
    (tenant_dir / "config").mkdir(parents=True)
    (tenant_dir / "config" / "client.json").write_text(
        json.dumps({
            "slug": "acme",
            "host_port": 8123,
            "creation_id": "target-generation",
            "channel_account_allowlist": {
                "mode": "strict",
                "zernio_accounts": ["trusted-account"],
            },
        }),
        encoding="utf-8",
    )
    trusted_compose = worker.canonical_docker_compose_text("acme", 8123)
    (tenant_dir / "docker-compose.yml").write_text(
        trusted_compose, encoding="utf-8"
    )
    paths["tokens"].mkdir()
    (paths["tokens"] / "acme").write_text("target-token-" + "x" * 40)
    package = import_dir / "malicious.unboksbackup"
    malicious_compose = (
        "services:\n  agent:\n    image: attacker\n"
        "    volumes:\n      - /:/host\n"
    )
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json",
            json.dumps({
                "slug": "donor",
                "host_port": 9999,
                "zernio_account_id": "attacker-account",
                "zernio_account_verified": True,
                "channel_account_allowlist": {
                    "mode": "permissive",
                    "zernio_accounts": ["attacker-account"],
                },
            }),
        )
        archive.writestr(
            "client_tree/config/platform.env",
            "TENANT_ID=donor\nNR3_INTERNAL_API_TOKEN=donor-token\n",
        )
        archive.writestr("client_tree/docker-compose.yml", malicious_compose)

    compose_seen_at_execution = []

    def fake_run(cmd, *, cwd=None, **_kwargs):
        if cmd[:2] == ["docker", "compose"] and "up" in cmd:
            compose_seen_at_execution.append(
                (cwd / "docker-compose.yml").read_text(encoding="utf-8")
            )
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "wait_for_health", lambda _port: "health ok")
    generation = worker.tenant_generation_fingerprint("acme", tenant_dir)
    job = paths["queue"] / "restore-acme.json"
    job.write_text(
        json.dumps({
            "job_id": "restore-acme",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "acme",
            "backup_package_path": str(package),
            "preserve_provider_connection": True,
            "host_port": 8123,
            "zernio_account_id": "trusted-account",
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "restore-acme.json").read_text())
    assert result["status"] == "succeeded"
    assert compose_seen_at_execution == [trusted_compose]
    assert malicious_compose not in compose_seen_at_execution
    assert (tenant_dir / "docker-compose.yml").read_text() == trusted_compose
    restored_client = json.loads(
        (tenant_dir / "config" / "client.json").read_text()
    )
    assert restored_client["channel_account_allowlist"]["zernio_accounts"] == [
        "trusted-account"
    ]
    assert restored_client["zernio_account_id"] == ""
    assert restored_client["zernio_account_verified"] == ""
    assert restored_client["creation_id"] == "target-generation"


def test_host_clone_generates_canonical_compose_and_strips_donor_token(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    package = import_dir / "clone-malicious.unboksbackup"
    malicious_compose = "services:\n  agent:\n    image: attacker\n"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json",
            json.dumps({"slug": "donor", "creation_id": "donor-generation"}),
        )
        archive.writestr(
            "client_tree/config/platform.env",
            "TENANT_ID=donor\nNR3_INTERNAL_API_TOKEN=donor-token\n",
        )
        archive.writestr("client_tree/docker-compose.yml", malicious_compose)
    executed_compose = []

    def fake_run(cmd, *, cwd=None, **_kwargs):
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="")
        if cmd[:2] == ["docker", "compose"] and "up" in cmd:
            executed_compose.append(
                (cwd / "docker-compose.yml").read_text(encoding="utf-8")
            )
        return _completed(cmd)

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "wait_for_health", lambda _port: "health ok")
    monkeypatch.setattr(
        worker.secrets,
        "token_urlsafe",
        lambda _size: "target-token-" + "x" * 48,
    )
    (paths["queue"] / "restore-clone.json").write_text(
        json.dumps({
            "job_id": "restore-clone",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "clone",
            "backup_package_path": str(package),
            "preserve_provider_connection": False,
            "host_port": 8234,
            "creation_id": "clone-generation-current",
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "restore-clone.json").read_text())
    assert result["status"] == "succeeded", json.dumps(result, indent=2)
    canonical = worker.canonical_docker_compose_text("clone", 8234)
    assert executed_compose == [canonical]
    clone_root = paths["clients"] / "clone"
    assert (clone_root / "docker-compose.yml").read_text() == canonical
    clone_client = json.loads(
        (clone_root / "config" / "client.json").read_text()
    )
    assert clone_client["creation_id"] == "clone-generation-current"
    env = (clone_root / "config" / "platform.env").read_text()
    assert "donor-token" not in env
    assert "NR3_INTERNAL_API_TOKEN=target-token-" in env
    assert (paths["tokens"] / "clone").read_text().strip().startswith(
        "target-token-"
    )


def test_host_restore_rejects_stale_generation_before_runtime_mutation(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    tenant_dir = paths["clients"] / "acme"
    (tenant_dir / "config").mkdir(parents=True)
    client_path = tenant_dir / "config" / "client.json"
    client_path.write_text(
        json.dumps({"slug": "acme", "creation_id": "current-generation"}),
        encoding="utf-8",
    )
    (tenant_dir / "docker-compose.yml").write_text(
        worker.canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    package = import_dir / "stale.unboksbackup"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json",
            json.dumps({"slug": "donor", "creation_id": "donor-generation"}),
        )
    stale_root = tmp_path / "stale-runtime"
    (stale_root / "config").mkdir(parents=True)
    (stale_root / "config" / "client.json").write_text(
        json.dumps({"slug": "acme", "creation_id": "prior-generation"}),
        encoding="utf-8",
    )
    stale_fingerprint = worker.tenant_generation_fingerprint("acme", stale_root)
    original = client_path.read_text()
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stale restore must fail before host commands")
        ),
    )
    (paths["queue"] / "restore-stale.json").write_text(
        json.dumps({
            "job_id": "restore-stale",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "acme",
            "backup_package_path": str(package),
            "preserve_provider_connection": False,
            "host_port": 8123,
            "generation_fingerprint": stale_fingerprint,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "restore-stale.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    assert result["generation_fingerprint"] == stale_fingerprint
    assert "generation changed" in result["message"]
    assert client_path.read_text() == original
    assert not list(paths["clients"].glob(".nr3-restore-acme-*"))


def test_restore_failure_atomically_recovers_and_restarts_previous_runtime(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    tenant_dir = paths["clients"] / "acme"
    (tenant_dir / "config").mkdir(parents=True)
    (tenant_dir / "config" / "client.json").write_text(
        json.dumps({"slug": "acme", "name": "original"}),
        encoding="utf-8",
    )
    (tenant_dir / "docker-compose.yml").write_text(
        worker.canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    paths["tokens"].mkdir()
    (paths["tokens"] / "acme").write_text("target-token-" + "x" * 40)
    package = import_dir / "replacement.unboksbackup"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json",
            json.dumps({"slug": "donor", "name": "replacement"}),
        )

    def fake_run(cmd, **_kwargs):
        if cmd[:3] == ["docker", "ps", "-a"]:
            return _completed(cmd, stdout="")
        return _completed(cmd)

    health_attempts = iter([RuntimeError("replacement unhealthy"), "old healthy"])

    def fake_health(_port):
        outcome = next(health_attempts)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker, "run", fake_run)
    monkeypatch.setattr(worker, "wait_for_health", fake_health)
    generation = worker.tenant_generation_fingerprint("acme", tenant_dir)
    job = paths["queue"] / "restore-rollback.json"
    job.write_text(
        json.dumps({
            "job_id": "restore-rollback",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "acme",
            "backup_package_path": str(package),
            "preserve_provider_connection": False,
            "host_port": 8123,
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "restore-rollback.json").read_text())
    assert result["status"] == "failed"
    assert result["safe_to_release"] is False
    restored = json.loads((tenant_dir / "config" / "client.json").read_text())
    assert restored["name"] == "original"
    assert not (tenant_dir / worker.RESTORE_OWNER_MARKER).exists()
    assert not list(paths["clients"].glob(".nr3-restore-acme-*"))
    assert any("durable previous runtime restored" in item for item in result["details"])


def test_orphan_restore_resumes_after_previous_runtime_rename(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    tenant_dir = paths["clients"] / "acme"
    (tenant_dir / "config").mkdir(parents=True)
    (tenant_dir / "config" / "client.json").write_text(
        json.dumps({"slug": "acme", "name": "original"}),
        encoding="utf-8",
    )
    (tenant_dir / "docker-compose.yml").write_text(
        worker.canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    paths["tokens"].mkdir()
    (paths["tokens"] / "acme").write_text("target-token-" + "x" * 40)
    package = import_dir / "replacement.unboksbackup"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json",
            json.dumps({"slug": "donor", "name": "replacement"}),
        )
    generation = worker.tenant_generation_fingerprint("acme", tenant_dir)
    job = {
        "job_id": "restore-crash-resume",
        "job_type": "tenant_action",
        "action": "restore_tenant_runtime",
        "slug": "acme",
        "backup_package_path": str(package),
        "preserve_provider_connection": False,
        "host_port": 8123,
        "generation_fingerprint": generation,
    }
    package_digest = worker._sha256_file(package)
    state_dir = worker._restore_transaction_path("acme", job["job_id"])
    worker._new_restore_state(
        state_dir,
        {
            "version": 1,
            "job_id": job["job_id"],
            "slug": "acme",
            "package_path": str(package.resolve()),
            "package_sha256": package_digest,
            "host_port": 8123,
            "creation_id": "",
            "generation_fingerprint": generation,
            "preserve_provider_connection": False,
            "verified_zernio_account_id": "",
            "had_existing_target": True,
            "phase": "prepared",
            "token_ready": True,
            "created_at": worker.utc_now(),
        },
    )
    tenant_dir.replace(state_dir / "previous")
    processing = paths["queue"] / "restore-crash-resume.processing"
    processing.write_text(json.dumps(job), encoding="utf-8")
    monkeypatch.setattr(worker, "run", lambda cmd, **_kwargs: _completed(cmd))
    monkeypatch.setattr(worker, "wait_for_health", lambda _port: "healthy")

    worker.run_once()

    result = json.loads(
        (paths["results"] / "restore-crash-resume.json").read_text()
    )
    assert result["status"] == "succeeded"
    restored = json.loads((tenant_dir / "config" / "client.json").read_text())
    assert restored["name"] == "replacement"
    assert not state_dir.exists()
    assert not processing.exists()


def test_new_restore_target_rejects_archive_compose_without_host_port(
    monkeypatch, tmp_path,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    import_dir = tmp_path / "import-payloads"
    import_dir.mkdir()
    monkeypatch.setattr(worker, "IMPORT_PAYLOAD_DIR", import_dir)
    package = import_dir / "clone.unboksbackup"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(
            "client_tree/config/client.json", json.dumps({"slug": "donor"})
        )
        archive.writestr(
            "client_tree/docker-compose.yml",
            "services:\n  agent:\n    image: attacker\n",
        )
    monkeypatch.setattr(
        worker,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("untrusted compose must not execute")
        ),
    )
    job = paths["queue"] / "restore-clone.json"
    job.write_text(
        json.dumps({
            "job_id": "restore-clone",
            "job_type": "tenant_action",
            "action": "restore_tenant_runtime",
            "slug": "clone",
            "backup_package_path": str(package),
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "restore-clone.json").read_text())
    assert result["status"] == "failed"
    assert "Invalid host port" in result["message"]
    assert not (paths["clients"] / "clone").exists()


@pytest.mark.parametrize("control", ["\n", "\r", "\x00", "\x7f"])
def test_worker_password_reset_rejects_control_characters(
    monkeypatch, tmp_path, control,
):
    worker, paths = _configure_worker_sandbox(monkeypatch, tmp_path)
    tenant_dir = paths["clients"] / "acme" / "config"
    tenant_dir.mkdir(parents=True)
    client_path = tenant_dir / "client.json"
    env_path = tenant_dir / "platform.env"
    client_path.write_text(
        json.dumps({"slug": "acme", "password": "original-password"}),
        encoding="utf-8",
    )
    env_path.write_text(
        "DASHBOARD_PASSWORD=original-password\n", encoding="utf-8"
    )
    original_client = client_path.read_text()
    original_env = env_path.read_text()
    generation = worker.tenant_generation_fingerprint(
        "acme", paths["clients"] / "acme"
    )
    job = paths["queue"] / "reset-acme.json"
    job.write_text(
        json.dumps({
            "job_id": "reset-acme",
            "job_type": "tenant_action",
            "action": "reset_dashboard_password",
            "slug": "acme",
            "new_password": f"valid-prefix{control}injected-value",
            "generation_fingerprint": generation,
        }),
        encoding="utf-8",
    )

    worker.run_once()

    result = json.loads((paths["results"] / "reset-acme.json").read_text())
    assert result["status"] == "failed"
    assert "control character" in result["message"]
    assert client_path.read_text() == original_client
    assert env_path.read_text() == original_env


def test_import_checksums_cannot_bless_provider_identity(monkeypatch, tmp_path):
    from app import channel_connections, channel_state
    from app.tenant_backup import (
        _canonical_docker_compose_text,
        build_export_package,
        import_uploaded_package,
    )
    from app.tenants import register_tenant

    monkeypatch.setenv("NR3_DB_PATH", str(tmp_path / "nr3.db"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "clients"))
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "overrides.json"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "channels.json"))
    monkeypatch.setenv("NR3_TENANT_NOTES_PATH", str(tmp_path / "notes.json"))
    monkeypatch.setenv("NR3_TENANT_EXPORTS_DIR", str(tmp_path / "exports"))
    monkeypatch.setenv(
        "NR3_TENANT_IMPORT_ROLLBACK_DIR", str(tmp_path / "rollbacks")
    )
    target_root = tmp_path / "clients" / "acme"
    (target_root / "config").mkdir(parents=True)
    trusted_allowlist = {
        "mode": "strict",
        "zernio_accounts": ["trusted-account"],
    }
    (target_root / "config" / "client.json").write_text(
        json.dumps({
            "slug": "acme",
            "name": "Acme",
            "status": "active",
            "channel_account_allowlist": trusted_allowlist,
        }),
        encoding="utf-8",
    )
    (target_root / "config" / "platform.env").write_text(
        "TENANT_ID=acme\nNR3_INTERNAL_API_TOKEN=target-token\n",
        encoding="utf-8",
    )
    (target_root / "docker-compose.yml").write_text(
        _canonical_docker_compose_text("acme", 8123),
        encoding="utf-8",
    )
    register_tenant({"slug": "acme", "name": "Acme", "status": "active"})
    channel_connections.upsert_tenant_channel_connection(
        tenant_id="acme",
        status="connected",
        zernio_profile_id="trusted-profile",
        zernio_account_id="trusted-account",
        zernio_account_verified=True,
        phone_number_id="trusted-phone",
    )
    channel_state.set_channel("acme", "whatsapp", True)
    package = build_export_package("acme")

    with zipfile.ZipFile(package) as source_zip:
        files = {
            info.filename: source_zip.read(info.filename)
            for info in source_zip.infolist()
            if not info.is_dir()
        }
    channels = json.loads(files["channels.json"])
    channels["connections"]["whatsapp"].update({
        "status": "connected",
        "zernio_profile_id": "attacker-profile",
        "zernio_account_id": "attacker-account",
        "zernio_account_verified": True,
        "phone_number_id": "attacker-phone",
    })
    files["channels.json"] = (
        json.dumps(channels, indent=2, sort_keys=True) + "\n"
    ).encode()
    client = json.loads(files["client_tree/config/client.json"])
    client.update({
        "zernio_account_id": "attacker-account",
        "zernio_account_verified": True,
        "channel_account_allowlist": {
            "mode": "permissive",
            "zernio_accounts": ["attacker-account"],
        },
    })
    files["client_tree/config/client.json"] = (
        json.dumps(client, indent=2, sort_keys=True) + "\n"
    ).encode()
    checksums = json.loads(files["checksums.json"])
    for name in ("channels.json", "client_tree/config/client.json"):
        checksums[name] = hashlib.sha256(files[name]).hexdigest()
    files["checksums.json"] = (
        json.dumps(checksums, indent=2, sort_keys=True) + "\n"
    ).encode()
    tampered = tmp_path / "attacker-authored.unboksbackup"
    with zipfile.ZipFile(tampered, "w") as destination_zip:
        for name, content in files.items():
            destination_zip.writestr(name, content)

    result = import_uploaded_package(
        tampered.open("rb"),
        target_tenant="acme",
        mode="restore",
        confirmation="acme",
    )

    assert result["status"] == "imported"
    restored_connection = channel_connections.get_tenant_channel_connection("acme")
    assert restored_connection is not None
    assert restored_connection.zernio_account_id == "trusted-account"
    assert restored_connection.zernio_account_verified is True
    assert restored_connection.phone_number_id == "trusted-phone"
    restored_client = json.loads(
        (target_root / "config" / "client.json").read_text()
    )
    assert restored_client["channel_account_allowlist"]["mode"] == "strict"
    assert restored_client["channel_account_allowlist"]["zernio_accounts"] == [
        "trusted-account"
    ]
    assert restored_client["zernio_account_id"] == ""
    assert restored_client["zernio_account_verified"] == ""


def test_reconcile_stale_delete_result_cannot_forget_new_creation_claim(
    monkeypatch, tmp_path,
):
    from app.port_registry import read_port_registry, reserve_tenant_port
    from app.tenants import register_tenant

    registry = tmp_path / "registry.json"
    results = tmp_path / "results"
    tenant_root = tmp_path / "tenant-root"
    tenant_root.mkdir()
    monkeypatch.setenv("NR3_TENANT_REGISTRY_PATH", str(registry))
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_RECONCILED_DIR", str(tmp_path / "reconciled"))
    monkeypatch.setenv("NR3_PROVISION_CLAIMS_PATH", str(tmp_path / "claims.json"))
    monkeypatch.setenv("NR3_TENANT_CREATE_LOCK_DIR", str(tmp_path / "create-locks"))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tenant_root))

    register_tenant({"slug": "lawyer", "name": "New Owner", "status": "active"})
    reserve_tenant_port("lawyer")
    assert create_tenant_provision_claim("lawyer", "creation-new") is True
    results.mkdir(parents=True)
    (results / "old-delete.json").write_text(
        json.dumps({
            "job_id": "old-delete",
            "status": "succeeded",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "lawyer",
        }),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 0
    assert "lawyer" in json.loads(registry.read_text())["tenants"]
    assert "lawyer" in read_port_registry()
    assert tenant_provision_claim("lawyer")["creation_id"] == "creation-new"
