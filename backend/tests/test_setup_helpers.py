import pytest

from core.setup.llm_config import LlmConfigSource, LOCAL_DUMMY_API_KEY, ResolvedLlmConfig


def _cloud_config(*, api_key: str = "sk-valid-key", action_capable: bool | None = True) -> ResolvedLlmConfig:
    return ResolvedLlmConfig(
        provider="openrouter",
        model="google/gemma-4-26b-a4b-it",
        base_url="https://openrouter.ai/api/v1",
        requires_api_key=True,
        api_key=api_key,
        source=LlmConfigSource.PERSISTED,
        action_capable=action_capable,
    )


def _local_config() -> ResolvedLlmConfig:
    return ResolvedLlmConfig(
        provider="ollama",
        model="qwen3:8b",
        base_url="http://127.0.0.1:11434/v1",
        requires_api_key=False,
        api_key=LOCAL_DUMMY_API_KEY,
        source=LlmConfigSource.PERSISTED,
        action_capable=True,
    )


def _unconfigured_config() -> ResolvedLlmConfig:
    return ResolvedLlmConfig(
        provider="openrouter",
        model="",
        base_url="",
        requires_api_key=True,
        api_key=None,
        source=LlmConfigSource.DEFAULT,
    )
