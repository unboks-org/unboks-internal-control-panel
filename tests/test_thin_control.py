"""Invariants of the v0.1-clean-thin-control workspace.

Calvin's brief (2026-05-20): exactly 5 sections, all closed by
default, only channel toggles connected to a real backend, every
other button shows the 'Not wired yet' modal. These tests pin that
state so future commits can't quietly re-introduce dead UI or
half-finished features.
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


def test_workspace_has_exactly_five_sections(client):
    """The 5 keep-list sections (Channels, AI Agent, Escalations,
    Tenant notes, Danger zone) -- no more, no less."""
    html = _html(client)
    aria_labels = re.findall(
        r'<details[^>]*ws-section[^>]*>.*?aria-label="([^"]+)"',
        html, re.DOTALL,
    )
    assert aria_labels == [
        "Channels", "AI Agent", "Escalations", "Tenant notes", "Danger zone",
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
    # The submit buttons must NOT carry data-action-backend="not_connected"
    # -- they go through the real POST handler, not the modal.
    form_blocks = re.findall(
        r'<form method="post"[^>]+channels[^>]+toggle"[^>]*>.*?</form>',
        html, re.DOTALL,
    )
    for blk in form_blocks:
        assert "not_connected" not in blk, "channel toggle form has not_connected stub"


def test_every_non_channel_button_routes_to_the_not_wired_modal(client):
    """Rule 1 + the modal contract: every button inside the workspace
    content (other than the channel-toggle <form>) carries
    data-action-backend='not_connected', so the existing admin.js
    handler will catch it and show the modal."""
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

    btn_blocks = re.findall(r'<button\b[^>]*>', workspace)
    # Drop the channel-toggle submit button (real backend, not a stub).
    btn_blocks = [b for b in btn_blocks if "ios-toggle" not in b]

    unwired = [b for b in btn_blocks
               if 'data-action-backend="not_connected"' not in b]
    assert unwired == [], (
        "workspace buttons missing the not_connected hook (would "
        f"silently do nothing on click): {unwired}"
    )


def test_suspend_button_is_clickable_not_disabled(client):
    """The old Suspend button was `disabled`, which made admin.js
    bail out before opening the modal -- so clicking it did nothing.
    It must be enabled so the modal fires with the 'dangerous'
    consequence text."""
    html = _html(client)
    suspend = re.search(
        r'<button[^>]*data-action="suspend-cut-off-tenant"[^>]*>',
        html,
    )
    assert suspend, "suspend button missing"
    btn = suspend.group(0)
    assert " disabled" not in btn, "Suspend button still disabled"
    assert 'data-action-backend="not_connected"' in btn
    assert "data-action-consequence=" in btn
