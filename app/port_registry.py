from __future__ import annotations

import json
import os
import re
import tempfile
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
    owners_by_port: dict[int, str] = {}
    for slug, value in raw.items():
        if not isinstance(slug, str) or re.fullmatch(
            r"[a-z][a-z0-9_-]{1,49}", slug
        ) is None:
            raise PortRegistryError(
                "Tenant port registry contains an invalid tenant slug."
            )
        if isinstance(value, bool):
            raise PortRegistryError(
                f"Tenant port registry contains an invalid port for {slug}."
            )
        if isinstance(value, int):
            port = value
        elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
            # Accept legacy numeric strings, but normalize only after the whole
            # shared registry has been validated.
            port = int(value)
        else:
            raise PortRegistryError(
                f"Tenant port registry contains an invalid port for {slug}."
            )
        if not 1 <= port <= 65535:
            raise PortRegistryError(
                f"Tenant port registry contains an out-of-range port for {slug}."
            )
        prior_owner = owners_by_port.get(port)
        if prior_owner is not None and prior_owner != slug:
            raise PortRegistryError(
                "Tenant port registry assigns one port to multiple tenants."
            )
        owners_by_port[port] = slug
        out[slug] = port
    return out


def _write(path: Path, data: dict[str, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(
                json.dumps(dict(sorted(data.items())), indent=2, ensure_ascii=False)
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


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
