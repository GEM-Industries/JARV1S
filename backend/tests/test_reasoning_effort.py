"""Tests for reasoning effort resolution."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.config import Settings
from core.config import settings
from core.turns.reasoning_effort import resolve_reasoning_effort


def test_resolve_reasoning_effort_audio_bound():
    llm = SimpleNamespace(supports_reasoning_effort=True)
    assert resolve_reasoning_effort(audio_bound=True, text_input=False, headless=False, llm=llm) is None
    assert resolve_reasoning_effort(audio_bound=True, text_input=True, headless=False, llm=llm) is None


def test_resolve_reasoning_effort_text_and_headless(monkeypatch):
    monkeypatch.setattr(settings, "LLM_TEXT_REASONING_EFFORT", "low")
    monkeypatch.setattr(settings, "LLM_HEADLESS_REASONING_EFFORT", "medium")
    supported = SimpleNamespace(supports_reasoning_effort=True)
    unsupported = SimpleNamespace(supports_reasoning_effort=False)
    assert resolve_reasoning_effort(audio_bound=False, text_input=True, headless=False, llm=supported) == "low"
    assert resolve_reasoning_effort(audio_bound=False, text_input=False, headless=True, llm=supported) == "medium"
    assert resolve_reasoning_effort(audio_bound=False, text_input=True, headless=False, llm=unsupported) is None


def test_reasoning_effort_settings_reject_unknown_values():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, LLM_TEXT_REASONING_EFFORT="extreme")
