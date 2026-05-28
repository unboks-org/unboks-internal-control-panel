"""Prompt source inventory and conflict checks for Nr3.

This module is intentionally conservative: it only reports conflicts it can
derive from real tenant data, ICP override state, and live prompt-builder files.
Sources that are known to exist but are not machine-readable yet are surfaced as
not indexed instead of being faked.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import icp_overrides
from app.tenants import get_tenant, get_tenant_client_data, list_tenants, validate_slug


PRIORITY_ORDER = (
    "Platform safety rules",
    "Legal/compliance restrictions",
    "Tenant-specific hard restrictions",
    "Language rules",
    "SOT/company facts",
    "Tone/style",
    "Channel-specific formatting",
    "Temporary campaign/offers",
    "Soft preferences",
)

PRIORITY_RANK = {name: idx + 1 for idx, name in enumerate(PRIORITY_ORDER)}
SAFETY_LOCK_PRIORITIES = {
    "Platform safety rules",
    "Legal/compliance restrictions",
    "Tenant-specific hard restrictions",
}

CONFLICT_STATE_PATH = "data/prompt_conflict_state.json"


@dataclass(frozen=True)
class PromptSource:
    id: str
    tenant_id: str
    name: str
    source_type: str
    location: str
    active: bool
    priority: str
    last_updated: str
    text: str
    used_in: tuple[str, ...]
    indexed: bool = True
    editable: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class PromptConflict:
    id: str
    tenant_id: str
    severity: str
    category: str
    title: str
    source_a: PromptSource
    source_b: PromptSource
    instruction_a: str
    instruction_b: str
    winner: str
    why: str
    recommended_fix: str
    ignored: bool = False
    resolved: bool = False


@dataclass(frozen=True)
class PromptAudit:
    tenant_id: str
    sources: tuple[PromptSource, ...]
    conflicts: tuple[PromptConflict, ...]
    effective_rules: tuple[PromptSource, ...]
    suppressed_rules: tuple[PromptSource, ...]
    missing_required_rules: tuple[str, ...]
    not_indexed_sources: tuple[PromptSource, ...]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _hash(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:12]


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _state_path() -> Path:
    return Path(os.getenv("NR3_PROMPT_CONFLICT_STATE_PATH", CONFLICT_STATE_PATH))


def _load_state() -> dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"sources": {}, "conflicts": {}}
    if not isinstance(data, dict):
        return {"sources": {}, "conflicts": {}}
    data.setdefault("sources", {})
    data.setdefault("conflicts", {})
    return data


def _save_state(data: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prompt_conflicts.", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _source_override(source_id: str) -> dict[str, Any]:
    state = _load_state()
    sources = state.get("sources") if isinstance(state.get("sources"), dict) else {}
    raw = sources.get(source_id)
    return raw if isinstance(raw, dict) else {}


def set_source_priority(source_id: str, priority: str) -> None:
    if priority not in PRIORITY_RANK:
        raise ValueError("Unsupported priority.")
    state = _load_state()
    sources = state.setdefault("sources", {})
    row = sources.setdefault(source_id, {})
    row["priority"] = priority
    row["updated_at"] = _now()
    _save_state(state)


def set_source_disabled(source_id: str, disabled: bool) -> None:
    state = _load_state()
    sources = state.setdefault("sources", {})
    row = sources.setdefault(source_id, {})
    row["disabled"] = bool(disabled)
    row["updated_at"] = _now()
    _save_state(state)


def set_conflict_state(conflict_id: str, *, ignored: bool | None = None, resolved: bool | None = None) -> None:
    state = _load_state()
    conflicts = state.setdefault("conflicts", {})
    row = conflicts.setdefault(conflict_id, {})
    if ignored is not None:
        row["ignored"] = bool(ignored)
    if resolved is not None:
        row["resolved"] = bool(resolved)
    row["updated_at"] = _now()
    _save_state(state)


def _conflict_state(conflict_id: str) -> dict[str, Any]:
    state = _load_state()
    conflicts = state.get("conflicts") if isinstance(state.get("conflicts"), dict) else {}
    raw = conflicts.get(conflict_id)
    return raw if isinstance(raw, dict) else {}


def _make_source(
    *,
    tenant_id: str,
    name: str,
    source_type: str,
    location: str,
    priority: str,
    text: str,
    used_in: tuple[str, ...],
    active: bool = True,
    last_updated: str = "",
    indexed: bool = True,
    editable: bool = False,
) -> PromptSource:
    source_id = _hash(tenant_id, name, location, text[:180])
    override = _source_override(source_id)
    final_priority = override.get("priority") if override.get("priority") in PRIORITY_RANK else priority
    disabled = bool(override.get("disabled"))
    return PromptSource(
        id=source_id,
        tenant_id=tenant_id,
        name=name,
        source_type=source_type,
        location=location,
        active=bool(active) and not disabled,
        priority=final_priority,
        last_updated=last_updated or "Unknown",
        text=text,
        used_in=used_in,
        indexed=indexed,
        editable=editable,
        disabled=disabled,
    )


def _wtyj_repo_path() -> Path | None:
    candidates = [
        os.getenv("WTYJ_REPO_PATH", "").strip(),
        "/Users/Calvi/Documents/Codex/wtyj-agent",
        "/root/wtyj",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _extract_file_snippet(path: Path, anchors: tuple[str, ...], radius: int = 4) -> str:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    matches: list[str] = []
    lower_anchors = tuple(anchor.lower() for anchor in anchors)
    for idx, line in enumerate(lines):
        if any(anchor in line.lower() for anchor in lower_anchors):
            start = max(0, idx - radius)
            end = min(len(lines), idx + radius + 1)
            matches.extend(lines[start:end])
            matches.append("...")
    return "\n".join(matches[:180]).strip()


def _static_prompt_sources(tenant_id: str) -> list[PromptSource]:
    repo = _wtyj_repo_path()
    if repo is None:
        return [
            _make_source(
                tenant_id=tenant_id,
                name="Marina live prompt builders",
                source_type="runtime_code",
                location="WTYJ_REPO_PATH not configured",
                priority="Platform safety rules",
                text="Not indexed yet: local/live WTYJ prompt builder repository is unavailable to Nr3.",
                used_in=("WhatsApp", "Email", "Instagram", "Facebook", "dashboard suggest reply"),
                active=True,
                indexed=False,
            )
        ]
    sources: list[PromptSource] = []
    marina_path = repo / "wtyj" / "agents" / "marina" / "marina_agent.py"
    marina_text = _extract_file_snippet(
        marina_path,
        (
            "HARD REFUSAL RULES",
            "FINAL TENANT-SPECIFIC OPERATOR OVERRIDES",
            "primary language",
            "legal-service tenants",
            "jokes",
            "clinical",
        ),
        radius=6,
    )
    if marina_text:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Base Marina system prompt and safety locks",
            source_type="runtime_code",
            location=str(marina_path),
            priority="Platform safety rules",
            text=marina_text,
            used_in=("WhatsApp", "Email"),
            active=True,
            indexed=True,
        ))
    dm_path = repo / "wtyj" / "agents" / "social" / "dm_agent.py"
    dm_text = _extract_file_snippet(
        dm_path,
        (
            "FINAL TENANT-SPECIFIC OPERATOR OVERRIDES",
            "friendly, casual",
            "master prompt",
            "fallback",
        ),
        radius=5,
    )
    if dm_text:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="DM channel prompt builder",
            source_type="runtime_code",
            location=str(dm_path),
            priority="Channel-specific formatting",
            text=dm_text,
            used_in=("Instagram", "Facebook", "Messenger", "X"),
            active=True,
            indexed=True,
        ))
    summary_path = repo / "wtyj" / "dashboard" / "escalation_summary.py"
    summary_text = _extract_file_snippet(
        summary_path,
        ("Switch to human takeover", "fallback", "system prompt"),
        radius=4,
    )
    if summary_text:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Escalation summary prompt",
            source_type="runtime_code",
            location=str(summary_path),
            priority="Tenant-specific hard restrictions",
            text=summary_text,
            used_in=("Escalations", "dashboard summary"),
            active=True,
            indexed=True,
        ))
    return sources


def _client_json_sources(tenant_id: str, data: dict[str, Any]) -> list[PromptSource]:
    sources: list[PromptSource] = []
    business = data.get("business") if isinstance(data.get("business"), dict) else {}
    language_bits = [
        _clean(data.get("primary_language") or business.get("primary_language")),
        ", ".join(data.get("languages") or business.get("languages") or [])
        if isinstance(data.get("languages") or business.get("languages"), list)
        else _clean(data.get("languages") or business.get("languages")),
    ]
    language_text = "\n".join(bit for bit in language_bits if bit)
    if language_text:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Tenant language settings",
            source_type="client_json",
            location=f"/root/clients/{tenant_id}/config/client.json",
            priority="Language rules",
            text=language_text,
            used_in=("WhatsApp", "Email", "Instagram", "dashboard suggest reply"),
            last_updated=_clean(data.get("updated_at") or data.get("created_at")),
            editable=False,
        ))
    tone = _clean(data.get("agent_tone") or business.get("agent_tone"))
    persona = data.get("agent_persona") if isinstance(data.get("agent_persona"), dict) else {}
    freeform = _clean(persona.get("freeform_notes") or data.get("agent_default") or data.get("business_brief"))
    if tone or freeform:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Tenant default tone/persona",
            source_type="client_json",
            location=f"/root/clients/{tenant_id}/config/client.json",
            priority="Tone/style",
            text="\n".join(part for part in (tone, freeform) if part),
            used_in=("WhatsApp", "Email", "Instagram"),
            last_updated=_clean(data.get("updated_at") or data.get("created_at")),
        ))
    guardrails = []
    for key in ("clinical_guardrails", "safety", "compliance", "safety_restrictions"):
        raw = data.get(key) or business.get(key)
        if isinstance(raw, list):
            guardrails.extend(_clean(item) for item in raw if _clean(item))
        elif isinstance(raw, dict):
            guardrails.extend(f"{k}: {v}" for k, v in raw.items() if _clean(v))
        elif _clean(raw):
            guardrails.append(_clean(raw))
    if guardrails:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Tenant safety/compliance restrictions",
            source_type="client_json",
            location=f"/root/clients/{tenant_id}/config/client.json",
            priority="Legal/compliance restrictions",
            text="\n".join(guardrails),
            used_in=("WhatsApp", "Email", "Instagram", "Escalations"),
            last_updated=_clean(data.get("updated_at") or data.get("created_at")),
        ))
    facts = []
    for key in ("name", "email", "phone", "whatsapp", "website", "opening_hours", "hours"):
        value = data.get(key) or business.get(key)
        if _clean(value):
            facts.append(f"{key}: {_clean(value)}")
    if facts:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Tenant business facts",
            source_type="client_json",
            location=f"/root/clients/{tenant_id}/config/client.json",
            priority="SOT/company facts",
            text="\n".join(facts),
            used_in=("WhatsApp", "Email", "Settings", "dashboard suggest reply"),
            last_updated=_clean(data.get("updated_at") or data.get("created_at")),
        ))
    return sources


def _icp_sources(tenant_id: str) -> list[PromptSource]:
    ai = icp_overrides.ai_agent_settings_for_tenant(tenant_id)
    entries = icp_overrides.sot_entries_for_tenant(tenant_id)
    sources: list[PromptSource] = []
    tone = ai.get("tone")
    if isinstance(tone, dict) and _clean(tone.get("tone")):
        sources.append(_make_source(
            tenant_id=tenant_id,
            name="Nr3 tone override",
            source_type="nr3_override",
            location="data/icp_overrides.json:ai_agent_settings.tone",
            priority="Tone/style",
            text="\n".join(filter(None, [_clean(tone.get("tone")), _clean(tone.get("notes"))])),
            used_in=("WhatsApp", "Email", "Instagram"),
            last_updated=_clean(tone.get("updated_at")),
            editable=True,
        ))
    rules = ai.get("escalation_rules")
    if isinstance(rules, dict):
        chunks = []
        for key in ("soft_escalation", "hard_escalation"):
            raw = rules.get(key)
            if isinstance(raw, dict) and _clean(raw.get("when")):
                chunks.append(f"{key}: {_clean(raw.get('when'))}")
        if chunks:
            sources.append(_make_source(
                tenant_id=tenant_id,
                name="Nr3 escalation rules override",
                source_type="nr3_override",
                location="data/icp_overrides.json:ai_agent_settings.escalation_rules",
                priority="Tenant-specific hard restrictions",
                text="\n".join(chunks),
                used_in=("WhatsApp", "Email", "Escalations"),
                last_updated=_clean(rules.get("updated_at")),
                editable=True,
            ))
    for entry in entries:
        sources.append(_make_source(
            tenant_id=tenant_id,
            name=f"Nr3 SOT: {entry.get('title')}",
            source_type="nr3_sot",
            location=f"data/icp_overrides.json:sot_entries:{entry.get('id')}",
            priority="SOT/company facts",
            text=f"{entry.get('title')}\n{entry.get('category')}\n{entry.get('content')}",
            used_in=("WhatsApp", "Email", "Instagram"),
            last_updated=_clean(entry.get("updated_at")),
            editable=True,
        ))
    return sources


def prompt_sources_for_tenant(tenant_id: str) -> tuple[PromptSource, ...]:
    validate_slug(tenant_id)
    data = get_tenant_client_data(tenant_id)
    sources: list[PromptSource] = []
    sources.extend(_static_prompt_sources(tenant_id))
    sources.extend(_client_json_sources(tenant_id, data))
    sources.extend(_icp_sources(tenant_id))
    sources.append(_make_source(
        tenant_id=tenant_id,
        name="Nr2 dashboard settings and knowledge sync",
        source_type="nr2_runtime",
        location=f"https://dashboard.unboks.org/login?workspace={tenant_id} + tenant runtime DB",
        priority="SOT/company facts",
        text=(
            "Not indexed yet: Nr3 can sync selected Nr2 knowledge, but the full "
            "live prompt-path extraction from tenant runtime DB is not fully mapped here yet."
        ),
        used_in=("WhatsApp", "Email", "Settings", "knowledge files"),
        indexed=False,
        active=True,
    ))
    return tuple(sources)


def _contains(text: str, words: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(word in lower for word in words)


def _languages(text: str) -> set[str]:
    lower = text.lower()
    found = set()
    markers = {
        "spanish": ("spanish", "español", "castellano", "reply in spanish", "responder en español"),
        "english": ("english", "inglés", "reply in english"),
        "dutch": ("dutch", "nederlands"),
        "german": ("german", "deutsch"),
    }
    for lang, values in markers.items():
        if any(value in lower for value in values):
            found.add(lang)
    return found


def _hour_values(text: str) -> set[str]:
    if not _contains(text, ("hour", "hours", "opening", "horario", "abierto", "open")):
        return set()
    return set(re.findall(r"\b(?:[01]?\d|2[0-3])[:.][0-5]\d\b", text))


def _priority_value(source: PromptSource) -> int:
    if _is_platform_safety_lock(source):
        return 0
    return PRIORITY_RANK.get(source.priority, 99)


def _is_platform_safety_lock(source: PromptSource) -> bool:
    if source.source_type != "runtime_code":
        return False
    return _contains(
        source.text,
        (
            "hard refusal rules",
            "not a comedian",
            "jokes",
            "medical advice",
            "legal advice",
            "secrets",
            "tenant isolation",
        ),
    )


def _winner(a: PromptSource, b: PromptSource) -> PromptSource:
    if _priority_value(a) < _priority_value(b):
        return a
    if _priority_value(b) < _priority_value(a):
        return b
    return a


def _conflict(
    tenant_id: str,
    severity: str,
    category: str,
    title: str,
    a: PromptSource,
    b: PromptSource,
    why: str,
    recommended_fix: str,
) -> PromptConflict:
    winner = _winner(a, b)
    cid = _hash(tenant_id, category, a.id, b.id, title)
    state = _conflict_state(cid)
    return PromptConflict(
        id=cid,
        tenant_id=tenant_id,
        severity=severity,
        category=category,
        title=title,
        source_a=a,
        source_b=b,
        instruction_a=a.text,
        instruction_b=b.text,
        winner=f"{winner.name} ({winner.priority})",
        why=why,
        recommended_fix=recommended_fix,
        ignored=bool(state.get("ignored")),
        resolved=bool(state.get("resolved")),
    )


def detect_conflicts(tenant_id: str, sources: tuple[PromptSource, ...]) -> tuple[PromptConflict, ...]:
    active = [s for s in sources if s.active and s.indexed and s.text.strip()]
    conflicts: list[PromptConflict] = []
    for i, a in enumerate(active):
        for b in active[i + 1:]:
            la = _languages(a.text)
            lb = _languages(b.text)
            if la and lb and la.isdisjoint(lb):
                conflicts.append(_conflict(
                    tenant_id,
                    "Warning",
                    "language",
                    "Language instructions disagree",
                    a,
                    b,
                    "Customers may receive replies in the wrong language.",
                    "Set one language rule as authoritative or remove the stale language instruction.",
                ))
            a_fun = _contains(a.text, ("be funny", "tell jokes", "joke", "humor", "comedic", "entertain"))
            b_fun = _contains(b.text, ("be funny", "tell jokes", "joke", "humor", "comedic", "entertain"))
            a_no_fun = _contains(a.text, ("no jokes", "never tell jokes", "may not tell jokes", "not a comedian", "off-topic"))
            b_no_fun = _contains(b.text, ("no jokes", "never tell jokes", "may not tell jokes", "not a comedian", "off-topic"))
            if (a_fun and b_no_fun) or (b_fun and a_no_fun):
                conflicts.append(_conflict(
                    tenant_id,
                    "Critical",
                    "forbidden_behavior",
                    "Humor/off-topic rule conflict",
                    a,
                    b,
                    "Safety rules say Marina must not entertain off-topic or joke requests.",
                    "Keep the safety lock. Remove or rewrite the lower-priority funny/entertainment instruction.",
                ))
            a_advice = _contains(a.text, ("give clinical advice", "give legal advice", "diagnose", "prescribe", "financial advice"))
            b_advice = _contains(b.text, ("give clinical advice", "give legal advice", "diagnose", "prescribe", "financial advice"))
            a_no_advice = _contains(a.text, ("no clinical advice", "no legal advice", "do not diagnose", "never give clinical", "avoid specific legal advice", "do not prescribe"))
            b_no_advice = _contains(b.text, ("no clinical advice", "no legal advice", "do not diagnose", "never give clinical", "avoid specific legal advice", "do not prescribe"))
            if (a_advice and b_no_advice) or (b_advice and a_no_advice):
                conflicts.append(_conflict(
                    tenant_id,
                    "Critical",
                    "safety_compliance",
                    "Advice safety conflict",
                    a,
                    b,
                    "Advice contradictions can cause unsafe medical, clinical, legal, or financial replies.",
                    "Safety/compliance wins. Rewrite tenant prompt to provide intake and general information only.",
                ))
            a_answer = _contains(a.text, ("always answer", "answer all", "never escalate", "do not escalate"))
            b_answer = _contains(b.text, ("always answer", "answer all", "never escalate", "do not escalate"))
            a_escalate = _contains(a.text, ("escalate", "human takeover", "handoff", "pass to human"))
            b_escalate = _contains(b.text, ("escalate", "human takeover", "handoff", "pass to human"))
            if (a_answer and b_escalate) or (b_answer and a_escalate):
                conflicts.append(_conflict(
                    tenant_id,
                    "Warning",
                    "escalation",
                    "Answer-vs-escalate conflict",
                    a,
                    b,
                    "Marina may answer when she should hand off, or hand off when she should help.",
                    "Define exact topics that must escalate and remove broad 'always answer' wording.",
                ))
            a_warm = _contains(a.text, ("warm", "friendly", "casual", "playful"))
            b_warm = _contains(b.text, ("warm", "friendly", "casual", "playful"))
            a_strict = _contains(a.text, ("strict", "formal", "direct", "professional", "authoritative"))
            b_strict = _contains(b.text, ("strict", "formal", "direct", "professional", "authoritative"))
            if (a_warm and b_strict) or (b_warm and a_strict):
                conflicts.append(_conflict(
                    tenant_id,
                    "Info",
                    "tone",
                    "Tone instructions pull in different directions",
                    a,
                    b,
                    "This can make replies feel inconsistent between messages or channels.",
                    "Choose the tenant tone in Nr3 and keep older/default tone text soft or removed.",
                ))
            hours_a = _hour_values(a.text)
            hours_b = _hour_values(b.text)
            if hours_a and hours_b and hours_a != hours_b:
                conflicts.append(_conflict(
                    tenant_id,
                    "Warning",
                    "business_facts",
                    "Opening-hour facts may disagree",
                    a,
                    b,
                    "Business fact conflicts can cause wrong customer promises.",
                    "Keep opening hours in one SOT entry and remove stale hours from other prompts.",
                ))
    return tuple(conflicts)


def effective_prompt_preview(sources: tuple[PromptSource, ...]) -> tuple[tuple[PromptSource, ...], tuple[PromptSource, ...], tuple[str, ...]]:
    active = sorted(
        [source for source in sources if source.active],
        key=lambda source: (_priority_value(source), source.name),
    )
    suppressed = tuple(source for source in sources if source.disabled or not source.active)
    missing = []
    text = "\n".join(source.text.lower() for source in active)
    if "no jokes" not in text and "not a comedian" not in text and "joke" not in text:
        missing.append("Safety lock: no jokes/off-topic entertainment was not found in indexed sources.")
    if "secret" not in text and "internal" not in text:
        missing.append("Safety lock: no secrets/internal-system-info rule was not found in indexed sources.")
    if not any(_languages(source.text) for source in active):
        missing.append("Language rule is missing from indexed sources.")
    return tuple(active), suppressed, tuple(missing)


def audit_tenant_prompts(tenant_id: str) -> PromptAudit:
    if get_tenant(tenant_id) is None:
        raise ValueError("Tenant not found.")
    sources = prompt_sources_for_tenant(tenant_id)
    conflicts = tuple(
        conflict for conflict in detect_conflicts(tenant_id, sources)
        if not conflict.resolved
    )
    effective, suppressed, missing = effective_prompt_preview(sources)
    return PromptAudit(
        tenant_id=tenant_id,
        sources=sources,
        conflicts=conflicts,
        effective_rules=effective,
        suppressed_rules=suppressed,
        missing_required_rules=missing,
        not_indexed_sources=tuple(source for source in sources if not source.indexed),
    )


def validate_prompt_change(tenant_id: str, new_source: PromptSource) -> tuple[PromptConflict, ...]:
    sources = prompt_sources_for_tenant(tenant_id) + (new_source,)
    return tuple(
        conflict for conflict in detect_conflicts(tenant_id, sources)
        if conflict.severity == "Critical" and not conflict.resolved and not conflict.ignored
    )


def make_pending_source(
    tenant_id: str,
    *,
    name: str,
    location: str,
    priority: str,
    text: str,
    used_in: tuple[str, ...],
) -> PromptSource:
    return _make_source(
        tenant_id=tenant_id,
        name=name,
        source_type="pending_edit",
        location=location,
        priority=priority,
        text=text,
        used_in=used_in,
        active=True,
        indexed=True,
        editable=True,
    )


def platform_prompt_audit() -> dict[str, PromptAudit]:
    return {tenant.id: audit_tenant_prompts(tenant.id) for tenant in list_tenants()}
