import pytest


@pytest.fixture(autouse=True)
def _force_development_env(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_ENV", "development")
    monkeypatch.setenv(
        "NR3_TENANT_REGISTRY_PATH",
        str(tmp_path / "tenant_registry.json"),
    )
    yield
