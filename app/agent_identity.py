"""Validation helpers for tenant AI Agent display names."""
from __future__ import annotations

import re
import unicodedata
from typing import Any


DEFAULT_AGENT_NAME = "Marina"
MAX_AGENT_NAME_LENGTH = 40

_URL_RE = re.compile(r"(https?://|www\.|[a-z0-9-]+\.[a-z]{2,})", re.IGNORECASE)
_BANNED = (
    "human support",
    "doctor",
    "dr.",
    "dr ",
    "lawyer",
    "attorney",
    "advocate",
    "therapist",
    "psychologist",
    "psychiatrist",
    "official meta support",
    "meta support",
    "facebook support",
    "whatsapp support",
    "openai",
    "anthropic",
    "claude",
    "system",
    "admin",
    "root",
)


def clean_agent_name(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def validate_agent_name(value: Any) -> tuple[bool, str, str]:
    name = clean_agent_name(value)
    if not name:
        return False, "", "AI Agent name is required."
    if len(name) > MAX_AGENT_NAME_LENGTH:
        return False, name, "AI Agent name must be 40 characters or less."
    if _URL_RE.search(name):
        return False, name, "AI Agent name cannot contain a URL or domain."
    lowered = name.lower()
    if any(term in lowered for term in _BANNED):
        return False, name, "That name could mislead customers or imply a protected role."
    for char in name:
        category = unicodedata.category(char)
        if category.startswith("S"):
            return False, name, "AI Agent name cannot contain emojis or symbols."
        if category.startswith("C"):
            return False, name, "AI Agent name contains an invalid character."
        if not (char.isalpha() or char.isspace() or char in ".-'"):
            return False, name, "Use letters, spaces, apostrophes, hyphens, or periods only."
    return True, name, ""

