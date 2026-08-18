from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal


@dataclass(frozen=True)
class TextEvent:
    text: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True)
class ReasoningEvent:
    text: str
    kind: Literal["reasoning"] = "reasoning"


@dataclass(frozen=True)
class ToolCallStarted:
    """First tool-call delta received. Complete calls follow at stream end."""

    kind: Literal["tool_call_started"] = "tool_call_started"


@dataclass(frozen=True)
class ToolCallEvent:
    """Complete wire tool call. Becomes CapabilityCall after registry name resolution."""

    call_id: str
    name: str
    arguments: dict[str, Any]
    kind: Literal["tool_call"] = "tool_call"


ModelEvent = TextEvent | ReasoningEvent | ToolCallStarted | ToolCallEvent


@dataclass(frozen=True)
class ChatResult:
    text: str
    tool_calls: tuple[ToolCallEvent, ...] = ()
    message: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMStreamEvent:
    """Per-request stream attempt metadata for diagnostics callbacks."""

    status: str
    attempt: int
    max_attempts: int
    retry_count: int
    timeout_ms: int | None = None
    model: str | None = None


LLMStreamEventCallback = Callable[[LLMStreamEvent], None]


def parse_tool_arguments(raw: str) -> dict[str, Any]:
    if not raw or not str(raw).strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__parse_error__": raw}
    if not isinstance(parsed, dict):
        return {"__parse_error__": raw}
    return parsed


def assistant_tool_message(text: str, tool_calls: list[ToolCallEvent] | tuple[ToolCallEvent, ...]) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    if tool_calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, default=str),
                },
            }
            for call in tool_calls
        ]
    return message


def tool_result_message(call_id: str, content: str, name: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "tool",
        "tool_call_id": call_id,
        "content": content,
    }
    if name:
        message["name"] = name
    return message
