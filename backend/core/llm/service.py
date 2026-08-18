import asyncio
import inspect
import logging
import time
from typing import AsyncIterator

from core.config import settings
from core.llm.adapters.base import LLMAdapter
from core.llm.adapters.litellm import LiteLLMAdapter, is_retryable_litellm_error
from core.llm.providers import LOCAL_LLM_PROVIDERS, infer_provider_from_base_url
from core.llm.prompt_dump import dump_prompt
from core.llm.types import (
    ChatResult,
    LLMStreamEvent,
    LLMStreamEventCallback,
    ModelEvent,
    TextEvent,
    ToolCallEvent,
    ToolCallStarted,
)
from core.prompts import SystemPrompt

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}
_RETRY_DELAYS = [1.0, 2.0]
_DEFAULT_SYSTEM = "You are JARVIS, a helpful and friendly AI voice assistant."


class LLMFirstTokenTimeoutError(TimeoutError):
    """Raised when a stream is established but no useful token arrives in time."""


class LLMStreamIdleTimeoutError(TimeoutError):
    """Raised when a stream stalls after it already yielded content."""


def _build_system_messages(system_prompt: str | SystemPrompt | None) -> list[dict]:
    content = str(system_prompt) if system_prompt else _DEFAULT_SYSTEM
    return [{"role": "system", "content": content}]


def _flatten_multimodal(msg: dict) -> dict:
    if msg.get("role") in {"tool"} or msg.get("tool_calls"):
        return msg
    content = msg.get("content")
    if not isinstance(content, list):
        return msg
    text = " ".join(p.get("text", "") for p in content if p.get("type") == "text").strip()
    return {**msg, "content": text or "[image]"}


async def _close_stream(stream: object) -> None:
    close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
    if close is None:
        return
    result = close()
    if inspect.isawaitable(result):
        await result


def _status_code(exc: Exception) -> int | None:
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) else None


def _is_retryable_error(exc: Exception) -> bool:
    status = _status_code(exc)
    if status in _RETRYABLE_STATUS_CODES:
        return True
    return is_retryable_litellm_error(exc)


class LLMService:
    """LLM facade: prompt assembly, timeout/retry policy, and adapter delegation."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        provider_name: str | None = None,
        first_token_timeout_s: float | None = None,
        first_token_retries: int | None = None,
        stream_idle_timeout_s: float | None = None,
        request_timeout_s: float | None = None,
        adapter: LLMAdapter | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._model = model
        self._max_tokens = max_tokens
        self._provider_name = provider_name
        self._first_token_timeout_s = first_token_timeout_s
        self._first_token_retries = first_token_retries
        self._stream_idle_timeout_s = stream_idle_timeout_s
        self._request_timeout_s = request_timeout_s
        self._adapter = adapter

    def configure(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        if api_key is not None:
            self._api_key = api_key
        if base_url is not None:
            self._base_url = base_url
        if model is not None:
            self._model = model
        if provider_name is not None:
            self._provider_name = provider_name
        self._adapter = None

    @property
    def provider_name(self) -> str:
        if self._provider_name:
            return self._provider_name
        return infer_provider_from_base_url(self._base_url or "")

    @property
    def model(self) -> str:
        return self._model or ""

    @property
    def supports_reasoning_effort(self) -> bool:
        if self._adapter is None:
            return False
        return self._adapter.supports_reasoning_effort()

    @property
    def is_initialized(self) -> bool:
        return self._adapter is not None

    async def initialize(self) -> None:
        api_key = self._api_key
        base_url = self._base_url
        model = self._model
        if not base_url or not model:
            logger.warning("LLM base URL/model not configured. LLM features will be disabled.")
            return
        if not api_key:
            logger.warning(
                "LLM API key not set for provider '%s'. LLM features will be disabled.",
                self.provider_name,
            )
            return

        self._adapter = LiteLLMAdapter(
            api_key=api_key,
            base_url=base_url,
            model=model,
            provider_name=self.provider_name,
            timeout_s=self._request_timeout_s or settings.LLM_HTTP_TIMEOUT_S,
        )
        logger.info("LLM initialized: %s via %s (%s)", model, base_url, self.provider_name)

    def _messages(
        self,
        *,
        user_message: str | list[dict],
        conversation_history: list[dict] | None,
        system_prompt: str | SystemPrompt | None,
    ) -> list[dict]:
        messages = _build_system_messages(system_prompt)
        if conversation_history:
            messages.extend(_flatten_multimodal(m) for m in conversation_history)
        if user_message:
            messages.append({"role": "user", "content": user_message})
        return messages

    def _max_tokens_value(self) -> int:
        return self._max_tokens or settings.LLM_MAX_TOKENS

    def _is_local_provider(self) -> bool:
        return self.provider_name in LOCAL_LLM_PROVIDERS

    def _resolve_first_token_timeout_s(self, override: float | None) -> float:
        if override is not None:
            return override
        if self._first_token_timeout_s is not None:
            return self._first_token_timeout_s
        if self._is_local_provider():
            return settings.LLM_LOCAL_STREAM_FIRST_TOKEN_TIMEOUT_S
        return settings.LLM_STREAM_FIRST_TOKEN_TIMEOUT_S

    def _resolve_idle_timeout_s(self, override: float | None) -> float:
        if override is not None:
            return override
        if self._stream_idle_timeout_s is not None:
            return self._stream_idle_timeout_s
        if self._is_local_provider():
            return settings.LLM_LOCAL_STREAM_IDLE_TIMEOUT_S
        return settings.LLM_STREAM_IDLE_TIMEOUT_S

    async def chat(
        self,
        user_message: str | list[dict],
        conversation_history: list[dict] | None = None,
        system_prompt: str | SystemPrompt | None = None,
        temperature: float = 0.7,
        dump_tag: str = "",
    ) -> str:
        if self._adapter is None:
            raise RuntimeError("LLM client not initialized. Call initialize() first.")

        messages = self._messages(
            user_message=user_message,
            conversation_history=conversation_history,
            system_prompt=system_prompt,
        )
        if settings.PROMPT_DUMP_ENABLED:
            dump_prompt(messages, self.model, tag=dump_tag)

        content = await self._adapter.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=self._max_tokens_value(),
        )
        logger.debug("LLM response: %s", content.text)
        return content.text

    async def complete(
        self,
        *,
        messages: list[dict],
        temperature: float = 0.0,
        tools: list[dict] | None = None,
        dump_tag: str = "",
    ) -> ChatResult:
        if self._adapter is None:
            raise RuntimeError("LLM client not initialized. Call initialize() first.")
        if settings.PROMPT_DUMP_ENABLED:
            dump_prompt(messages, self.model, tag=dump_tag)
        return await self._adapter.chat(
            messages=messages,
            temperature=temperature,
            max_tokens=self._max_tokens_value(),
            tools=tools,
        )

    async def chat_stream(
        self,
        user_message: str | list[dict],
        conversation_history: list[dict] | None = None,
        system_prompt: str | SystemPrompt | None = None,
        temperature: float = 0.7,
        dump_tag: str = "",
        first_token_timeout_s: float | None = None,
        first_token_retries: int | None = None,
        stream_idle_timeout_s: float | None = None,
        on_stream_event: LLMStreamEventCallback | None = None,
        reasoning_effort: str | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[ModelEvent]:
        if self._adapter is None:
            logger.error("LLM client not initialized")
            yield TextEvent(text="I'm sorry, I'm not ready to respond yet.")
            return

        messages = self._messages(
            user_message=user_message,
            conversation_history=conversation_history,
            system_prompt=system_prompt,
        )
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        if settings.PROMPT_DUMP_ENABLED:
            dump_prompt(messages, self.model, tag=dump_tag)

        timeout_s = self._resolve_first_token_timeout_s(first_token_timeout_s)

        max_retries = (
            first_token_retries
            if first_token_retries is not None
            else self._first_token_retries
        )
        if max_retries is None:
            max_retries = settings.LLM_STREAM_FIRST_TOKEN_RETRIES
        max_attempts = max(1, int(max_retries) + 1)
        timeout_ms = int(timeout_s * 1000) if timeout_s and timeout_s > 0 else None

        idle_timeout_s = self._resolve_idle_timeout_s(stream_idle_timeout_s)
        idle_timeout_ms = int(idle_timeout_s * 1000) if idle_timeout_s and idle_timeout_s > 0 else None

        def emit(status: str, attempt: int, override_timeout_ms: int | None = None) -> None:
            if on_stream_event is None:
                return
            on_stream_event(LLMStreamEvent(
                status=status,
                attempt=attempt,
                max_attempts=max_attempts,
                retry_count=max(0, attempt - 1),
                timeout_ms=timeout_ms if override_timeout_ms is None else override_timeout_ms,
                model=self.model,
            ))

        last_exc: Exception | None = None
        for attempt_index in range(max_attempts):
            attempt = attempt_index + 1
            delay = 0.0 if attempt_index == 0 else _RETRY_DELAYS[min(attempt_index - 1, len(_RETRY_DELAYS) - 1)]
            if delay:
                emit("retrying", attempt)
                logger.warning(
                    "LLM stream retry %d/%d after %.0fs (model=%s)",
                    attempt, max_attempts, delay, self.model,
                )
                await asyncio.sleep(delay)

            stream: AsyncIterator[ModelEvent] | None = None
            try:
                stream = self._adapter.chat_stream(
                    messages=messages,
                    temperature=temperature,
                    max_tokens=self._max_tokens_value(),
                    reasoning_effort=reasoning_effort,
                    tools=tools,
                )
                logger.debug("LLM stream: %d messages, ~%d tokens", len(messages), total_chars // 4)
                emit("waiting", attempt)

                stream_iter = aiter(stream)
                first_chunk = await (
                    anext(stream_iter)
                    if not timeout_s or timeout_s <= 0
                    else asyncio.wait_for(anext(stream_iter), timeout=timeout_s)
                )
                emit("first_token", attempt)
                yield first_chunk
                last_content_at = time.monotonic() if _event_is_content(first_chunk) else None
                last_content_gap_log_at: float | None = None

                while True:
                    try:
                        chunk = await (
                            anext(stream_iter)
                            if not idle_timeout_s or idle_timeout_s <= 0
                            else asyncio.wait_for(anext(stream_iter), timeout=idle_timeout_s)
                        )
                    except StopAsyncIteration:
                        return
                    except asyncio.TimeoutError as exc:
                        emit("stream_idle_timeout", attempt, idle_timeout_ms)
                        await _close_stream(stream)
                        raise LLMStreamIdleTimeoutError(
                            f"LLM stream stalled for {idle_timeout_s:.1f}s after first token "
                            f"(model={self.model})"
                        ) from exc

                    if _event_is_content(chunk):
                        last_content_at = time.monotonic()
                    yield chunk
                    if not _event_is_content(chunk) and last_content_at is not None:
                        now = time.monotonic()
                        if (
                            (last_content_gap_log_at is None or now - last_content_gap_log_at >= 5.0)
                            and now - last_content_at >= 5.0
                        ):
                            logger.warning(
                                "LLM stream content gap | model=%s gap_ms=%.1f",
                                self.model, (now - last_content_at) * 1000,
                            )
                            last_content_gap_log_at = now

            except LLMStreamIdleTimeoutError:
                raise
            except asyncio.TimeoutError:
                last_exc = LLMFirstTokenTimeoutError(
                    f"LLM stream first token timed out after {timeout_s:.1f}s "
                    f"(attempt {attempt}/{max_attempts}, model={self.model})"
                )
                emit("timeout", attempt)
                logger.warning("%s", last_exc)
                if stream is not None:
                    await _close_stream(stream)
            except Exception as exc:
                if not _is_retryable_error(exc):
                    raise
                last_exc = exc
                emit("error", attempt)
                logger.warning("LLM stream transient error (attempt %d): %s", attempt, exc)
        else:
            raise last_exc  # type: ignore[misc]


def _event_is_content(event: ModelEvent) -> bool:
    return isinstance(event, (TextEvent, ToolCallEvent, ToolCallStarted))
