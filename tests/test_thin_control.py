"""Invariants of the thin-control tenant workspace.

The workspace stays collapsed and thin. Tenant-workspace buttons must post to
real backend routes, while read-only sync panels can display live state.
"""
import re

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("NR3_ADMIN_PASSWORD", "test-password")
    monkeypatch.setenv("NR3_SESSION_SECRET", "test-secret-32-bytes-long-abc")
    monkeypatch.setenv("NR3_TENANTS_CLIENT_DIR", str(tmp_path / "tenants"))
    monkeypatch.setenv("NR3_CHANNEL_STATE_PATH", str(tmp_path / "ch.json"))
    monkeypatch.setenv("NR3_ICP_STATE_PATH", str(tmp_path / "ov.json"))
    (tmp_path / "tenants").mkdir()
    yield


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"password": "test-password"})
    return c


def _html(client) -> str:
    r = client.get("/admin/tenants/unboks")
    assert r.status_code == 200
    return r.text


def test_workspace_has_expected_sections(client):
    """The keep-list sections plus the read-only Nr2 knowledge sync panel."""
    html = _html(client)
    aria_labels = re.findall(
        r'<details[^>]*ws-section[^>]*>.*?aria-label="([^"]+)"',
        html, re.DOTALL,
    )
    assert aria_labels == [
        "Channels",
        "AI Agent",
        "API Usage and Health",
        "Nr2 company knowledge",
        "Escalations",
        "Tenant notes",
        "Danger zone",
    ], f"section drift: {aria_labels}"


def test_every_section_starts_closed(client):
    """Rule 3: thin control. No `<details open>` -- everything
    starts collapsed so the operator sees the whole map before
    diving into one section."""
    html = _html(client)
    opened = re.findall(r'<details[^>]*\bopen\b', html)
    assert opened == [], f"found details with open attribute: {opened}"


def test_channels_toggle_posts_to_real_backend(client):
    """Rule 1: only wired buttons are real. The channel toggle is
    a real <form method="post">, not a stub modal trigger."""
    html = _html(client)
    # 8 toggle forms (one per channel) hitting the real endpoint.
    posts = re.findall(
        r'<form method="post" action="(/admin/tenants/[^/]+/channels/[^"]+/toggle)"',
        html,
    )
    assert len(posts) == 8
    # The submit buttons must go through the real POST handler.
    form_blocks = re.findall(
        r'<form method="post"[^>]+channels[^>]+toggle"[^>]*>.*?</form>',
        html, re.DOTALL,
    )
    for blk in form_blocks:
        assert "not_connected" not in blk, "channel toggle form has not_connected stub"


def test_workspace_has_no_not_wired_modal_buttons(client):
    """Every workspace button must submit a real form endpoint."""
    html = _html(client)
    # Scope to the <main class="page-content">...</main> block. Buttons
    # from admin_base chrome (sidebar drawer, menu toggle, logout) live
    # outside this and aren't workspace buttons.
    main_m = re.search(
        r'<main[^>]+id="content"[^>]*>(.*?)</main>',
        html, re.DOTALL,
    )
    assert main_m, "page-content <main> block not found"
    workspace = main_m.group(1)

    real_forms = re.findall(
        r'<form method="post" action="([^"]+)"[^>]*>.*?</form>',
        workspace,
        re.DOTALL,
    )
    expected_real_prefixes = (
        "/admin/tenants/unboks/channels/",
        "/admin/tenants/unboks/details",
        "/admin/tenants/unboks/agent/",
        "/admin/tenants/unboks/notes",
        "/admin/tenants/unboks/sot",
        "/admin/tenants/unboks/nr2-knowledge/refresh",
    )
    for action in real_forms:
        assert action.startswith(expected_real_prefixes), action

    assert 'data-action-backend="not_connected"' not in workspace


def test_reserved_tenant_has_no_fake_suspend_button(client):
    """The master tenant is protected with text, not a fake modal button."""
    html = _html(client)
    suspend = re.search(
        r'<button[^>]*data-action="suspend-cut-off-tenant"[^>]*>',
        html,
    )
    assert suspend is None
    assert "The master Unboks tenant is protected from suspension." in html
