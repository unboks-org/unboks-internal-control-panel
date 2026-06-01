import json
from pathlib import Path

from app.provisioning import (
    auto_provision_tenant,
    queue_tenant_host_action,
    reconcile_host_action_results,
)


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


def test_auto_provision_does_not_duplicate_active_slug_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    jobs.mkdir(parents=True)
    existing = jobs / "existing.json"
    existing.write_text(
        json.dumps({"job_id": "existing-job", "slug": "acme"}),
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
    )

    assert result.status == "queued"
    assert result.job_id == "existing-job"
    assert "already active" in result.message
    assert len(list(jobs.glob("*.json"))) == 1


def test_host_action_queue_writes_suspend_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

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


def test_host_action_queue_writes_unpause_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

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


def test_host_action_queue_writes_password_reset_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

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


def test_host_action_queue_writes_restart_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

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


def test_host_action_queue_writes_restore_runtime_job(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    monkeypatch.setenv("NR3_AUTO_PROVISION", "true")
    monkeypatch.setenv("NR3_PROVISION_QUEUE_DIR", str(jobs))
    monkeypatch.setenv("NR3_PROVISION_RESULT_DIR", str(results))
    monkeypatch.setenv("NR3_PROVISION_TIMEOUT_SECONDS", "0")

    result = queue_tenant_host_action(
        slug="acme",
        action="restore_tenant_runtime",
        dashboard_url="https://dashboard.unboks.org/acme",
        backup_package_path="/root/unboks-internal-control-panel/data/tenant_import_payloads/acme.unboksbackup",
    )

    assert result.status == "queued"
    job_files = list(jobs.glob("*.json"))
    assert len(job_files) == 1
    payload = json.loads(job_files[0].read_text())
    assert payload["job_type"] == "tenant_action"
    assert payload["action"] == "restore_tenant_runtime"
    assert payload["slug"] == "acme"
    assert payload["backup_package_path"].endswith("acme.unboksbackup")


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


def test_host_action_queue_does_not_duplicate_same_active_action(monkeypatch, tmp_path):
    jobs = tmp_path / "jobs"
    results = tmp_path / "results"
    jobs.mkdir(parents=True)
    (jobs / "existing.processing").write_text(
        json.dumps({
            "job_id": "existing-delete",
            "job_type": "tenant_action",
            "action": "delete_tenant",
            "slug": "acme",
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
    )

    assert result.status == "queued"
    assert result.job_id == "existing-delete"
    assert "already active" in result.message
    assert len(list(jobs.glob("*.json"))) == 0


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
    assert '"down", "-v", "--remove-orphans"' in worker_source
    assert "NR3_PROVISION_NGINX_BACKUP_DIR=/root/nginx-sites-enabled-backups" in service_source


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
                "status": "succeeded",
                "job_type": "tenant_action",
                "action": "delete_tenant",
                "slug": "lawyer",
            }
        ),
        encoding="utf-8",
    )

    assert reconcile_host_action_results() == 1
    assert channel_connections.get_tenant_zernio_profile_id("lawyer") is None
    assert (reconciled / "job_delete.done").exists()
    assert reconcile_host_action_results() == 0
