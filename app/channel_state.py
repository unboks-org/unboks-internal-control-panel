"""Per-tenant channel on/off state.

A single JSON file (path overridable via NR3_CHANNEL_STATE_PATH,
default ./data/channel_state.json) maps

    {<tenant_slug>: {<channel_key>: True|False, ...}, ...}

Atomic write via os.replace so a half-written file can't corrupt
the store. Each toggle also writes the Nr2-facing ICP override key
used by /internal/tenants/{tenant}/overrides.
"""
import json
import logging
import os
import tempfile
from typing import Iterable

from app import icp_overrides


logger = logging.getLogger(__name__)


CHANNEL_KEYS: tuple[tuple[str, str], ...] = (
    ("WhatsApp", "whatsapp"),
    ("Email", "email"),
    ("Instagram", "instagram"),
    ("Facebook", "facebook"),
    ("Messenger", "messenger"),
    ("Telegram", "telegram"),
    ("Tiktok", "tiktok"),
    ("X", "x"),
)
_VALID_KEYS = {key for _, key in CHANNEL_KEYS}


def _state_path() -> str:
    return os.environ.get(
        "NR3_CHANNEL_STATE_PATH", "data/channel_state.json").strip()


def _load_all() -> dict:
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_all(data: dict) -> None:
    path = _state_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    # Atomic write via tempfile + os.replace -- a crash mid-write
    # leaves the previous file intact.
    fd, tmp = tempfile.mkstemp(
        prefix=".channel_state.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_channels(slug: str) -> dict[str, bool]:
    """Return {channel_key: bool} for the tenant. Missing channels
    default to False so the template can render every row even on
    the first visit. Never raises."""
    all_state = _load_all()
    tenant_state = all_state.get(slug) or {}
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


def toggle_channel(slug: str, channel: str) -> dict[str, bool]:
    """Flip one channel for one tenant; return the new full state
    for that tenant. Unknown channel keys are ignored (so a tampered
    URL can\'t write garbage into the state file)."""
    if channel not in _VALID_KEYS:
        logger.warning(
            "channel_state.toggle_unknown_key slug=%s channel=%r", slug, channel)
        return read_channels(slug)
    all_state = _load_all()
    tenant_state = dict(all_state.get(slug) or {})
    tenant_state[channel] = not bool(tenant_state.get(channel, False))
    all_state[slug] = tenant_state
    try:
        _save_all(all_state)
        icp_overrides.set_channel_visibility(
            slug,
            channel,
            tenant_state[channel],
        )
        logger.info(
            "channel_state.toggle slug=%s channel=%s now=%s",
            slug, channel, tenant_state[channel])
    except OSError as exc:
        logger.warning(
            "channel_state.save_failed slug=%s channel=%s err=%r",
            slug, channel, exc)
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


def forget_tenant(slug: str) -> bool:
    """Drop every channel toggle for ``slug``.

    Mirrors icp_overrides.forget_tenant so a deleted tenant leaves no
    state behind. Returns True if anything was removed."""
    all_state = _load_all()
    if slug not in all_state:
        return False
    all_state.pop(slug, None)
    try:
        _save_all(all_state)
        icp_overrides.forget_tenant(slug)
        logger.info("channel_state.forget_tenant slug=%s", slug)
    except OSError as exc:
        logger.warning("channel_state.forget_failed slug=%s err=%r", slug, exc)
    return True

