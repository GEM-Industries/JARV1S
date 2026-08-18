from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.setup.llm_config import LlmConfigSource, LOCAL_DUMMY_API_KEY, ResolvedLlmConfig
from core.setup.runtime import JarvisRuntime
from tests.test_setup_helpers import _cloud_config


@pytest.mark.asyncio
async def test_runtime_prewarms_optional_voice_output_after_core_ready(monkeypatch):
    runtime = JarvisRuntime()
    llm = SimpleNamespace(_client=object(), is_initialized=True, configure=lambda **_: None)
    tool_router = SimpleNamespace(initialize=AsyncMock())
    prewarm = MagicMock()
    unregister_calls: list[str] = []

    async def initialize_llm_component():
        return None

    monkeypatch.setattr("core.setup.runtime.resolve_llm_config", AsyncMock(return_value=_cloud_config()))
    monkeypatch.setattr("api.websockets.handlers.llm", llm)
    monkeypatch.setattr("api.websockets.handlers.initialize_llm_component", initialize_llm_component)
    monkeypatch.setattr("core.tool_router.tool_router", tool_router)
    monkeypatch.setattr(runtime, "_initialize_background_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_prewarm_optional_voice_output", prewarm)
    monkeypatch.setattr(
        "core.integrations.manager.integrations.unregister",
        AsyncMock(side_effect=lambda name: unregister_calls.append(name)),
    )

    assert await runtime.initialize_if_ready()
    assert runtime.core_ready is True
    prewarm.assert_called_once()
    tool_router.initialize.assert_awaited_once_with(llm_service=llm)
    assert unregister_calls == ["background_agent"]


@pytest.mark.asyncio
async def test_runtime_stays_not_ready_when_router_index_fails(monkeypatch):
    runtime = JarvisRuntime()
    llm = SimpleNamespace(_client=object(), is_initialized=True, configure=lambda **_: None)
    tool_router = SimpleNamespace(
        initialize=AsyncMock(side_effect=RuntimeError("semantic routing index")),
    )

    async def initialize_llm_component():
        return None

    monkeypatch.setattr("core.setup.runtime.resolve_llm_config", AsyncMock(return_value=_cloud_config()))
    monkeypatch.setattr("api.websockets.handlers.llm", llm)
    monkeypatch.setattr("api.websockets.handlers.initialize_llm_component", initialize_llm_component)
    monkeypatch.setattr("core.tool_router.tool_router", tool_router)
    monkeypatch.setattr(runtime, "_initialize_background_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_prewarm_optional_voice_output", MagicMock())

    assert await runtime.initialize_if_ready() is False
    assert runtime.core_ready is False
    assert "semantic routing index" in (runtime.last_error or "")


@pytest.mark.asyncio
async def test_runtime_applies_full_resolved_llm_config(monkeypatch):
    """Provider must travel with model/base_url — LiteLLM rejects bare local model ids."""
    runtime = JarvisRuntime()
    captured: dict[str, object] = {}

    def configure(**kwargs):
        captured.update(kwargs)

    llm = SimpleNamespace(is_initialized=True, configure=configure)
    tool_router = SimpleNamespace(initialize=AsyncMock())

    async def initialize_llm_component():
        return None

    config = ResolvedLlmConfig(
        provider="ollama",
        model="gemma4:12b-mlx",
        base_url="http://127.0.0.1:11435/v1",
        requires_api_key=False,
        api_key=LOCAL_DUMMY_API_KEY,
        source=LlmConfigSource.PERSISTED,
        action_capable=True,
    )
    monkeypatch.setattr("core.setup.runtime.resolve_llm_config", AsyncMock(return_value=config))
    monkeypatch.setattr("api.websockets.handlers.llm", llm)
    monkeypatch.setattr("api.websockets.handlers.initialize_llm_component", initialize_llm_component)
    monkeypatch.setattr("core.tool_router.tool_router", tool_router)
    monkeypatch.setattr(runtime, "_initialize_background_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_prewarm_optional_voice_output", MagicMock())
    monkeypatch.setattr(
        "core.integrations.manager.integrations.unregister",
        AsyncMock(),
    )

    assert await runtime.initialize_if_ready()
    assert captured == {
        "api_key": LOCAL_DUMMY_API_KEY,
        "base_url": "http://127.0.0.1:11435/v1",
        "model": "gemma4:12b-mlx",
        "provider_name": "ollama",
    }


def test_resolved_llm_config_apply_to_includes_provider():
    captured: dict[str, object] = {}

    class FakeLlm:
        def configure(self, **kwargs):
            captured.update(kwargs)

    ResolvedLlmConfig(
        provider="ollama",
        model="gemma4:12b-mlx",
        base_url="http://127.0.0.1:11435/v1",
        requires_api_key=False,
        api_key=LOCAL_DUMMY_API_KEY,
        source=LlmConfigSource.PERSISTED,
    ).apply_to(FakeLlm())

    assert captured["provider_name"] == "ollama"
    assert captured["model"] == "gemma4:12b-mlx"


@pytest.mark.asyncio
async def test_background_llm_uses_anthropic_key(monkeypatch):
    runtime = JarvisRuntime()
    captured: dict[str, str] = {}

    class FakeLLMService:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def initialize(self):
            return None

    monkeypatch.setattr(
        "core.setup.runtime.credential_store.get_stored_secret",
        lambda name: "sk-ant-background" if name == "ANTHROPIC_API_KEY" else None,
    )
    monkeypatch.setattr("core.llm.service.LLMService", FakeLLMService)

    background_llm = await runtime._initialize_background_llm()

    assert background_llm is not None
    assert captured["api_key"] == "sk-ant-background"
    assert captured["model"] == "claude-opus-4-8"
    assert captured["provider_name"] == "anthropic"


@pytest.mark.asyncio
async def test_background_llm_ignores_environment_key(monkeypatch):
    runtime = JarvisRuntime()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env-only")
    monkeypatch.setattr(
        "core.setup.runtime.credential_store.get_stored_secret",
        lambda _name: None,
    )

    assert await runtime._initialize_background_llm() is None


@pytest.mark.asyncio
async def test_background_llm_failure_does_not_raise(monkeypatch):
    runtime = JarvisRuntime()

    class FakeLLMService:
        def __init__(self, **_kwargs):
            pass

        async def initialize(self):
            raise RuntimeError("bad background key")

    monkeypatch.setattr(
        "core.setup.runtime.credential_store.get_stored_secret",
        lambda name: "sk-ant-bad" if name == "ANTHROPIC_API_KEY" else None,
    )
    monkeypatch.setattr("core.llm.service.LLMService", FakeLLMService)

    background_llm = await runtime._initialize_background_llm()

    assert background_llm is None
    assert runtime.background_agent_ready is False
    assert "bad background key" in (runtime.background_agent_last_error or "")
