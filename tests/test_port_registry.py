import json

import pytest

from app.port_registry import (
    PortRegistryError,
    read_port_registry,
    release_tenant_port,
    reserve_tenant_port,
)


def test_reserve_tenant_port_is_stable_and_sequential(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_TENANT_PORT_START", "8100")
    monkeypatch.setenv("NR3_TENANT_PORT_END", "8102")

    assert reserve_tenant_port("alpha") == 8100
    assert reserve_tenant_port("bravo") == 8101
    assert reserve_tenant_port("alpha") == 8100
    assert read_port_registry() == {"alpha": 8100, "bravo": 8101}


def test_reserve_tenant_port_raises_when_range_is_full(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_TENANT_PORT_START", "8100")
    monkeypatch.setenv("NR3_TENANT_PORT_END", "8100")

    assert reserve_tenant_port("alpha") == 8100
    with pytest.raises(PortRegistryError):
        reserve_tenant_port("bravo")


def test_release_tenant_port_frees_mapping(monkeypatch, tmp_path):
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(tmp_path / "ports.json"))
    monkeypatch.setenv("NR3_TENANT_PORT_START", "8100")
    monkeypatch.setenv("NR3_TENANT_PORT_END", "8101")

    assert reserve_tenant_port("alpha") == 8100
    assert release_tenant_port("alpha") is True
    assert reserve_tenant_port("bravo") == 8100


def test_invalid_registry_json_fails_closed(monkeypatch, tmp_path):
    path = tmp_path / "ports.json"
    path.write_text("{bad", encoding="utf-8")
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    with pytest.raises(PortRegistryError):
        reserve_tenant_port("alpha")


def test_registry_file_is_sorted_json(monkeypatch, tmp_path):
    path = tmp_path / "ports.json"
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    reserve_tenant_port("zulu")
    reserve_tenant_port("alpha")

    assert list(json.loads(path.read_text(encoding="utf-8")).keys()) == ["alpha", "zulu"]
