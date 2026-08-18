"""Capability definitions, calls, outcomes, and per-run invocation ledger.

One definition describes a callable `jarvis.<plugin>.<tool>` surface.
`CapabilityCall` is the only request the dispatcher accepts. Turn/task traces
persist the invocation ledger.
"""

from __future__ import annotations

import inspect
import json
import time
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from core.id import generate_id
from core.plugins.types import UIEnvelope

CapabilitySource = Literal["first_party", "mcp", "eval"]
InvocationSource = Literal["ui", "test", "structured"]


class InvocationStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    INTERRUPTED = "interrupted"
    NOT_EXECUTED = "not_executed"


BLOCKED_ERROR_CODES: frozenset[str] = frozenset({
    "approval_needed",
    "reauth_needed",
    "skipped",
})

_ARG_PREVIEW_CHARS = 120
_MAX_PREVIEW_KEYS = 12
_SECRET_KEYS = frozenset({
    "password",
    "token",
    "access_token",
    "refresh_token",
    "api_key",
    "apikey",
    "authorization",
    "secret",
    "client_secret",
})


@dataclass(frozen=True, slots=True)
class CapabilityDefinition:
    """Immutable catalog entry for one mounted capability."""

    fqn: str
    plugin: str
    name: str
    implementation: Callable[..., Awaitable[Any]]
    visible_signature: inspect.Signature
    documentation: str
    return_schema: dict[str, Any] = field(default_factory=dict)
    source: CapabilitySource = "first_party"
    enabled: bool = True
    mcp_annotations: Mapping[str, Any] | None = None
    trusted_mcp: bool = False
    provider_name: str = ""
    description: str = ""
    injected: tuple[str, ...] = ()
    input_schema: dict[str, Any] = field(default_factory=dict)
    input_model: type[BaseModel] | None = None

    @property
    def visible_signature_str(self) -> str:
        return str(self.visible_signature)


@dataclass(frozen=True, slots=True)
class CapabilityCall:
    """Resolved request to invoke a JARV1S-managed capability."""

    capability: str
    arguments: dict[str, Any]
    call_id: str


class CapabilityErrorDetail(BaseModel):
    """Typed failure for a non-success capability outcome."""

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)

    def __str__(self) -> str:
        return self.message

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str) and item in self.message


@dataclass(slots=True)
class InvocationRecord:
    """One capability call within a dispatcher scope."""

    invocation_id: str
    capability: str
    status: InvocationStatus
    source: InvocationSource = "structured"
    tool_call_id: str | None = None
    turn_id: str | None = None
    task_id: str | None = None
    started_at_ms: int = 0
    ended_at_ms: int | None = None
    duration_ms: float | None = None
    error_type: str | None = None
    pending_input_id: str | None = None
    consent_decision: str | None = None
    args_preview: dict[str, Any] = field(default_factory=dict)

    def close(
        self,
        status: InvocationStatus,
        *,
        error_type: str | None = None,
    ) -> None:
        if self.ended_at_ms is not None:
            return
        now = _now_ms()
        self.ended_at_ms = now
        if self.started_at_ms:
            self.duration_ms = max(0.0, float(now - self.started_at_ms))
        self.status = status
        if error_type is not None:
            self.error_type = error_type

    def to_trace_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


class CapabilityOutcome(BaseModel):
    """Normalized dispatcher result for one capability call."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    call_id: str
    capability: str
    status: InvocationStatus
    data: Any = None
    error: CapabilityErrorDetail | None = None
    ui_events: list[UIEnvelope] = Field(default_factory=list)
    invocation: InvocationRecord | None = None

    def observation(self) -> str:
        """Model-visible string for the provider tool-result message."""
        if self.error is not None:
            return self.error.message
        if isinstance(self.data, str):
            return self.data
        if self.data is None:
            return ""
        from core.plugins.serialization import format_tool_output

        return str(format_tool_output(self.data))


class InvocationLedger:
    """In-memory invocation spans for one dispatcher scope.

    Nested invokes use a stack. Concurrent TaskGroup calls close by id.
    """

    def __init__(self) -> None:
        self._records: list[InvocationRecord] = []
        self._stack: list[InvocationRecord] = []
        self._by_id: dict[str, InvocationRecord] = {}

    @property
    def records(self) -> tuple[InvocationRecord, ...]:
        return tuple(self._records)

    @property
    def active(self) -> InvocationRecord | None:
        return self._stack[-1] if self._stack else None

    def open(
        self,
        *,
        capability: str,
        args_preview: dict[str, Any] | None = None,
        source: InvocationSource = "structured",
        tool_call_id: str | None = None,
        turn_id: str | None = None,
        task_id: str | None = None,
    ) -> InvocationRecord:
        record = InvocationRecord(
            invocation_id=generate_id("inv-"),
            capability=capability,
            status=InvocationStatus.NOT_EXECUTED,
            source=source,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            task_id=task_id,
            started_at_ms=_now_ms(),
            args_preview=dict(args_preview or {}),
        )
        self._records.append(record)
        self._by_id[record.invocation_id] = record
        self._stack.append(record)
        return record

    def close(
        self,
        invocation_id: str,
        status: InvocationStatus,
        *,
        error_type: str | None = None,
    ) -> InvocationRecord | None:
        record = self._by_id.get(invocation_id)
        if record is None:
            return None
        record.close(status, error_type=error_type)
        if self._stack and self._stack[-1].invocation_id == invocation_id:
            self._stack.pop()
        else:
            self._stack = [item for item in self._stack if item.invocation_id != invocation_id]
        return record

    def annotate(
        self,
        invocation_id: str | None = None,
        **fields: Any,
    ) -> None:
        record = self._by_id.get(invocation_id) if invocation_id else self.active
        if record is None:
            return
        for key, value in fields.items():
            if hasattr(record, key) and value is not None:
                setattr(record, key, value)

    def close_open(
        self,
        status: InvocationStatus,
        *,
        error_type: str | None = None,
    ) -> None:
        for record in self._records:
            if record.ended_at_ms is None:
                record.close(status, error_type=error_type)
        self._stack.clear()

    def snapshot(self) -> list[dict[str, Any]]:
        return [record.to_trace_dict() for record in self._records]


_invocation_ledger: ContextVar[InvocationLedger | None] = ContextVar(
    "invocation_ledger",
    default=None,
)
_active_invocation_id: ContextVar[str | None] = ContextVar(
    "active_invocation_id",
    default=None,
)
_tool_call_id: ContextVar[str | None] = ContextVar("tool_call_id", default=None)
_turn_id: ContextVar[str | None] = ContextVar("capability_turn_id", default=None)
_task_id: ContextVar[str | None] = ContextVar("capability_task_id", default=None)
_invocation_source: ContextVar[InvocationSource] = ContextVar(
    "invocation_source",
    default="structured",
)
# Stash for barge-in/cancel so execute_turn can persist a partial tool_result.
_last_execution_invocations: ContextVar[tuple[dict[str, Any], ...] | None] = ContextVar(
    "last_execution_invocations",
    default=None,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def redact_args_preview(
    kwargs: Mapping[str, Any],
    *,
    bound_args: Mapping[str, Any] | None = None,
    positional: tuple[Any, ...] | None = None,
) -> dict[str, Any]:
    """Bounded, redacted argument preview for traces — never raw secrets."""
    if bound_args is not None:
        source = dict(bound_args)
    else:
        source = dict(kwargs)
        if positional:
            source = {"*args": [_preview_value(v) for v in positional[:8]], **source}
    preview: dict[str, Any] = {}
    for index, (key, value) in enumerate(source.items()):
        if index >= _MAX_PREVIEW_KEYS:
            preview["…"] = f"+{len(source) - _MAX_PREVIEW_KEYS} more"
            break
        lowered = str(key).lower()
        if lowered in _SECRET_KEYS or any(part in lowered for part in ("secret", "password", "token")):
            preview[key] = "***"
            continue
        preview[key] = _preview_value(value)
    return preview


def _preview_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) <= _ARG_PREVIEW_CHARS:
            return value
        return value[:_ARG_PREVIEW_CHARS] + "…"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__}(len={len(value)})"
    if isinstance(value, dict):
        return f"dict(len={len(value)})"
    text = repr(value)
    if len(text) > _ARG_PREVIEW_CHARS:
        return text[:_ARG_PREVIEW_CHARS] + "…"
    return text


def get_invocation_ledger() -> InvocationLedger | None:
    return _invocation_ledger.get()


def bind_invocation_ledger(ledger: InvocationLedger | None = None) -> Token:
    return _invocation_ledger.set(ledger if ledger is not None else InvocationLedger())


def reset_invocation_ledger(token: Token) -> None:
    _invocation_ledger.reset(token)


def get_tool_call_id() -> str | None:
    return _tool_call_id.get()


def set_tool_call_id(tool_call_id: str | None) -> Token:
    return _tool_call_id.set(tool_call_id)


def reset_tool_call_id(token: Token) -> None:
    _tool_call_id.reset(token)


def get_capability_turn_id() -> str | None:
    return _turn_id.get()


def set_capability_turn_id(turn_id: str | None) -> Token:
    return _turn_id.set(turn_id)


def reset_capability_turn_id(token: Token) -> None:
    _turn_id.reset(token)


def get_capability_task_id() -> str | None:
    return _task_id.get()


def set_capability_task_id(task_id: str | None) -> Token:
    return _task_id.set(task_id)


def reset_capability_task_id(token: Token) -> None:
    _task_id.reset(token)


def get_invocation_source() -> InvocationSource:
    return _invocation_source.get()


def set_invocation_source(source: InvocationSource) -> Token:
    return _invocation_source.set(source)


def reset_invocation_source(token: Token) -> None:
    _invocation_source.reset(token)


def get_active_invocation_id() -> str | None:
    return _active_invocation_id.get()


def set_active_invocation_id(invocation_id: str | None) -> Token:
    return _active_invocation_id.set(invocation_id)


def reset_active_invocation_id(token: Token) -> None:
    _active_invocation_id.reset(token)


def stash_execution_invocations(records: list[InvocationRecord] | tuple[InvocationRecord, ...]) -> None:
    _last_execution_invocations.set(tuple(invocation_trace_payload(records)))


def take_stashed_execution_invocations() -> list[dict[str, Any]]:
    payload = _last_execution_invocations.get()
    _last_execution_invocations.set(None)
    return list(payload or ())


def capability_call_preview(capability: str, arguments: Mapping[str, Any] | None = None) -> str:
    """Short display string for traces, history, and UI."""
    try:
        payload = json.dumps(dict(arguments or {}), default=str, separators=(",", ":"))
    except TypeError:
        payload = str(arguments or {})
    if len(payload) > 240:
        payload = payload[:240] + "…"
    return f"{capability}({payload})"


def capability_fqns(records: list[InvocationRecord] | tuple[InvocationRecord, ...] | list[Any]) -> list[str]:
    seen: list[str] = []
    for record in records:
        capability = getattr(record, "capability", None)
        if capability is None and isinstance(record, dict):
            capability = record.get("capability")
        if capability and capability not in seen:
            seen.append(str(capability))
    return seen


def invocation_trace_payload(
    records: list[InvocationRecord] | tuple[InvocationRecord, ...] | list[Any],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for record in records:
        if hasattr(record, "to_trace_dict"):
            payload.append(record.to_trace_dict())
        elif isinstance(record, dict):
            payload.append(record)
    return payload


def status_for_result(result: Any) -> InvocationStatus:
    if isinstance(result, CapabilityErrorDetail):
        if result.code in BLOCKED_ERROR_CODES:
            return InvocationStatus.BLOCKED
        return InvocationStatus.FAILED
    return InvocationStatus.SUCCEEDED
