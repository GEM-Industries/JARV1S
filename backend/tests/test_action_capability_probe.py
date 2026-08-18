from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.llm.types import ChatResult, ToolCallEvent, assistant_tool_message
from core.setup.validation import probe_action_capability
from core.setup.runtime import JarvisRuntime
from tests.test_setup_helpers import _cloud_config


@pytest.mark.asyncio
async def test_probe_live_round_trip_succeeds():
    call = ToolCallEvent(call_id="c1", name="system__think", arguments={"thought": "probe"})

    class FakeLLM:
        model = "gpt-test"
        calls = 0

        async def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ChatResult(
                    text="",
                    tool_calls=(call,),
                    message=assistant_tool_message("", (call,)),
                )
            return ChatResult(text="ok", message={"role": "assistant", "content": "ok"})

    llm = FakeLLM()
    assert await probe_action_capability(llm) is True
    assert llm.calls == 2


@pytest.mark.asyncio
async def test_probe_live_round_trip_fails_without_tool_call():
    class FakeLLM:
        model = "gpt-test"

        async def complete(self, **_kwargs):
            return ChatResult(text="I cannot call tools.", message={"role": "assistant", "content": "no"})

    assert await probe_action_capability(FakeLLM()) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("previous", [None, False])
async def test_runtime_probes_when_action_capable_is_not_proven(monkeypatch, previous):
    runtime = JarvisRuntime()
    llm = SimpleNamespace(_client=object(), is_initialized=True, configure=lambda **_: None)
    tool_router = SimpleNamespace(initialize=AsyncMock())
    saved = {}

    async def initialize_llm_component():
        return None

    async def fake_save(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(
        "core.setup.runtime.resolve_llm_config",
        AsyncMock(return_value=_cloud_config(action_capable=previous)),
    )
    monkeypatch.setattr("api.websockets.handlers.llm", llm)
    monkeypatch.setattr("api.websockets.handlers.initialize_llm_component", initialize_llm_component)
    monkeypatch.setattr("core.tool_router.tool_router", tool_router)
    monkeypatch.setattr(runtime, "_initialize_background_llm", AsyncMock(return_value=None))
    monkeypatch.setattr(runtime, "_prewarm_optional_voice_output", MagicMock())
    monkeypatch.setattr("core.setup.validation.probe_action_capability", AsyncMock(return_value=True))
    monkeypatch.setattr("core.setup.runtime.llm_config_store.save", fake_save)
    monkeypatch.setattr(
        "core.integrations.manager.integrations.unregister",
        AsyncMock(),
    )

    assert await runtime.initialize_if_ready()
    assert saved["action_capable"] is True
