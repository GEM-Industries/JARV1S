import asyncio
from types import SimpleNamespace

import pytest

from core.llm.adapters.litellm import LiteLLMAdapter
from core.llm.service import LLMService, LLMStreamIdleTimeoutError
from core.llm.types import ReasoningEvent, TextEvent, ToolCallEvent


class FakeAdapterStream:
    def __init__(self, chunks, *, delay_s: float = 0.0):
        self._chunks = list(chunks)
        self._delay_s = delay_s
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def aclose(self):
        self.closed = True


class DelayedAdapterStream:
    def __init__(self, chunks, delays):
        self._chunks = list(chunks)
        self._delays = list(delays)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        delay = self._delays.pop(0) if self._delays else 0.0
        if delay:
            await asyncio.sleep(delay)
        return self._chunks.pop(0)

    async def aclose(self):
        self.closed = True


class FakeAdapter:
    def __init__(self, streams):
        self.streams = list(streams)

    def supports_reasoning_effort(self):
        return False

    async def chat(self, **_kwargs):
        from core.llm.types import ChatResult

        return ChatResult(text="ok", message={"role": "assistant", "content": "ok"})

    def chat_stream(self, **_kwargs):
        return self.streams.pop(0)


def _litellm_chunk(*, content: str | None = None, reasoning: str | None = None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning,
                ),
            )
        ]
    )


@pytest.mark.asyncio
async def test_chat_stream_retries_when_first_token_times_out():
    slow = FakeAdapterStream([TextEvent(text="too late")], delay_s=0.05)
    fast = FakeAdapterStream([TextEvent(text="ok")])
    service = LLMService(
        first_token_timeout_s=0.01,
        first_token_retries=1,
        adapter=FakeAdapter([slow, fast]),
    )

    events = []
    chunks = [
        chunk
        async for chunk in service.chat_stream(
            user_message="hello",
            on_stream_event=events.append,
        )
    ]

    assert chunks == [TextEvent(text="ok")]
    assert slow.closed is True
    assert [(event.status, event.attempt) for event in events] == [
        ("waiting", 1),
        ("timeout", 1),
        ("retrying", 2),
        ("waiting", 2),
        ("first_token", 2),
    ]
    assert events[-1].retry_count == 1


@pytest.mark.asyncio
async def test_chat_stream_times_out_when_stream_stalls_after_first_token():
    stream = DelayedAdapterStream(
        [TextEvent(text="first"), TextEvent(text="too late")],
        delays=[0.0, 0.05],
    )
    service = LLMService(
        first_token_timeout_s=0.1,
        stream_idle_timeout_s=0.01,
        adapter=FakeAdapter([stream]),
    )

    events = []
    gen = service.chat_stream(
        user_message="hello",
        on_stream_event=events.append,
    )

    assert await anext(gen) == TextEvent(text="first")
    with pytest.raises(LLMStreamIdleTimeoutError):
        await anext(gen)

    assert stream.closed is True
    assert [(event.status, event.attempt) for event in events] == [
        ("waiting", 1),
        ("first_token", 1),
        ("stream_idle_timeout", 1),
    ]
    assert events[-1].timeout_ms == 10


@pytest.mark.asyncio
async def test_litellm_adapter_yields_reasoning_and_text(monkeypatch):
    async def fake_acompletion(**kwargs):
        assert kwargs["model"] == "anthropic/claude-sonnet-4-6"
        assert kwargs["reasoning_effort"] == "low"
        assert "thinking" not in kwargs
        assert "temperature" not in kwargs

        async def stream():
            yield _litellm_chunk(reasoning="check ")
            yield _litellm_chunk(content="done")

        return stream()

    monkeypatch.setattr("core.llm.adapters.litellm.litellm.acompletion", fake_acompletion)
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-6",
        provider_name="anthropic",
        timeout_s=None,
    )

    chunks = [
        chunk
        async for chunk in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.7,
            max_tokens=128,
            reasoning_effort="low",
        )
    ]

    assert chunks == [
        ReasoningEvent(text="check "),
        TextEvent(text="done"),
    ]


def test_litellm_adapter_reports_reasoning_effort_capability(monkeypatch):
    monkeypatch.setattr(
        "core.llm.adapters.litellm.litellm.get_supported_openai_params",
        lambda **_kwargs: ["stream", "reasoning_effort"],
    )
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        model="claude-opus-4-8",
        provider_name="anthropic",
        timeout_s=None,
    )

    assert adapter.supports_reasoning_effort() is True


def test_litellm_adapter_rejects_unknown_reasoning_effort_capability(monkeypatch):
    monkeypatch.setattr(
        "core.llm.adapters.litellm.litellm.get_supported_openai_params",
        lambda **_kwargs: ["stream"],
    )
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="unknown-model",
        provider_name="custom",
        timeout_s=None,
    )

    assert adapter.supports_reasoning_effort() is False


def test_managed_ollama_port_infers_ollama_provider():
    from core.llm.providers import infer_provider_from_base_url

    assert infer_provider_from_base_url("http://127.0.0.1:11435/v1") == "ollama"
    assert infer_provider_from_base_url("http://127.0.0.1:11434/v1") == "ollama"


def test_ollama_adapter_prefixes_model_and_strips_v1_api_base():
    adapter = LiteLLMAdapter(
        api_key="local",
        base_url="http://127.0.0.1:11435/v1",
        model="gemma4:12b-mlx",
        provider_name="ollama",
        timeout_s=None,
    )

    assert adapter.model == "ollama_chat/gemma4:12b-mlx"
    assert adapter._kwargs()["api_base"] == "http://127.0.0.1:11435"


def test_google_ai_studio_adapter_uses_native_gemini_without_openai_api_base():
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        model="gemini-3.5-flash",
        provider_name="google-ai-studio",
        timeout_s=None,
    )

    assert adapter.model == "gemini/gemini-3.5-flash"
    assert "api_base" not in adapter._kwargs()


def test_google_ai_studio_adapter_keeps_custom_api_base():
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://gateway.example/gemini",
        model="gemini-3.5-flash",
        provider_name="google-ai-studio",
        timeout_s=None,
    )

    assert adapter._kwargs()["api_base"] == "https://gateway.example/gemini"


def test_anthropic_adapter_omits_default_api_base():
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://api.anthropic.com/v1",
        model="claude-sonnet-4-6",
        provider_name="anthropic",
        timeout_s=None,
    )

    assert adapter.model == "anthropic/claude-sonnet-4-6"
    assert "api_base" not in adapter._kwargs()


def test_configure_updates_provider_name_used_for_inference():
    service = LLMService()
    service.configure(
        api_key="local",
        base_url="http://127.0.0.1:11435/v1",
        model="gemma4:12b-mlx",
        provider_name="ollama",
    )
    assert service.provider_name == "ollama"


def test_local_provider_uses_extended_first_token_timeout():
    service = LLMService(
        api_key="local",
        base_url="http://127.0.0.1:11435/v1",
        model="gemma4:12b-mlx",
        provider_name="ollama",
    )
    assert service._resolve_first_token_timeout_s(None) == 90.0
    assert service._resolve_idle_timeout_s(None) == 90.0


def test_cloud_provider_keeps_short_first_token_timeout():
    service = LLMService(
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        model="google/gemma-4-26b-a4b-it",
        provider_name="openrouter",
    )
    assert service._resolve_first_token_timeout_s(None) == 7.0
    assert service._resolve_idle_timeout_s(None) == 30.0


def test_explicit_timeout_override_wins_for_local_provider():
    service = LLMService(
        api_key="local",
        base_url="http://127.0.0.1:11435/v1",
        model="gemma4:12b-mlx",
        provider_name="ollama",
        first_token_timeout_s=12.0,
        stream_idle_timeout_s=15.0,
    )
    assert service._resolve_first_token_timeout_s(None) == 12.0
    assert service._resolve_first_token_timeout_s(3.0) == 3.0
    assert service._resolve_idle_timeout_s(None) == 15.0


def test_ollama_disables_default_thinking_but_preserves_explicit_effort():
    adapter = LiteLLMAdapter(
        api_key="local",
        base_url="http://127.0.0.1:11435/v1",
        model="gemma4:12b-mlx",
        provider_name="ollama",
        timeout_s=None,
    )
    assert adapter._provider_params(reasoning_effort=None) == {"reasoning_effort": "none"}
    assert adapter._provider_params(reasoning_effort="low") == {"reasoning_effort": "low"}
    assert adapter._provider_params(reasoning_effort="medium") == {"reasoning_effort": "medium"}
    assert adapter._provider_params(reasoning_effort="high") == {"reasoning_effort": "high"}


def _litellm_tool_chunk(*, index=0, call_id=None, name=None, arguments=""):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=None,
                    reasoning_content=None,
                    tool_calls=[
                        SimpleNamespace(
                            index=index,
                            id=call_id,
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                ),
            )
        ]
    )


@pytest.mark.asyncio
async def test_litellm_adapter_assembles_fragmented_and_multiple_tool_calls(monkeypatch):
    async def fake_acompletion(**kwargs):
        assert kwargs["tools"] == [{"type": "function", "function": {"name": "system__think"}}]

        async def stream():
            yield _litellm_tool_chunk(index=0, call_id="c1", name="weather__get", arguments='{"city":')
            yield _litellm_tool_chunk(index=0, arguments='"Sydney"}')
            yield _litellm_tool_chunk(index=1, call_id="c2", name="system__think", arguments='{"thought":"ok"}')

        return stream()

    monkeypatch.setattr("core.llm.adapters.litellm.litellm.acompletion", fake_acompletion)
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="google/gemma-4-26b-a4b-it",
        provider_name="openrouter",
        timeout_s=None,
    )

    from core.llm.types import ToolCallStarted

    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.0,
            max_tokens=128,
            tools=[{"type": "function", "function": {"name": "system__think"}}],
        )
    ]

    assert isinstance(events[0], ToolCallStarted)
    calls = [event for event in events if isinstance(event, ToolCallEvent)]
    assert calls[0].call_id == "c1"
    assert calls[0].name == "weather__get"
    assert calls[0].arguments == {"city": "Sydney"}
    assert calls[1].call_id == "c2"
    assert calls[1].arguments == {"thought": "ok"}


@pytest.mark.asyncio
async def test_litellm_adapter_marks_malformed_tool_arguments(monkeypatch):
    async def fake_acompletion(**_kwargs):
        async def stream():
            yield _litellm_tool_chunk(index=0, call_id="c1", name="system__think", arguments="{not-json")

        return stream()

    monkeypatch.setattr("core.llm.adapters.litellm.litellm.acompletion", fake_acompletion)
    adapter = LiteLLMAdapter(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="google/gemma-4-26b-a4b-it",
        provider_name="openrouter",
        timeout_s=None,
    )

    events = [
        event
        async for event in adapter.chat_stream(
            messages=[{"role": "user", "content": "hello"}],
            temperature=0.0,
            max_tokens=32,
        )
    ]
    call = next(event for event in events if isinstance(event, ToolCallEvent))
    assert call.arguments["__parse_error__"] == "{not-json"


def test_cloud_gemma4_still_uses_extra_body_none_when_effort_unset():
    adapter = LiteLLMAdapter(
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
        model="google/gemma-4-26b-a4b-it",
        provider_name="openrouter",
        timeout_s=None,
    )
    assert adapter._provider_params(reasoning_effort=None) == {
        "extra_body": {"reasoning_effort": "none"},
    }
    assert adapter._provider_params(reasoning_effort="low") == {"reasoning_effort": "low"}

