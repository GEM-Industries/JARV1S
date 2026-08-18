from __future__ import annotations

import logging
import os
from typing import Any, AsyncIterator

from core.config import settings
from core.id import generate_id
from core.llm.providers import get_llm_provider
from core.llm.types import (
    ChatResult,
    ModelEvent,
    ReasoningEvent,
    TextEvent,
    ToolCallEvent,
    ToolCallStarted,
    assistant_tool_message,
    parse_tool_arguments,
)

_LITELLM_LOGGERS = ("LiteLLM", "LiteLLM Router", "LiteLLM Proxy")


def _configure_litellm_logging() -> None:
    """Apply JARV1S logging policy before litellm attaches its stderr handler."""
    level = "DEBUG" if settings.LITELLM_VERBOSE_LOGGING else "WARNING"
    os.environ.setdefault("LITELLM_LOG", level)
    if settings.LITELLM_VERBOSE_LOGGING:
        return
    for name in _LITELLM_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


_configure_litellm_logging()

import litellm  # noqa: E402

litellm.modify_params = True
if not settings.LITELLM_VERBOSE_LOGGING:
    litellm.turn_off_message_logging = True


_PROVIDER_MODEL_PREFIXES = {
    "anthropic": "anthropic",
    "deepinfra": "deepinfra",
    "openrouter": "openrouter",
    "groq": "groq",
    "together": "together_ai",
    "cerebras": "cerebras",
    "google-ai-studio": "gemini",
    "ollama": "ollama_chat",
}

# Native LiteLLM routes must not receive the preset's OpenAI-compatible catalog URL.
_NATIVE_LITELLM_PREFIXES = frozenset({"anthropic", "gemini"})


def _value(obj: object, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _model_name(provider_name: str, model: str) -> str:
    if "/" in model and model.split("/", 1)[0] in set(_PROVIDER_MODEL_PREFIXES.values()):
        return model
    prefix = _PROVIDER_MODEL_PREFIXES.get(provider_name)
    return f"{prefix}/{model}" if prefix else model


def _api_base(provider_name: str, base_url: str | None) -> str | None:
    if not base_url:
        return None
    if provider_name == "ollama":
        return base_url.removesuffix("/v1").rstrip("/")
    prefix = _PROVIDER_MODEL_PREFIXES.get(provider_name)
    if prefix in _NATIVE_LITELLM_PREFIXES:
        default = get_llm_provider(provider_name).base_url.rstrip("/")
        if base_url.rstrip("/") == default:
            return None
    return base_url


def _content_from_delta(delta: object) -> str | None:
    value = _value(delta, "content")
    return value if isinstance(value, str) and value else None


def _reasoning_from_delta(delta: object) -> str | None:
    for key in ("reasoning_content", "reasoning"):
        value = _value(delta, key)
        if isinstance(value, str) and value:
            return value

    provider_fields = _value(delta, "provider_specific_fields") or {}
    thinking_blocks = _value(delta, "thinking_blocks") or _value(provider_fields, "thinking_blocks")
    if thinking_blocks:
        parts: list[str] = []
        for block in thinking_blocks:
            text = _value(block, "thinking") or _value(block, "text") or _value(block, "content")
            if isinstance(text, str) and text:
                parts.append(text)
        if parts:
            return "".join(parts)

    return None


class _ToolCallAssembler:
    """Assemble streamed OpenAI-style tool_call deltas by index."""

    def __init__(self) -> None:
        self._slots: dict[int, dict[str, str]] = {}
        self.started = False

    def add(self, tool_calls: Any) -> bool:
        if not tool_calls:
            return False
        first = not self.started
        self.started = True
        for item in tool_calls:
            index = _value(item, "index")
            if not isinstance(index, int):
                index = len(self._slots)
            slot = self._slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
            call_id = _value(item, "id")
            if isinstance(call_id, str) and call_id:
                slot["id"] = call_id
            function = _value(item, "function") or {}
            name = _value(function, "name")
            if isinstance(name, str) and name:
                slot["name"] = name
            arguments = _value(function, "arguments")
            if isinstance(arguments, str) and arguments:
                slot["arguments"] += arguments
        return first

    def complete(self) -> list[ToolCallEvent]:
        completed: list[ToolCallEvent] = []
        for index in sorted(self._slots):
            slot = self._slots[index]
            completed.append(
                ToolCallEvent(
                    call_id=slot["id"] or generate_id("tcall-"),
                    name=slot["name"],
                    arguments=parse_tool_arguments(slot["arguments"]),
                )
            )
        return completed


def _message_tool_calls(message: object) -> list[ToolCallEvent]:
    raw_calls = _value(message, "tool_calls") or []
    assembler = _ToolCallAssembler()
    assembler.add(raw_calls)
    return assembler.complete()


class LiteLLMAdapter:
    """LiteLLM-backed provider adapter."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        provider_name: str,
        timeout_s: float | None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._provider_name = provider_name
        self._timeout_s = timeout_s

    @property
    def model(self) -> str:
        return _model_name(self._provider_name, self._model)

    def supports_reasoning_effort(self) -> bool:
        try:
            from litellm import get_supported_openai_params

            params = get_supported_openai_params(
                model=self.model,
                custom_llm_provider=self._provider_name,
            )
            return bool(params and "reasoning_effort" in params)
        except Exception:
            return False

    def _kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "api_key": self._api_key,
        }
        if self._timeout_s:
            kwargs["timeout"] = self._timeout_s
        api_base = _api_base(self._provider_name, self._base_url)
        if api_base:
            kwargs["api_base"] = api_base
        return kwargs

    def _provider_params(self, *, reasoning_effort: str | None = None) -> dict[str, Any]:
        provider = get_llm_provider(self._provider_name)
        params = provider.extra_request_params(
            model=self._model,
            reasoning_effort=reasoning_effort,
        )
        if reasoning_effort:
            params["reasoning_effort"] = reasoning_effort
        return params

    async def chat(
        self,
        *,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        kwargs = {
            **self._kwargs(),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **self._provider_params(),
        }
        if tools:
            kwargs["tools"] = tools
        response = await litellm.acompletion(**kwargs)
        choices = _value(response, "choices") or []
        if not choices:
            return ChatResult(text="", message={"role": "assistant", "content": ""})
        message = _value(choices[0], "message")
        content = _value(message, "content")
        text = content if isinstance(content, str) else ""
        tool_calls = tuple(_message_tool_calls(message))
        return ChatResult(
            text=text,
            tool_calls=tool_calls,
            message=assistant_tool_message(text, tool_calls) if tool_calls else {
                "role": "assistant",
                "content": text,
            },
        )

    async def chat_stream(
        self,
        *,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelEvent]:
        kwargs = {
            **self._kwargs(),
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": True,
            **self._provider_params(reasoning_effort=reasoning_effort),
        }
        if not reasoning_effort:
            kwargs["temperature"] = temperature
        if tools:
            kwargs["tools"] = tools

        stream = await litellm.acompletion(**kwargs)
        assembler = _ToolCallAssembler()
        async for raw_chunk in stream:
            choices = _value(raw_chunk, "choices") or []
            if not choices:
                continue
            delta = _value(choices[0], "delta")
            reasoning = _reasoning_from_delta(delta)
            if reasoning:
                yield ReasoningEvent(text=reasoning)
            content = _content_from_delta(delta)
            if content:
                yield TextEvent(text=content)
            if assembler.add(_value(delta, "tool_calls")):
                yield ToolCallStarted()
        for call in assembler.complete():
            yield call


def is_retryable_litellm_error(exc: Exception) -> bool:
    return isinstance(exc, (
        litellm.APIConnectionError,
        litellm.BadGatewayError,
        litellm.InternalServerError,
        litellm.RateLimitError,
        litellm.ServiceUnavailableError,
    ))
