from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.file_lock import exclusive_file_lock


DEFAULT_START = 8100
DEFAULT_END = 8999


class PortRegistryError(RuntimeError):
    pass


def _registry_path() -> Path:
    return Path(
        os.environ.get("NR3_PORT_REGISTRY_PATH")
        or os.environ.get("NR3_TENANT_PORT_REGISTRY_PATH")
        or "data/port_registry.json"
    )


def _port_range() -> tuple[int, int]:
    try:
        start = int(os.environ.get("NR3_TENANT_PORT_START", DEFAULT_START))
        end = int(os.environ.get("NR3_TENANT_PORT_END", DEFAULT_END))
    except ValueError as exc:
        raise PortRegistryError("Tenant port range must be numeric.") from exc
    if start < 1 or end > 65535 or start > end:
        raise PortRegistryError("Tenant port range is invalid.")
    return start, end


def _load(path: Path) -> dict[str, int]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PortRegistryError(f"Tenant port registry is not valid JSON: {path}") from exc
    if not isinstance(raw, dict):
        raise PortRegistryError("Tenant port registry must be an object.")
    out: dict[str, int] = {}
    for slug, value in raw.items():
        if isinstance(slug, str):
            try:
                out[slug] = int(value)
            except (TypeError, ValueError):
                continue
    return out


def _write(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(
        json.dumps(dict(sorted(data.items())), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)


def reserve_tenant_port(slug: str) -> int:
    """Return a stable, collision-free host port for a tenant slug.

    The old allocator used a 100-port hash window, which made collisions likely
    as tenant count grew. This registry keeps existing allocations stable and
    assigns the first free port in a wider range for new tenants.
    """
    clean_slug = slug.strip().lower()
    if not clean_slug:
        raise PortRegistryError("Tenant slug is required for port allocation.")

    path = _registry_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        registry = _load(path)
        if clean_slug in registry:
            return registry[clean_slug]

        start, end = _port_range()
        used = {port for port in registry.values() if start <= port <= end}
        for port in range(start, end + 1):
            if port not in used:
                registry[clean_slug] = port
                _write(path, registry)
                return port

    raise PortRegistryError(
        f"No free tenant host ports left in configured range {start}-{end}."
    )


def release_tenant_port(slug: str) -> bool:
    clean_slug = slug.strip().lower()
    if not clean_slug:
        return False
    path = _registry_path()
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        registry = _load(path)
        if clean_slug not in registry:
            return False
        del registry[clean_slug]
        _write(path, registry)
    return True


def read_port_registry() -> dict[str, Any]:
    return dict(_load(_registry_path()))
