from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Iterable


CATEGORIES = (
    "general",
    "products",
    "pricing",
    "ordering",
    "delivery",
    "payment",
    "tone",
    "escalation",
    "policy",
    "ingredients",
    "availability",
    "contact",
)

MODEL_USED = "deterministic-sot-parser-v1"
MAX_TEXT_CHARS = 120_000
MAX_ENTRIES = 80


_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("payment", ("payment", "pay", "cash", "card", "bank", "transfer", "invoice", "paid")),
    ("pricing", ("price", "pricing", "cost", "subtotal", "total", "fee", "tariff", "rate", "$", "€", "ang")),
    ("ordering", ("order", "ordering", "checkout", "minimum", "quantity", "cart")),
    ("delivery", ("delivery", "deliver", "pickup", "address", "shipping", "drop-off", "drop off")),
    ("ingredients", ("ingredient", "allergen", "gluten", "dairy", "nuts", "vegan", "sugar")),
    ("availability", ("available", "availability", "stock", "hours", "open", "closed", "schedule")),
    ("contact", ("contact", "phone", "email", "whatsapp", "website", "address")),
    ("tone", ("tone", "style", "friendly", "formal", "reply", "voice")),
    ("escalation", ("escalate", "handover", "human", "operator", "urgent", "complaint")),
    ("policy", ("policy", "refund", "cancel", "privacy", "terms", "rule", "requirement")),
    ("products", ("product", "menu", "item", "cupcake", "cake", "service", "offer")),
)

_VAGUE_TITLES = {"note", "notes", "info", "information", "misc", "other", "general"}


@dataclass(frozen=True)
class ExistingSotEntry:
    title: str
    content: str


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.lower())).strip()


def _compact(value: str, limit: int = 700) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _title_case(value: str) -> str:
    words = re.sub(r"[_\-]+", " ", value).strip()
    words = re.sub(r"\s+", " ", words)
    if not words:
        return "Source of Truth"
    if len(words) > 72:
        words = words[:72].rsplit(" ", 1)[0]
    return words[:1].upper() + words[1:]


def _guess_category(title: str, fact: str) -> str:
    haystack = f"{title} {fact}".lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in haystack for keyword in keywords):
            return category
    return "general"


def _source_excerpt(text: str) -> str:
    return _compact(text, 220)


def _existing_entries(entries: Iterable[dict]) -> list[ExistingSotEntry]:
    out: list[ExistingSotEntry] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        out.append(
            ExistingSotEntry(
                title=str(entry.get("title") or ""),
                content=str(entry.get("content") or entry.get("fact") or ""),
            )
        )
    return out


def _duplicate_of(title: str, fact: str, existing: list[ExistingSotEntry]) -> str:
    candidate = _normalize(f"{title} {fact}")
    fact_norm = _normalize(fact)
    if not candidate:
        return ""
    for entry in existing:
        entry_norm = _normalize(f"{entry.title} {entry.content}")
        entry_fact = _normalize(entry.content)
        if not entry_norm:
            continue
        if candidate == entry_norm or fact_norm == entry_fact:
            return entry.title or "Existing SOT entry"
        if SequenceMatcher(None, fact_norm, entry_fact).ratio() >= 0.88:
            return entry.title or "Existing SOT entry"
    return ""


def _entry(
    *,
    title: str,
    fact: str,
    source: str,
    confidence: float,
    existing: list[ExistingSotEntry],
) -> dict | None:
    title = _title_case(title)
    fact = _compact(fact)
    if len(_normalize(fact)) < 12:
        return None
    if _normalize(title) in _VAGUE_TITLES and len(fact.split()) < 8:
        return None
    duplicate = _duplicate_of(title, fact, existing)
    return {
        "title": title,
        "category": _guess_category(title, fact),
        "fact": fact,
        "confidence": round(max(0.25, min(confidence, 0.98)), 2),
        "sourceExcerpt": _source_excerpt(source),
        "possibleDuplicate": bool(duplicate),
        "duplicateOf": duplicate,
        "selected": not bool(duplicate),
    }


def _looks_like_heading(line: str) -> bool:
    stripped = line.strip(" -:\t")
    if not stripped or len(stripped) > 90:
        return False
    if stripped.endswith("."):
        return False
    if len(stripped.split()) <= 7 and not re.search(r"[.!?]", stripped):
        return True
    return bool(re.match(r"^[A-Z][A-Za-z0-9 /&-]{2,60}:?$", stripped))


def _paragraphs(text: str) -> list[str]:
    blocks = [block.strip() for block in re.split(r"\n\s*\n+", text) if block.strip()]
    if blocks:
        return blocks
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_candidates(text: str, existing: list[ExistingSotEntry]) -> list[dict]:
    entries: list[dict] = []
    current_heading = ""
    current_bullets: list[str] = []

    def flush_bullets() -> None:
        nonlocal current_bullets
        if current_heading and current_bullets:
            fact = " ".join(current_bullets)
            item = _entry(
                title=current_heading,
                fact=fact,
                source=f"{current_heading}: {fact}",
                confidence=0.84,
                existing=existing,
            )
            if item:
                entries.append(item)
        current_bullets = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            flush_bullets()
            continue
        bullet_match = re.match(r"^(?:[-*•]|\d+[.)])\s+(.+)$", line)
        key_value = re.match(r"^([^:]{2,80}):\s*(.{2,})$", line)
        dash_value = re.match(r"^([A-Za-z][A-Za-z0-9 /&-]{2,80})\s+-\s+(.{2,})$", line)

        if key_value:
            flush_bullets()
            title, fact = key_value.group(1), key_value.group(2)
            item = _entry(
                title=title,
                fact=fact,
                source=line,
                confidence=0.93,
                existing=existing,
            )
            if item:
                entries.append(item)
            continue

        if dash_value:
            flush_bullets()
            title, fact = dash_value.group(1), dash_value.group(2)
            item = _entry(
                title=title,
                fact=fact,
                source=line,
                confidence=0.88,
                existing=existing,
            )
            if item:
                entries.append(item)
            continue

        if bullet_match and current_heading:
            current_bullets.append(bullet_match.group(1).strip())
            continue

        if _looks_like_heading(line):
            flush_bullets()
            current_heading = line.strip(" :")
            continue

        flush_bullets()
        item = _entry(
            title=line.split(". ", 1)[0],
            fact=line,
            source=line,
            confidence=0.66,
            existing=existing,
        )
        if item:
            entries.append(item)
    flush_bullets()
    return entries


def extract_sot_entries(text: str, existing_entries: Iterable[dict] = ()) -> dict:
    clean_text = (text or "").replace("\x00", "").strip()
    if not clean_text:
        raise ValueError("Upload a .txt file with business information first.")
    if len(clean_text) > MAX_TEXT_CHARS:
        clean_text = clean_text[:MAX_TEXT_CHARS]

    existing = _existing_entries(existing_entries)
    candidates = _extract_candidates(clean_text, existing)
    if len(candidates) < 2:
        for paragraph in _paragraphs(clean_text):
            item = _entry(
                title=paragraph.split(". ", 1)[0],
                fact=paragraph,
                source=paragraph,
                confidence=0.62,
                existing=existing,
            )
            if item:
                candidates.append(item)

    seen: set[str] = set()
    entries: list[dict] = []
    for candidate in candidates:
        fingerprint = _normalize(f"{candidate['title']} {candidate['fact']}")
        fact_only = _normalize(candidate["fact"])
        if not fingerprint or fingerprint in seen or fact_only in seen:
            continue
        seen.add(fingerprint)
        seen.add(fact_only)
        entries.append(candidate)
        if len(entries) >= MAX_ENTRIES:
            break

    return {
        "modelUsed": MODEL_USED,
        "categories": list(CATEGORIES),
        "entries": entries,
        "limitations": [
            "Initial release supports .txt files only.",
            "Extraction is deterministic and uses only text present in the uploaded file.",
            "Low-confidence or duplicate entries should be reviewed before saving.",
        ],
    }


def duplicate_title(title: str, fact: str, existing_entries: Iterable[dict]) -> str:
    """Return the matching existing SOT title, if the candidate is a duplicate."""
    return _duplicate_of(title, fact, _existing_entries(existing_entries))
