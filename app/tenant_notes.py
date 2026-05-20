"""Small persistent note store for Nr 3 tenant workspaces."""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone

from app.tenants import NOTE_PRIORITIES, TenantNote


_VALID_PRIORITIES = {key for key, _ in NOTE_PRIORITIES}


def _state_path() -> str:
    return os.getenv("NR3_TENANT_NOTES_PATH", "data/tenant_notes.json").strip()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _load_all() -> dict:
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


def _save_all(data: dict) -> None:
    path = _state_path()
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tenant_notes.", suffix=".json", dir=parent)
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


def list_notes(slug: str) -> tuple[TenantNote, ...]:
    data = _load_all()
    tenant_notes = data.get("tenants", {}).get(slug, [])
    if not isinstance(tenant_notes, list):
        return tuple()
    parsed: list[TenantNote] = []
    for raw in tenant_notes:
        if not isinstance(raw, dict):
            continue
        note_id = raw.get("id")
        body = raw.get("body")
        if not isinstance(note_id, str) or not isinstance(body, str) or not body.strip():
            continue
        priority = raw.get("priority")
        if priority not in _VALID_PRIORITIES:
            priority = "normal"
        follow_up_date = raw.get("follow_up_date")
        if not isinstance(follow_up_date, str) or not follow_up_date.strip():
            follow_up_date = None
        parsed.append(TenantNote(
            id=note_id,
            body=body.strip(),
            author=str(raw.get("author") or "Calvin"),
            created_at=str(raw.get("created_at") or "—"),
            priority=priority,
            pinned=raw.get("pinned") is True,
            follow_up_date=follow_up_date,
            follow_up_done=raw.get("follow_up_done") is True,
        ))
    return tuple(sorted(parsed, key=lambda n: n.created_at, reverse=True))


def add_note(
    slug: str,
    body: str,
    *,
    priority: str = "normal",
    follow_up_date: str = "",
    author: str = "Calvin",
) -> TenantNote:
    clean_body = (body or "").strip()
    if not clean_body:
        raise ValueError("Note body is required.")
    clean_priority = priority if priority in _VALID_PRIORITIES else "normal"
    clean_follow = (follow_up_date or "").strip() or None
    note = {
        "id": "note-" + secrets.token_hex(8),
        "body": clean_body,
        "author": author,
        "created_at": _now(),
        "priority": clean_priority,
        "pinned": False,
        "follow_up_date": clean_follow,
        "follow_up_done": False,
    }
    data = _load_all()
    tenants = data.setdefault("tenants", {})
    notes = tenants.setdefault(slug, [])
    if not isinstance(notes, list):
        notes = []
        tenants[slug] = notes
    notes.insert(0, note)
    _save_all(data)
    return list_notes(slug)[0]


def _update_note(slug: str, note_id: str, updater) -> bool:
    data = _load_all()
    notes = data.get("tenants", {}).get(slug, [])
    if not isinstance(notes, list):
        return False
    changed = False
    for raw in notes:
        if isinstance(raw, dict) and raw.get("id") == note_id:
            updater(raw)
            changed = True
            break
    if changed:
        _save_all(data)
    return changed


def toggle_pin(slug: str, note_id: str) -> bool:
    return _update_note(
        slug,
        note_id,
        lambda raw: raw.__setitem__("pinned", not bool(raw.get("pinned"))),
    )


def mark_follow_up_done(slug: str, note_id: str) -> bool:
    return _update_note(
        slug,
        note_id,
        lambda raw: raw.__setitem__("follow_up_done", True),
    )


def forget_tenant(slug: str) -> bool:
    data = _load_all()
    tenants = data.get("tenants")
    if not isinstance(tenants, dict) or slug not in tenants:
        return False
    tenants.pop(slug, None)
    _save_all(data)
    return True
