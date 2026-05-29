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
import secrets
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


def _tenant_state(data: dict[str, Any], tenant_id: str) -> dict[str, Any]:
    tenants = data.setdefault("tenants", {})
    state = tenants.setdefault(tenant_id, {})
    if not isinstance(state, dict):
        state = {}
        tenants[tenant_id] = state
    return state


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _normalize_tone(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    tone = _clean_text(raw.get("tone"))
    if not tone:
        return None
    return {
        "tone": tone,
        "notes": _clean_text(raw.get("notes")),
        "source": raw.get("source") or "icp_override",
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def _normalize_escalation_rules(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    soft_raw = raw.get("soft_escalation")
    hard_raw = raw.get("hard_escalation")
    if not isinstance(soft_raw, dict) and not isinstance(hard_raw, dict):
        return None
    soft_when = _clean_text(
        soft_raw.get("when") if isinstance(soft_raw, dict) else ""
    )
    hard_when = _clean_text(
        hard_raw.get("when") if isinstance(hard_raw, dict) else ""
    )
    soft_enabled = bool(
        soft_raw.get("enabled") if isinstance(soft_raw, dict) else False
    )
    hard_enabled = bool(
        hard_raw.get("enabled") if isinstance(hard_raw, dict) else False
    )
    if not (soft_enabled or hard_enabled or soft_when or hard_when):
        return None
    return {
        "soft_escalation": {
            "enabled": soft_enabled,
            "when": soft_when,
        },
        "hard_escalation": {
            "enabled": hard_enabled,
            "when": hard_when,
        },
        "source": raw.get("source") or "icp_override",
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def _normalize_sot_entry(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _clean_text(raw.get("title"))
    content = _clean_text(raw.get("content"))
    if not title or not content:
        return None
    return {
        "id": _clean_text(raw.get("id")) or secrets.token_urlsafe(8),
        "title": title,
        "content": content,
        "category": _clean_text(raw.get("category")) or "general",
        "source": raw.get("source") or "icp_override",
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def set_feature_toggle(
    tenant_id: str,
    feature_key: str,
    value: bool,
    *,
    updated_by: str = "nr3-admin",
) -> None:
    """Persist one feature toggle override for one tenant."""
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
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


def set_ai_tone(
    tenant_id: str,
    tone: str,
    *,
    notes: str = "",
    updated_by: str = "nr3-admin",
) -> None:
    """Set or clear the ICP tone/personality override for one tenant."""
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
    settings = tenant_state.setdefault("ai_agent_settings", {})
    clean_tone = _clean_text(tone)
    if clean_tone:
        settings["tone"] = {
            "tone": clean_tone,
            "notes": _clean_text(notes),
            "source": "icp_override",
            "updated_at": _now(),
            "updated_by": updated_by,
        }
    else:
        settings["tone"] = None
    settings.setdefault("escalation_rules", None)
    _save_all(data)
    logger.info(
        "icp_overrides.set_ai_tone tenant=%s present=%s",
        tenant_id,
        bool(clean_tone),
    )


def set_escalation_rules(
    tenant_id: str,
    *,
    soft_when: str = "",
    hard_when: str = "",
    updated_by: str = "nr3-admin",
) -> None:
    """Set or clear the ICP escalation rules override for one tenant.

    A non-empty textarea enables that escalation type. Empty text disables
    it. When both are empty, the whole override is cleared.
    """
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
    settings = tenant_state.setdefault("ai_agent_settings", {})
    soft = _clean_text(soft_when)
    hard = _clean_text(hard_when)
    if soft or hard:
        settings["escalation_rules"] = {
            "soft_escalation": {
                "enabled": bool(soft),
                "when": soft,
            },
            "hard_escalation": {
                "enabled": bool(hard),
                "when": hard,
            },
            "source": "icp_override",
            "updated_at": _now(),
            "updated_by": updated_by,
        }
    else:
        settings["escalation_rules"] = None
    settings.setdefault("tone", None)
    _save_all(data)
    logger.info(
        "icp_overrides.set_escalation_rules tenant=%s present=%s",
        tenant_id,
        bool(soft or hard),
    )


def set_agent_name_override(
    tenant_id: str,
    name: str,
    *,
    updated_by: str = "nr3-admin",
) -> None:
    """Set or clear the admin AI Agent name override for one tenant."""
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
    settings = tenant_state.setdefault("ai_agent_settings", {})
    clean_name = _clean_text(name)
    if clean_name:
        settings["agent_name"] = {
            "name": clean_name,
            "source": "icp_override",
            "updated_at": _now(),
            "updated_by": updated_by,
        }
    else:
        settings["agent_name"] = None
    settings.setdefault("tone", None)
    settings.setdefault("escalation_rules", None)
    _save_all(data)
    logger.info(
        "icp_overrides.set_agent_name tenant=%s present=%s",
        tenant_id,
        bool(clean_name),
    )


def _normalize_response_timing(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    preset = str(raw.get("preset") or "balanced").strip().lower()
    if preset not in {"fast", "balanced", "patient"}:
        preset = "balanced"
    preset_delay = {"fast": 5.0, "balanced": 12.0, "patient": 15.0}[preset]
    try:
        delay = float(raw.get("delay_seconds", preset_delay))
    except (TypeError, ValueError):
        delay = preset_delay
    try:
        max_wait = float(raw.get("max_wait_seconds", 25.0))
    except (TypeError, ValueError):
        max_wait = 25.0
    delay = max(3.0, min(20.0, delay))
    max_wait = max(delay, min(45.0, max_wait))
    return {
        "message_batching_enabled": bool(raw.get("message_batching_enabled", True)),
        "preset": preset,
        "delay_seconds": delay,
        "max_wait_seconds": max_wait,
    }


def set_response_timing_override(
    tenant_id: str,
    *,
    enabled: bool = True,
    preset: str = "balanced",
    delay_seconds: float = 12.0,
    max_wait_seconds: float = 25.0,
    clear: bool = False,
    updated_by: str = "nr3-admin",
) -> None:
    """Set or clear the admin response timing override for one tenant."""
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
    if clear:
        tenant_state["response_timing"] = None
    else:
        settings = _normalize_response_timing({
            "message_batching_enabled": enabled,
            "preset": preset,
            "delay_seconds": delay_seconds,
            "max_wait_seconds": max_wait_seconds,
        })
        tenant_state["response_timing"] = {
            "settings": settings,
            "source": "icp_override",
            "updated_at": _now(),
            "updated_by": updated_by,
        }
    _save_all(data)
    logger.info(
        "icp_overrides.set_response_timing tenant=%s present=%s",
        tenant_id,
        not clear,
    )


def response_timing_for_tenant(tenant_id: str) -> dict[str, Any] | None:
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    tenant_state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    raw = tenant_state.get("response_timing") if isinstance(tenant_state, dict) else None
    if not isinstance(raw, dict):
        return None
    settings = _normalize_response_timing(raw.get("settings"))
    if not settings:
        return None
    return {
        "settings": settings,
        "source": raw.get("source") or "icp_override",
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
    }


def ai_agent_settings_for_tenant(tenant_id: str) -> dict[str, Any]:
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    tenant_state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    raw = (
        tenant_state.get("ai_agent_settings")
        if isinstance(tenant_state, dict)
        else {}
    )
    if not isinstance(raw, dict):
        raw = {}
    return {
        "tone": _normalize_tone(raw.get("tone")),
        "escalation_rules": _normalize_escalation_rules(
            raw.get("escalation_rules")
        ),
        "agent_name": (
            raw.get("agent_name")
            if isinstance(raw.get("agent_name"), dict)
            else None
        ),
    }


def sot_entries_for_tenant(tenant_id: str) -> list[dict[str, Any]]:
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    tenant_state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    raw_entries = (
        tenant_state.get("sot_entries")
        if isinstance(tenant_state, dict)
        else []
    )
    if not isinstance(raw_entries, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw in raw_entries:
        entry = _normalize_sot_entry(raw)
        if entry:
            entries.append(entry)
    return entries


def add_sot_entry(
    tenant_id: str,
    *,
    title: str,
    content: str,
    category: str = "general",
    entry_id: str = "",
    updated_by: str = "nr3-admin",
) -> dict[str, Any]:
    """Add one authoritative Source of Truth entry for a tenant."""
    clean_title = _clean_text(title)
    clean_content = _clean_text(content)
    if not clean_title:
        raise ValueError("SOT title is required.")
    if not clean_content:
        raise ValueError("SOT content is required.")
    clean_id = _clean_text(entry_id)
    entry = {
        "id": clean_id or secrets.token_urlsafe(8),
        "title": clean_title,
        "content": clean_content,
        "category": _clean_text(category) or "general",
        "source": "icp_override",
        "updated_at": _now(),
        "updated_by": updated_by,
    }
    data = _load_all()
    tenant_state = _tenant_state(data, tenant_id)
    entries = tenant_state.setdefault("sot_entries", [])
    if not isinstance(entries, list):
        entries = []
        tenant_state["sot_entries"] = entries
    if clean_id:
        entries = [
            existing for existing in entries
            if _clean_text(existing.get("id")) != clean_id
        ]
    entries.insert(0, entry)
    _save_all(data)
    logger.info("icp_overrides.add_sot_entry tenant=%s title=%r", tenant_id, clean_title)
    return entry


def delete_sot_entry(tenant_id: str, entry_id: str) -> bool:
    data = _load_all()
    tenants = data.get("tenants") if isinstance(data, dict) else {}
    tenant_state = tenants.get(tenant_id) if isinstance(tenants, dict) else {}
    if not isinstance(tenant_state, dict):
        return False
    entries = tenant_state.get("sot_entries")
    if not isinstance(entries, list):
        return False
    target = _clean_text(entry_id)
    kept = [entry for entry in entries if _clean_text(entry.get("id")) != target]
    if len(kept) == len(entries):
        return False
    tenant_state["sot_entries"] = kept
    _save_all(data)
    logger.info("icp_overrides.delete_sot_entry tenant=%s entry=%s", tenant_id, target)
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
        "channel_connections": channel_connections_for_tenant(tenant_id),
        "display_metadata": {},
        "sot_entries": sot_entries_for_tenant(tenant_id),
        "ai_agent_settings": ai_agent_settings_for_tenant(tenant_id),
        "response_timing": response_timing_for_tenant(tenant_id),
    }


def channel_connections_for_tenant(tenant_id: str) -> dict[str, Any]:
    """Return provider-backed channel connection state for Nr2 consumers."""
    try:
        from app import channel_connections

        connection = channel_connections.get_tenant_channel_connection(tenant_id)
    except Exception as exc:
        logger.warning(
            "icp_overrides.channel_connections_failed tenant=%s error=%s",
            tenant_id,
            str(exc)[:200],
        )
        return {}
    if connection is None:
        return {}
    return {
        connection.channel: {
            "provider": connection.provider,
            "status": connection.status,
            "connected": connection.status == "connected",
            "display_phone_number": connection.display_phone_number,
            "phone_number_id": connection.phone_number_id,
            "zernio_account_id": connection.zernio_account_id,
            "connected_at": connection.connected_at,
            "updated_at": connection.updated_at,
        }
    }
