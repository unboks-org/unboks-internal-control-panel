"""delete_tenant_directory + RESERVED_SLUGS lock.

Benson's rule (2026-05-20): the `unboks` tenant is the master /
admin account. The Internal Control Panel must refuse to delete it
no matter how the call arrives. Other tenants delete cleanly.
"""
import json
import os
from pathlib import Path

import pytest

from app.tenants import (
    RESERVED_SLUGS,
    TenantDeleteError,
    create_tenant_directory,
    delete_tenant_directory,
)


@pytest.fixture
def client_dir(tmp_path):
    return str(tmp_path / "clients")


def _make(slug: str, client_dir: str, name: str = "Demo") -> str:
    return create_tenant_directory(
        slug, {"name": name, "status": "active", "plan": "trial"},
        client_dir=client_dir)


def test_unboks_is_reserved():
    assert "unboks" in RESERVED_SLUGS


def test_delete_removes_directory_completely(client_dir):
    root = _make("alpha", client_dir)
    assert Path(root, "config", "client.json").exists()

    delete_tenant_directory("alpha", client_dir=client_dir)
    assert not Path(root).exists()


def test_delete_rejects_reserved_slug(client_dir):
    # Even if a directory exists, the reserved-slug guard must fire
    # BEFORE the rmtree call.
    _make("unboks", client_dir)
    with pytest.raises(TenantDeleteError, match="reserved"):
        delete_tenant_directory("unboks", client_dir=client_dir)
    # And the directory must still be there afterwards.
    assert Path(client_dir, "unboks", "config", "client.json").exists()


def test_delete_rejects_reserved_slug_case_insensitive(client_dir):
    """validate_slug() lowercases its input, so UPPER/Mixed case
    variants of unboks must also be blocked."""
    _make("unboks", client_dir)
    for variant in ("UNBOKS", "Unboks", "UnBoks"):
        with pytest.raises(TenantDeleteError, match="reserved"):
            delete_tenant_directory(variant, client_dir=client_dir)
    assert Path(client_dir, "unboks", "config", "client.json").exists()


def test_delete_rejects_missing_directory(client_dir):
    os.makedirs(client_dir, exist_ok=True)
    with pytest.raises(TenantDeleteError, match="not found"):
        delete_tenant_directory("ghost", client_dir=client_dir)


def test_delete_rejects_bad_slug(client_dir):
    """Slug validation runs first -- garbage input never touches the
    filesystem."""
    from app.tenants import TenantCreateError
    with pytest.raises(TenantCreateError):
        delete_tenant_directory("../etc/passwd", client_dir=client_dir)
    with pytest.raises(TenantCreateError):
        delete_tenant_directory("", client_dir=client_dir)


def test_delete_one_tenant_leaves_others(client_dir):
    _make("alpha", client_dir, "Alpha")
    _make("bravo", client_dir, "Bravo")
    _make("unboks", client_dir, "Unboks")

    delete_tenant_directory("alpha", client_dir=client_dir)

    surviving = sorted(os.listdir(client_dir))
    assert surviving == ["bravo", "unboks"]
    # Bravo's config is untouched.
    with open(Path(client_dir, "bravo", "config", "client.json")) as f:
        cfg = json.load(f)
    assert cfg["business"]["name"] == "Bravo"
