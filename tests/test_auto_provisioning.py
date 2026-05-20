import json
from pathlib import Path

from app.provisioning import auto_provision_tenant, queue_tenant_host_action


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


def test_host_worker_keeps_nginx_backups_outside_sites_enabled():
    worker_source = Path("host/nr3_provision_worker.py").read_text()
    service_source = Path("host/nr3-provision-worker.service").read_text()

    assert "NGINX_BACKUP_DIR" in worker_source
    assert "Never place backups inside sites-enabled" in worker_source
    assert "NGINX_SITE.with_name" not in worker_source
    assert "job_type" in worker_source
    assert "suspend_tenant" in worker_source
    assert "NR3_PROVISION_NGINX_BACKUP_DIR=/root/nginx-sites-enabled-backups" in service_source
