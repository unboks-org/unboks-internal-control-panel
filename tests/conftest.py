import pytest


@pytest.fixture(autouse=True)
def _force_development_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ENV", "development")
    monkeypatch.setenv(
        "NR3_TENANT_REGISTRY_PATH",
        str(tmp_path / "tenant_registry.json"),
    )
    monkeypatch.setenv(
        "NR3_DELETE_OPERATIONS_DIR",
        str(tmp_path / "delete-operations"),
    )
    monkeypatch.setenv(
        "NR3_TENANT_CREATE_LOCK_DIR",
        str(tmp_path / "tenant-create-locks"),
    )
    monkeypatch.setenv(
        "NR3_PROVISION_CLAIMS_PATH",
        str(tmp_path / "tenant-provision-claims.json"),
    )
    yield
