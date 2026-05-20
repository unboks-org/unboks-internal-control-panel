"""Nr3 -> Nr2 ICP override bridge state.

Nr2 reads channel visibility through its tenant API endpoint
`/dashboard/api/icp-overrides`. The tenant API then calls Nr3 at
`/internal/tenants/{tenant}/overrides` and expects an envelope with
feature toggles. This module is the small persistent store behind that
envelope.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from typing import Any


logger = logging.getLogger(__name__)


CHANNEL_FEATURE_KEYS: dict[str, str] = {
    "whatsapp": "whatsapp_inbox",
    "email": "email_inbox",
    "instagram": "instagram_dms",
    "facebook": "facebook_dms",
    "messenger": "messenger_dms",
    "telegram": "telegram_alerts",
    "tiktok": "tiktok_dms",
    "x": "x_dms",
}


def _state_path() -> str:
    return os.environ.get("NR3_ICP_STATE_PATH", "data/icp_overrides.json").strip()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_all() -> dict[str, Any]:
    path = _state_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {"tenants": {}}
    if not isinstance(data, dict):
        return {"tenants": {}}
    tenants = data.get("tenants")
    if not isinstance(tenants, dict):
        data["tenants"] = {}
    return data


def _save_all(data: dict[str, Any]) -> None:
    path = _state_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".icp_overrides.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def set_feature_toggle(
    tenant_id: str,
    feature_key: str,
    value: bool,
    *,
    updated_by: str = "nr3-admin",
) -> None:
    """Persist one feature toggle override for one tenant."""
    data = _load_all()
    tenants = data.setdefault("tenants", {})
    tenant_state = tenants.setdefault(tenant_id, {})
    toggles = tenant_state.setdefault("feature_toggles", {})
    toggles[feature_key] = {
        "value": bool(value),
        "source": "icp_override",
        "wired": True,
        "updated_at": _now(),
        "updated_by": updated_by,
    }
    _save_all(data)
    logger.info(
        "icp_overrides.set_feature tenant=%s key=%s value=%s",
        tenant_id,
        feature_key,
        bool(value),
    )


def set_channel_visibility(
    tenant_id: str,
    channel_key: str,
    value: bool,
    *,
    updated_by: str = "nr3-admin",
) -> None:
    feature_key = CHANNEL_FEATURE_KEYS.get(channel_key)
    if not feature_key:
        logger.warning(
            "icp_overrides.unknown_channel tenant=%s channel=%r",
            tenant_id,
            channel_key,
        )
        return
    set_feature_toggle(
        tenant_id,
        feature_key,
        value,
        updated_by=updated_by,
    )


def forget_tenant(tenant_id: str) -> bool:
    """Drop every override row for ``tenant_id``.

    Returns True if any state was removed. Called from the tenant-delete
    flow so a deleted tenant does not leave ghost feature-toggle entries
    that resurface in Nr2 via /internal/tenants/.../overrides.
    """
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else None
    if not isinstance(tenants, dict) or tenant_id not in tenants:
        return False
    tenants.pop(tenant_id, None)
    _save_all(data)
    logger.info("icp_overrides.forget_tenant tenant=%s", tenant_id)
    return True


def feature_toggles_for_tenant(tenant_id: str) -> dict[str, dict[str, Any]]:
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    tenant_state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    toggles = (
        tenant_state.get("feature_toggles")
        if isinstance(tenant_state, dict)
        else {}
    )
    if not isinstance(toggles, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for key, raw in toggles.items():
        if not isinstance(key, str) or not isinstance(raw, dict):
            continue
        result[key] = {
            "value": raw.get("value") is True,
            "source": raw.get("source") or "icp_override",
            "wired": raw.get("wired") is not False,
            "updated_at": raw.get("updated_at"),
            "updated_by": raw.get("updated_by"),
        }
    return result


def effective_state_envelope(tenant_id: str) -> dict[str, Any]:
    """Return the exact envelope shape Nr2 expects from the bridge."""
    return {
        "available": True,
        "tenant_id": tenant_id,
        "feature_toggles": feature_toggles_for_tenant(tenant_id),
        "display_metadata": {},
        "sot_entries": [],
        "ai_agent_settings": {
            "tone": None,
            "escalation_rules": None,
        },
    }
