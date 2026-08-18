"""Per-turn reasoning effort resolution from latency contract + model capability."""

from __future__ import annotations

from typing import Protocol

from core.config import settings


class _SupportsReasoningEffort(Protocol):
    @property
    def supports_reasoning_effort(self) -> bool: ...


def resolve_reasoning_effort(
    *,
    audio_bound: bool,
    text_input: bool,
    headless: bool,
    llm: _SupportsReasoningEffort,
) -> str | None:
    """Return provider reasoning_effort for this turn, or None when disabled."""
    if audio_bound or not llm.supports_reasoning_effort:
        return None
    if headless:
        return settings.LLM_HEADLESS_REASONING_EFFORT
    if text_input:
        return settings.LLM_TEXT_REASONING_EFFORT
    return None
