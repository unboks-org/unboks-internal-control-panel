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
from functools import wraps
from pathlib import Path
from typing import Iterable

from app import icp_overrides
from app.file_lock import exclusive_file_lock


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


class ChannelActivationError(ValueError):
    """Raised when a channel cannot be enabled with fail-closed evidence."""


def _require_whatsapp_activation_ready(slug: str) -> None:
    from app import channel_connections
    from app.tenants import get_tenant_client_data

    client_data = get_tenant_client_data(slug)
    business = client_data.get("business")
    source = business if isinstance(business, dict) and business else client_data
    if str(source.get("status") or client_data.get("status") or "").lower() != "active":
        raise ChannelActivationError(
            "Activate the tenant runtime before enabling WhatsApp."
        )
    suspended = icp_overrides.feature_toggles_for_tenant(slug).get(
        "tenant_suspended", {}
    )
    if suspended.get("value") is True:
        raise ChannelActivationError(
            "Unpause the tenant runtime before enabling WhatsApp."
        )
    connection = channel_connections.get_tenant_channel_connection(slug)
    if not (
        connection
        and connection.status == "connected"
        and connection.zernio_account_verified
        and str(connection.zernio_profile_id or "").strip()
        and str(connection.zernio_account_id or "").strip()
    ):
        raise ChannelActivationError(
            "Connect and verify a WhatsApp account before enabling this channel."
        )
    account_id = str(connection.zernio_account_id).strip()
    allowlist = client_data.get("channel_account_allowlist")
    raw_accounts = (
        allowlist.get("zernio_accounts") if isinstance(allowlist, dict) else None
    )
    accounts = (
        [str(item).strip() for item in raw_accounts if str(item).strip()]
        if isinstance(raw_accounts, list)
        else []
    )
    if (
        not isinstance(allowlist, dict)
        or str(allowlist.get("mode") or "").strip().lower() != "strict"
        or accounts != [account_id]
        or channel_connections.provider_id_owned_by_other_tenant(
            tenant_id=slug,
            zernio_account_id=account_id,
            zernio_profile_id=connection.zernio_profile_id,
        )
    ):
        raise ChannelActivationError(
            "WhatsApp remains off until its verified account has an exact strict allowlist."
        )


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


def _load_all_strict() -> dict:
    """Load mutation state without treating corruption as an empty store.

    ``read_channels`` deliberately remains tolerant so a damaged display store
    cannot take down the tenant workspace. Read/modify/write operations must be
    stricter: replacing an unreadable file with one tenant's new state would
    silently erase every other tenant.
    """
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError(f"Channel state is unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Channel state is malformed: {path}")
    for tenant_slug, tenant_state in data.items():
        if not isinstance(tenant_slug, str) or not isinstance(tenant_state, dict):
            raise RuntimeError(f"Channel state is malformed: {path}")
        if any(
            not isinstance(channel, str) or not isinstance(value, bool)
            for channel, value in tenant_state.items()
        ):
            raise RuntimeError(f"Channel state is malformed: {path}")
    return data


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
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _state_lock_path() -> Path:
    path = Path(_state_path())
    return path.with_name(f"{path.name}.lock")


def _serialized_tenant_mutation(function):
    """Serialize state writes with tenant creation/deletion and gate tombstones."""
    @wraps(function)
    def wrapped(slug: str, *args, **kwargs):
        from app.delete_operations import require_tenant_mutation_generation
        from app.provisioning import tenant_creation_lock

        expected_generation_id = kwargs.pop("expected_generation_id", None)
        with tenant_creation_lock(slug):
            require_tenant_mutation_generation(
                slug,
                expected_generation_id=expected_generation_id,
            )
            return function(slug, *args, **kwargs)

    return wrapped


def read_channels(slug: str) -> dict[str, bool]:
    """Return {channel_key: bool} for the tenant. Missing channels
    default to False so the template can render every row even on
    the first visit. Never raises."""
    all_state = _load_all()
    tenant_state = all_state.get(slug) or {}
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


@_serialized_tenant_mutation
def toggle_channel(slug: str, channel: str) -> dict[str, bool]:
    """Flip one channel for one tenant; return the new full state
    for that tenant. Unknown channel keys are ignored (so a tampered
    URL can\'t write garbage into the state file)."""
    if channel not in _VALID_KEYS:
        logger.warning(
            "channel_state.toggle_unknown_key slug=%s channel=%r", slug, channel)
        return read_channels(slug)
    with exclusive_file_lock(_state_lock_path()):
        all_state = _load_all_strict()
        tenant_state = dict(all_state.get(slug) or {})
        desired = not bool(tenant_state.get(channel, False))
        if channel == "whatsapp" and desired:
            _require_whatsapp_activation_ready(slug)
        if not desired:
            # Effective runtime disable comes first. A later display-store
            # failure may look stale, but cannot leave the live channel on.
            icp_overrides.set_channel_visibility(slug, channel, False)
            tenant_state[channel] = False
            all_state[slug] = tenant_state
            _save_all(all_state)
        else:
            tenant_state[channel] = True
            all_state[slug] = tenant_state
            _save_all(all_state)
            try:
                icp_overrides.set_channel_visibility(slug, channel, True)
            except Exception:
                tenant_state[channel] = False
                all_state[slug] = tenant_state
                _save_all(all_state)
                raise
    logger.info(
        "channel_state.toggle slug=%s channel=%s now=%s",
        slug, channel, tenant_state[channel])
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


@_serialized_tenant_mutation
def set_channel(slug: str, channel: str, value: bool) -> dict[str, bool]:
    """Set one channel to an explicit on/off value."""
    if channel not in _VALID_KEYS:
        logger.warning(
            "channel_state.set_unknown_key slug=%s channel=%r", slug, channel)
        return read_channels(slug)
    desired = bool(value)
    if channel == "whatsapp" and desired:
        _require_whatsapp_activation_ready(slug)
    with exclusive_file_lock(_state_lock_path()):
        all_state = _load_all_strict()
        tenant_state = dict(all_state.get(slug) or {})
        if not desired:
            icp_overrides.set_channel_visibility(slug, channel, False)
            tenant_state[channel] = False
            all_state[slug] = tenant_state
            _save_all(all_state)
        else:
            tenant_state[channel] = True
            all_state[slug] = tenant_state
            _save_all(all_state)
            try:
                icp_overrides.set_channel_visibility(slug, channel, True)
            except Exception:
                tenant_state[channel] = False
                all_state[slug] = tenant_state
                _save_all(all_state)
                raise
    logger.info(
        "channel_state.set slug=%s channel=%s value=%s",
        slug, channel, desired)
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


@_serialized_tenant_mutation
def set_all_channels(slug: str, value: bool) -> dict[str, bool]:
    """Set every known channel to the same explicit on/off value."""
    desired = bool(value)
    if desired:
        _require_whatsapp_activation_ready(slug)
    values = {key: desired for _, key in CHANNEL_KEYS}
    with exclusive_file_lock(_state_lock_path()):
        all_state = _load_all_strict()
        tenant_state = dict(all_state.get(slug) or {})
        if not desired:
            icp_overrides.set_channel_visibility_batch(slug, values)
            tenant_state.update(values)
            all_state[slug] = tenant_state
            _save_all(all_state)
        else:
            tenant_state.update(values)
            all_state[slug] = tenant_state
            _save_all(all_state)
            try:
                icp_overrides.set_channel_visibility_batch(slug, values)
            except Exception:
                safe_values = {key: False for _, key in CHANNEL_KEYS}
                tenant_state.update(safe_values)
                all_state[slug] = tenant_state
                _save_all(all_state)
                raise
    logger.info("channel_state.set_all slug=%s value=%s", slug, desired)
    return {key: bool(tenant_state.get(key, False)) for _, key in CHANNEL_KEYS}


def forget_tenant(slug: str) -> bool:
    """Drop every channel toggle for ``slug``.

    Mirrors icp_overrides.forget_tenant so a deleted tenant leaves no
    state behind. Returns True if anything was removed."""
    with exclusive_file_lock(_state_lock_path()):
        all_state = _load_all_strict()
        if slug not in all_state:
            return False
        all_state.pop(slug, None)
        _save_all(all_state)
    icp_overrides.forget_tenant(slug)
    logger.info("channel_state.forget_tenant slug=%s", slug)
    return True
