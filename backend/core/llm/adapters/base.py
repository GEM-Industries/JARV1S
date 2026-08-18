from __future__ import annotations

from typing import Any, AsyncIterator, Protocol

from core.llm.types import ChatResult, ModelEvent


class LLMAdapter(Protocol):
    """Provider adapter contract used by LLMService."""

    def supports_reasoning_effort(self) -> bool:
        """Return whether the active provider/model accepts reasoning_effort."""

    async def chat(
        self,
        *,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResult:
        """Return a complete assistant response, including any tool calls."""

    def chat_stream(
        self,
        *,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        reasoning_effort: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[ModelEvent]:
        """Yield normalized text, reasoning, and complete tool-call events."""
