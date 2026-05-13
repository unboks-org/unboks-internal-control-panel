import pytest


@pytest.fixture(autouse=True)
def _force_development_env(monkeypatch):
    monkeypatch.setenv("NR3_ENV", "development")
    yield
