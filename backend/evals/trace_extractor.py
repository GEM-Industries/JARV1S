"""Extract normalized agent-behavior fields from TurnResult / turn_trace."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from core.plugins.capabilities import capability_call_preview
from core.turns.delivery import TurnResult


@dataclass(frozen=True)
class ExtractedToolCall:
    fqns: tuple[str, ...]
    capability: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    tool_call_id: str | None = None
    spoken: str | None = None
    output: str | None = None
    invocations: tuple[dict, ...] = ()

    @property
    def code(self) -> str:
        if self.capability:
            return capability_call_preview(self.capability, self.arguments)
        return ""


@dataclass
class TurnTraceSnapshot:
    model: str = ""
    routed_tools: list[str] = field(default_factory=list)
    tools_called: list[str] = field(default_factory=list)
    full_response: str = ""
    tool_calls: list[ExtractedToolCall] = field(default_factory=list)
    tool_outputs: list[str] = field(default_factory=list)
    interrupted: bool = False

    @property
    def all_code(self) -> str:
        blobs: list[str] = []
        for call in self.tool_calls:
            payload = {"capability": call.capability or (call.fqns[0] if call.fqns else "")}
            payload.update(call.arguments or {})
            blobs.append(json.dumps(payload, default=str))
        return "\n".join(blobs)


def _fqns_from_meta(meta: dict) -> tuple[str, ...]:
    invocations = meta.get("invocations") or []
    from_invocations: list[str] = []
    for record in invocations:
        if isinstance(record, dict):
            capability = record.get("capability")
            if capability and capability not in from_invocations:
                from_invocations.append(str(capability))
    if from_invocations:
        return tuple(from_invocations)
    focus = meta.get("focus_tools") or []
    return tuple(str(item) for item in focus)


def extract_turn_trace(result: TurnResult) -> TurnTraceSnapshot:
    """Normalize TurnResult into scorer-friendly fields."""
    snapshot = TurnTraceSnapshot(
        model=result.model,
        routed_tools=list(result.routed_tools),
        tools_called=list(result.tools_called),
        full_response=result.full_response or "",
        interrupted=bool(result.interrupted),
    )

    pending: ExtractedToolCall | None = None
    for entry in result.turn_trace:
        if len(entry) < 2:
            continue
        role, content = entry[0], entry[1]
        meta = entry[2] if len(entry) > 2 and isinstance(entry[2], dict) else {}

        if role == "assistant" and meta.get("turn_type") == "tool_call":
            capability = str(meta.get("capability") or "")
            arguments = meta.get("arguments") if isinstance(meta.get("arguments"), dict) else {}
            fqns = (capability,) if capability else ()
            pending = ExtractedToolCall(
                fqns=fqns,
                capability=capability,
                arguments=dict(arguments),
                tool_call_id=meta.get("tool_call_id"),
                spoken=meta.get("spoken"),
            )
            snapshot.tool_calls.append(pending)
            continue

        if role == "user" and meta.get("turn_type") == "tool_result":
            output = str(meta.get("output") or content or "")
            snapshot.tool_outputs.append(output)
            invocations = tuple(
                record for record in (meta.get("invocations") or [])
                if isinstance(record, dict)
            )
            fqns = _fqns_from_meta(meta)
            capability = str(meta.get("capability") or (fqns[0] if fqns else ""))
            if pending is not None and pending.tool_call_id == meta.get("tool_call_id"):
                pending = ExtractedToolCall(
                    fqns=fqns or pending.fqns,
                    capability=capability or pending.capability,
                    arguments=pending.arguments,
                    tool_call_id=pending.tool_call_id,
                    spoken=pending.spoken,
                    output=output,
                    invocations=invocations,
                )
                snapshot.tool_calls[-1] = pending
            else:
                snapshot.tool_calls.append(
                    ExtractedToolCall(
                        fqns=fqns,
                        capability=capability,
                        tool_call_id=meta.get("tool_call_id"),
                        output=output,
                        invocations=invocations,
                    )
                )
            pending = None
            continue

        if role == "assistant" and meta.get("turn_type") == "text_only":
            text = str(content)
            if text and not snapshot.full_response:
                snapshot.full_response = text

    if not snapshot.full_response:
        for entry in reversed(result.turn_trace):
            if len(entry) >= 2 and entry[0] == "assistant":
                snapshot.full_response = str(entry[1])
                break

    if not snapshot.tools_called:
        seen: list[str] = []
        for call in snapshot.tool_calls:
            for fqn in call.fqns:
                if fqn not in seen:
                    seen.append(fqn)
        snapshot.tools_called = seen

    return snapshot
