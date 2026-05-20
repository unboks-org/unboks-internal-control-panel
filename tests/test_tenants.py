from __future__ import annotations

"""J3-BE-01: real tenant discovery from clients/*/config/client.json files."""
import json
import os

import pytest

from app import tenants


@pytest.fixture
def fake_client_dir(tmp_path):
    """Build a {tmp_path}/<dir>/config/client.json layout."""
    def _make(slug: str, business: dict, dir_name: str | None = None) -> None:
        d = tmp_path / (dir_name or slug) / "config"
        d.mkdir(parents=True, exist_ok=True)
        (d / "client.json").write_text(
            json.dumps({"business": business}, ensure_ascii=False))
    return tmp_path, _make


def test_load_tenants_from_disk_returns_two_real_tenants(monkeypatch, fake_client_dir):
    """tmp_path with two fake client.json files returns both tenants,
    alphabetically sorted by id, with business.name/status/plan mapped."""
    tmp_path, make = fake_client_dir
    make("unboks", {
        "slug": "unboks",
        "name": "Unboks AI",
        "status": "active",
        "plan": "demo",
    })
    make("bluefinn-charters", {
        "slug": "bluefinn-charters",
        "name": "BlueFinn Charters",
        "status": "paused",
        "plan": "trial",
    })
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path))
    result = tenants.list_tenants()
    assert len(result) == 2
    # Alphabetical sort by id: bluefinn-charters < unboks
    assert result[0].id == "bluefinn-charters"
    assert result[0].name == "BlueFinn Charters"
    assert result[0].status == "paused"
    assert result[0].plan == "trial"
    assert result[1].id == "unboks"
    assert result[1].name == "Unboks AI"
    assert result[1].status == "active"
    assert result[1].plan == "demo"
    # get_tenant pulls from list_tenants
    fetched = tenants.get_tenant("unboks")
    assert fetched is not None
    assert fetched.name == "Unboks AI"


def test_load_tenants_skips_invalid_json_and_keeps_valid(monkeypatch, tmp_path):
    """A broken client.json next to a valid one MUST NOT crash. The valid
    tenant still loads."""
    # Valid tenant
    good = tmp_path / "goodtenant" / "config"
    good.mkdir(parents=True)
    (good / "client.json").write_text(json.dumps({
        "business": {"slug": "goodtenant", "name": "Good Tenant"}
    }))
    # Broken JSON
    bad = tmp_path / "badtenant" / "config"
    bad.mkdir(parents=True)
    (bad / "client.json").write_text("{not valid json at all")
    # Another file that's valid JSON but not a dict (defensive)
    weird = tmp_path / "weirdtenant" / "config"
    weird.mkdir(parents=True)
    (weird / "client.json").write_text(json.dumps(["not", "a", "dict"]))

    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path))
    result = tenants.list_tenants()
    ids = [t.id for t in result]
    assert "goodtenant" in ids
    assert "badtenant" not in ids
    assert "weirdtenant" not in ids
    # Only 1 valid tenant loaded - the broken ones are silently skipped
    assert len(result) == 1


def test_empty_or_unset_dir_falls_back_to_placeholders(monkeypatch, tmp_path):
    """When the env var points at a missing/empty directory (or no
    parseable client.json files inside), the loader falls back to the
    hard-coded placeholder tenant list. Local-dev path."""
    # Case 1: directory exists but has no client.json files
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(empty_dir))
    result = tenants.list_tenants()
    # Fallback to placeholders means the original 3 hard-coded tenants
    ids = [t.id for t in result]
    assert "unboks" in ids, f"expected placeholder fallback; got {ids}"

    # Case 2: env var points at a non-existent path
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "does_not_exist"))
    result = tenants.list_tenants()
    ids = [t.id for t in result]
    assert "unboks" in ids

    # Case 3: env var explicitly empty -> falls through to the default
    # _DEFAULT_TENANTS_CLIENT_DIR which (in test environment) doesn't exist
    # -> placeholders kick in.
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", "")
    # Force the default path to be one that doesn't exist
    monkeypatch.setattr(tenants, "_DEFAULT_TENANTS_CLIENT_DIR",
                         str(tmp_path / "no_such_default"))
    result = tenants.list_tenants()
    ids = [t.id for t in result]
    assert "unboks" in ids


def test_slug_missing_falls_back_to_directory_name(monkeypatch, tmp_path):
    """If business.slug is missing/empty, the directory name is used as
    the tenant id."""
    d = tmp_path / "fallback-dir" / "config"
    d.mkdir(parents=True)
    (d / "client.json").write_text(json.dumps({
        "business": {"name": "Fallback Tenant", "plan": "trial"}
    }))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path))
    result = tenants.list_tenants()
    assert len(result) == 1
    assert result[0].id == "fallback-dir"
    assert result[0].name == "Fallback Tenant"


def test_invalid_status_falls_back_to_active(monkeypatch, tmp_path):
    """Unknown status string must NOT propagate to the Tenant; default
    to 'active'."""
    d = tmp_path / "weirdstatus" / "config"
    d.mkdir(parents=True)
    (d / "client.json").write_text(json.dumps({
        "business": {
            "slug": "weirdstatus",
            "name": "Weird Status",
            "status": "exploding",  # invalid
        }
    }))
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path))
    result = tenants.list_tenants()
    assert len(result) == 1
    assert result[0].status == "active"
