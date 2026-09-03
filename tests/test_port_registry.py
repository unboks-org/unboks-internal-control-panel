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


def test_release_fails_closed_without_erasing_other_malformed_row(
    monkeypatch, tmp_path
):
    path = tmp_path / "ports.json"
    original = '{"alpha": 8100, "bravo": "not-a-port"}\n'
    path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    with pytest.raises(PortRegistryError, match="invalid port for bravo"):
        release_tenant_port("alpha")

    assert path.read_text(encoding="utf-8") == original


@pytest.mark.parametrize("bad_port", [True, 8100.5, 0, 65536])
def test_registry_rejects_ambiguous_or_out_of_range_ports(
    monkeypatch, tmp_path, bad_port
):
    path = tmp_path / "ports.json"
    path.write_text(json.dumps({"alpha": bad_port}), encoding="utf-8")
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    with pytest.raises(PortRegistryError):
        read_port_registry()


def test_registry_rejects_duplicate_cross_tenant_port(monkeypatch, tmp_path):
    path = tmp_path / "ports.json"
    path.write_text(
        json.dumps({"alpha": 8100, "bravo": 8100}),
        encoding="utf-8",
    )
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    with pytest.raises(PortRegistryError, match="multiple tenants"):
        reserve_tenant_port("charlie")


def test_registry_file_is_sorted_json(monkeypatch, tmp_path):
    path = tmp_path / "ports.json"
    monkeypatch.setenv("NR3_PORT_REGISTRY_PATH", str(path))

    reserve_tenant_port("zulu")
    reserve_tenant_port("alpha")

    assert list(json.loads(path.read_text(encoding="utf-8")).keys()) == ["alpha", "zulu"]
