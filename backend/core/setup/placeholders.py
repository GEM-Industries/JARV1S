"""Detect placeholder API keys that should be treated as missing."""

import re

_PLACEHOLDER_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^your[_-]", re.I),
    re.compile(r"[_-]here$", re.I),
    re.compile(r"^sk-ant-\.{3}$", re.I),
    re.compile(r"^changeme$", re.I),
    re.compile(r"^placeholder$", re.I),
    re.compile(r"^example$", re.I),
)

_KNOWN_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "your_deepinfra_key",
        "your_openrouter_key",
        "your_google_ai_studio_key",
        "your_groq_key",
        "your_together_key",
        "your_custom_key",
        "your_cartesia_key",
        "your_owm_key",
        "your_exa_key",
    }
)


def is_placeholder_api_key(value: str | None) -> bool:
    if not value:
        return True
    stripped = value.strip()
    if not stripped:
        return True
    lowered = stripped.lower()
    if lowered in _KNOWN_PLACEHOLDERS:
        return True
    return any(pattern.search(stripped) for pattern in _PLACEHOLDER_PATTERNS)
