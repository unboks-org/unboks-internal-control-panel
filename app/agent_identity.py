"""Validation for tenant AI Agent display names in Nr3."""

from __future__ import annotations

import re


DEFAULT_AGENT_NAME = "Marina"
_URL_RE = re.compile(r"https?://|www\.|[a-z0-9-]+\.[a-z]{2,}", re.I)
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF\U00002700-\U000027BF\U00002600-\U000026FF]")
_UNSAFE_TERMS = (
    "human support",
    "doctor",
    "dr.",
    "dr ",
    "lawyer",
    "attorney",
    "therapist",
    "psychologist",
    "official meta support",
    "meta support",
    "system",
    "admin",
)


def clean_agent_name(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().split())


def validate_agent_name(value: str | None) -> str:
    name = clean_agent_name(value)
    if not name:
        raise ValueError("AI Agent name is required.")
    if len(name) > 40:
        raise ValueError("AI Agent name must be 40 characters or fewer.")
    if _URL_RE.search(name):
        raise ValueError("AI Agent name cannot contain a URL.")
    if _EMOJI_RE.search(name):
        raise ValueError("AI Agent name cannot contain emojis.")
    lowered = name.lower()
    if any(term in lowered for term in _UNSAFE_TERMS):
        raise ValueError("Choose a name that does not imply a human role or professional license.")
    return name

