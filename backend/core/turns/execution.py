"""Delivery-agnostic agent turn execution.

Runs history load, context assembly, tool routing, and the structured
capability-call loop. Callers own session locks, VoiceDelivery lifecycle,
cancellation persistence, and mode transitions.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

from core.agent.agent import AgentEventType, JarvisAgent
from core.plugins.capabilities import (
    InvocationStatus,
    capability_fqns,
    invocation_trace_payload,
    reset_capability_turn_id,
    set_capability_turn_id,
    take_stashed_execution_invocations,
)
from core.routing.policies import RoutingPolicy, SYSTEM_POLICY, TEXT_POLICY, VOICE_POLICY
from core.setup.readiness import require_llm_ready
from core.time import build_turn_time_context
from core.turns.delivery import (
    DeliveryStrategy,
    HeadlessDelivery,
    StreamEvent,
    TurnResult,
    VoiceDelivery,
)
from core.turns.history import HistoryPolicy, load_turn_history
from core.turns.reasoning_effort import resolve_reasoning_effort
from core.turns.sanitizer import sanitize_assistant_output
from plugins.profile import get_profile_block
from services.diagnostics import diagnostics_service
from services.perf import perf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToolRoutingRequest:
    utterance: str
    policy: RoutingPolicy
    used_routing_hint: bool


def resolve_tool_routing_request(
    *,
    source: str,
    transcript: str,
    routing_hint: str | None,
    text_input: bool,
    attachments: list[dict[str, Any]] | None,
) -> ToolRoutingRequest | None:
    if source != "system" and transcript:
        return ToolRoutingRequest(
            utterance=transcript,
            policy=TEXT_POLICY if text_input or attachments else VOICE_POLICY,
            used_routing_hint=False,
        )

    if routing_hint:
        return ToolRoutingRequest(
            utterance=routing_hint,
            policy=SYSTEM_POLICY,
            used_routing_hint=True,
        )

    return None


def format_tool_call_trace(spoken_text: str, preview: str) -> str:
    if spoken_text:
        return f"{spoken_text}\n\n{preview}"
    return preview


async def execute_turn(
    *,
    agent: JarvisAgent,
    transcript: str,
    source: str,
    connection_id: str,
    owner_id: str,
    session_context: dict[str, Any],
    text_input: bool,
    attachments: Optional[list[dict[str, Any]]],
    delivery: DeliveryStrategy,
    result: TurnResult,
    routing_hint: Optional[str] = None,
    current_turn_id: Optional[str] = None,
    history_policy: HistoryPolicy | None = None,
    trigger_decision: str | None = None,
) -> None:
    """Run the agent loop, forward stream events to `delivery`, populate `result`.

    Delivery-agnostic: voice callers pass a `VoiceDelivery`; headless
    callers pass a `HeadlessDelivery`. `result` is mutated in place so the
    caller can persist partial trace on cancellation.
    """
    require_llm_ready()
    result.transcript = transcript

    # --- History ---
    perf.start("db_history", connection_id)
    resolved_history_policy: HistoryPolicy = (
        history_policy
        or ("interactive_user" if source == "user" else "proactive_bounded")
    )
    loaded_history = await load_turn_history(
        owner_id=owner_id,
        session_context=session_context,
        current_turn_id=current_turn_id,
        policy=resolved_history_policy,
    )
    db_history = loaded_history.messages
    perf.end("db_history", connection_id)
    perf.log(
        "history_loaded",
        session=connection_id,
        history_items=len(db_history),
        source=source,
        history_policy=resolved_history_policy,
        reply_carryover_tool_count=len(loaded_history.reply_tools),
    )

    # --- Context ---
    modality = "text" if text_input else "voice"
    if attachments:
        modality = "multimodal"
    context: dict[str, Any] = {
        **session_context,
        "source": source,
        "modality": modality,
        "owner_id": owner_id,
        "connection_id": connection_id,
        "node_id": session_context.get("node_id"),
        "location_ref": session_context.get("location_ref"),
        "speaker_id": session_context.get("speaker_id"),
    }
    if session_context.get("speaker_confidence") is not None:
        context["speaker_confidence"] = session_context.get("speaker_confidence")
    if session_context.get("speaker_source"):
        context["speaker_source"] = session_context.get("speaker_source")
    if trigger_decision:
        context["trigger_decision"] = trigger_decision

    if db_history:
        context["has_history"] = True

    tz_name = session_context.get("timezone", "UTC")
    try:
        context.update(build_turn_time_context(tz_name))
    except Exception as e:
        logger.error(f"Failed to calculate local time for context: {e}")

    # Inject user profile for system prompt
    context["user_profile"] = await get_profile_block(owner_id)
    if source == "user":
        try:
            from plugins.agents.work import load_open_roster

            context["open_work_block"] = await load_open_roster(owner_id)
        except Exception:
            logger.debug("Open-work roster unavailable", exc_info=True)

    # Tool Router: semantic plugin match for this turn's tools= set.
    # Always-on FQNs are unioned later in JarvisAgent. User turns route on the
    # transcript; system turns route on an optional routing_hint. No hint →
    # empty match set (protocols, SystemPulse).
    from core.tool_router import tool_router
    if source == "user" and loaded_history.reply_tools:
        tool_router.record_route_carryover(
            connection_id,
            tools=set(loaded_history.reply_tools),
        )
    routing_request = resolve_tool_routing_request(
        source=source,
        transcript=transcript,
        routing_hint=routing_hint,
        text_input=text_input,
        attachments=attachments,
    )
    if routing_request:
        context["routed_tools"] = await tool_router.route(
            routing_request.utterance,
            connection_id,
            policy=routing_request.policy,
        )
    else:
        context["routed_tools"] = set()
    from core.setup.llm_config import resolve_llm_config_sync

    context["action_capable"] = bool(resolve_llm_config_sync().action_capable is not False)
    route_diagnostics = tool_router.get_last_diagnostics(connection_id) if routing_request else None
    route_metadata = route_diagnostics.as_dict() if route_diagnostics else {}
    route_metadata.setdefault("routed_tool_count", len(context["routed_tools"]))
    perf.log(
        "tool_routing_complete",
        session=connection_id,
        transcript_chars=len(transcript),
        used_routing_hint=routing_request.used_routing_hint if routing_request else False,
        **route_metadata,
    )

    # --- User content + initial trace ---
    if attachments:
        parts = attachments if not transcript else [{"type": "text", "text": transcript}] + attachments
        user_content: Any = parts
    else:
        user_content = transcript

    trace_role = "user" if source == "user" else "system"
    result.turn_trace.append((trace_role, user_content))

    # --- Snapshot metadata ---
    audio_bound = isinstance(delivery, VoiceDelivery) and delivery.produce_audio
    headless = isinstance(delivery, HeadlessDelivery)
    turn_agent = agent
    result.model = turn_agent.llm.model
    diagnostics_service.record_turn_model(turn_agent.llm.model)
    turn_reasoning_effort = resolve_reasoning_effort(
        audio_bound=audio_bound,
        text_input=text_input,
        headless=headless,
        llm=turn_agent.llm,
    )
    perf.log(
        "reasoning_effort_resolved",
        session=connection_id,
        reasoning_effort=turn_reasoning_effort,
        audio_bound=audio_bound,
        headless=headless,
        text_input=text_input,
        model=turn_agent.llm.model,
    )
    routed_tools_snapshot = sorted(context.get("routed_tools", set()))
    result.routed_tools = routed_tools_snapshot
    perf.log(
        "model_selected",
        session=connection_id,
        model=turn_agent.llm.model,
        routed_tool_count=len(routed_tools_snapshot),
        transcript_chars=len(transcript),
    )

    # Tool-call correlation ID held here so trace writes and StreamEvents share the same ID.
    current_tool_call_id: Optional[str] = None
    current_tool_fqns: list[str] = []
    full_response = ""  # raw model text for the current segment (resets on tool_call)
    reasoning_buffer = ""
    current_response_id: Optional[str] = getattr(delivery, "response_id", None)
    had_model_text = False
    blocked_for_approval = False

    def _flush_reasoning_trace() -> None:
        nonlocal reasoning_buffer
        text = reasoning_buffer.strip()
        if not text:
            return
        result.turn_trace.append(("assistant", text, {
            "turn_type": "reasoning",
            "response_id": current_response_id,
            "model": turn_agent.llm.model,
            "reasoning_effort": turn_reasoning_effort,
            "routed_tools": routed_tools_snapshot,
        }))
        perf.log(
            "reasoning_chunk",
            session=connection_id,
            reasoning_chars=len(text),
            response_id=current_response_id,
        )
        reasoning_buffer = ""

    turn_token = set_capability_turn_id(current_turn_id)
    try:
        async for event in turn_agent.process_stream(
            user_content, db_history, connection_id, context=context,
            reasoning_effort=turn_reasoning_effort,
        ):
            if event.type == AgentEventType.REASONING:
                reasoning_buffer += event.content
                if isinstance(delivery, VoiceDelivery) and not audio_bound:
                    await delivery.on_stream(StreamEvent(tag="reasoning", content=event.content))
                continue

            if event.type == AgentEventType.TEXT:
                sanitized_content = sanitize_assistant_output(event.content)
                if not sanitized_content:
                    continue
                had_model_text = True
                full_response += sanitized_content
                result.full_response = full_response
                await delivery.on_stream(StreamEvent(tag="text", content=sanitized_content))

            elif event.type == AgentEventType.ERROR:
                sanitized_content = sanitize_assistant_output(event.content)
                if not sanitized_content:
                    continue
                result.runtime_error = sanitized_content.strip()
                result.turn_trace.append(("assistant", sanitized_content, {
                    "turn_type": "runtime_error",
                    "model": turn_agent.llm.model,
                    "routed_tools": routed_tools_snapshot,
                }))
                if isinstance(delivery, VoiceDelivery):
                    await delivery.on_stream(StreamEvent(tag="text", content=sanitized_content))

            elif event.type == AgentEventType.TOOL_COMPOSING:
                await delivery.on_stream(StreamEvent(tag="tool_status", content="composing_tool"))

            elif event.type == AgentEventType.TOOL_CALL:
                _flush_reasoning_trace()
                current_tool_call_id = event.tool_call_id
                current_tool_fqns = [event.capability] if event.capability else []
                await delivery.on_stream(StreamEvent(
                    tag="tool_call",
                    content=event.content,
                    tool_call_id=current_tool_call_id,
                    capability=event.capability,
                ))
                current_response_id = getattr(delivery, "response_id", None)

                spoken_text = sanitize_assistant_output(full_response).strip()
                preview = format_tool_call_trace(spoken_text, event.content)
                result.turn_trace.append(("assistant", preview, {
                    "turn_type": "tool_call",
                    "capability": event.capability,
                    "provider_name": event.provider_name,
                    "arguments": event.arguments or {},
                    "spoken": spoken_text,
                    "tool_call_id": current_tool_call_id,
                    "model": turn_agent.llm.model,
                    "routed_tools": routed_tools_snapshot,
                }))

                # Reset raw text buffer for the next segment.
                full_response = ""
                result.full_response = ""

            elif event.type == AgentEventType.TOOL_OUTPUT:
                record = event.outcome.invocation if event.outcome is not None else None
                invocations = [record] if record is not None else []
                current_tool_fqns = capability_fqns(invocations) or (
                    [event.capability] if event.capability else current_tool_fqns
                )
                for fqn in current_tool_fqns:
                    if fqn not in result.tools_called:
                        result.tools_called.append(fqn)
                if current_tool_call_id is None:
                    current_tool_call_id = event.tool_call_id
                if current_tool_fqns:
                    tool_router.record_tool_focus(
                        connection_id,
                        tools=set(current_tool_fqns),
                    )
                await delivery.on_stream(StreamEvent(
                    tag="tool_output",
                    content=event.content,
                    tool_call_id=current_tool_call_id,
                ))
                tool_output = "" if event.content is None else str(event.content)
                result.turn_trace.append(("user", tool_output, {
                    "turn_type": "tool_result",
                    "tool_call_id": current_tool_call_id,
                    "output": tool_output,
                    "capability": event.capability,
                    "invocation_id": record.invocation_id if record is not None else None,
                    "status": event.outcome.status.value if event.outcome is not None else None,
                    "focus_tools": list(current_tool_fqns),
                    "invocations": invocation_trace_payload(invocations),
                }))
                if (
                    event.outcome is not None
                    and event.outcome.status == InvocationStatus.BLOCKED
                    and event.outcome.error is not None
                    and event.outcome.error.code == "approval_needed"
                ):
                    blocked_for_approval = True
                current_tool_call_id = None
                current_tool_fqns = []

            elif event.type == AgentEventType.UI_UPDATE:
                await delivery.on_stream(StreamEvent(tag="ui_update", content=event.content))

            elif event.type == AgentEventType.UI_DELETE:
                await delivery.on_stream(StreamEvent(tag="ui_delete", content=event.content))

            elif event.type == AgentEventType.CONTEXT_METRICS:
                await delivery.on_stream(StreamEvent(tag="context_metrics", content=event.content))

        # End-of-stream: let delivery flush buffered text and emit the final RESPONSE.
        _flush_reasoning_trace()
        if blocked_for_approval and not had_model_text:
            from core.pending_inputs import get_latest_pending_input

            pending = await get_latest_pending_input(owner_id)
            prompt = str((pending or {}).get("prompt") or "").strip()
            if prompt:
                await delivery.on_stream(StreamEvent(tag="text", content=prompt))
                full_response = prompt
                result.full_response = prompt
        await delivery.on_stream(StreamEvent(tag="final_text"))
        perf.log(
            "agent_stream_complete",
            session=connection_id,
            raw_response_chars=len(full_response),
            tool_count=len(result.tools_called),
            routed_tool_count=len(routed_tools_snapshot),
        )

        sanitized_response = sanitize_assistant_output(full_response).strip()
        if sanitized_response:
            result.full_response = sanitized_response
            result.turn_trace.append(("assistant", sanitized_response, {
                "turn_type": "text_only",
                "model": turn_agent.llm.model,
                "tools_called": list(result.tools_called),
                "routed_tools": routed_tools_snapshot,
            }))

        # VoiceDelivery fills this from its sentence stream; HeadlessDelivery leaves "".
        result.delivered_text = sanitize_assistant_output(getattr(delivery, "delivered_text", ""))

    except asyncio.CancelledError:
        result.interrupted = True
        result.delivered_text = sanitize_assistant_output(getattr(delivery, "delivered_text", ""))

        # Persist any capability invocations that closed before barge-in cancelled
        # the stream (executor stashes them when CancelledError fires mid-execute).
        stashed = take_stashed_execution_invocations()
        if stashed and current_tool_call_id:
            focus = capability_fqns(stashed)
            for fqn in focus:
                if fqn not in result.tools_called:
                    result.tools_called.append(fqn)
            result.turn_trace.append(("user", "[interrupted]", {
                "turn_type": "tool_result",
                "tool_call_id": current_tool_call_id,
                "output": "[interrupted]",
                "focus_tools": focus,
                "invocations": stashed,
                "interrupted": True,
            }))

        # Append a partial text_only trace entry if we had buffered text
        # and haven't already traced one for this turn. The caller persists
        # result.turn_trace in its own CancelledError handler.
        already_saved = any(
            e[0] == "assistant" and len(e) > 2 and e[2].get("turn_type") == "text_only"
            for e in result.turn_trace
        )
        partial = sanitize_assistant_output(result.delivered_text or full_response).strip()
        if partial and not already_saved and source == "user":
            result.turn_trace.append(("assistant", partial, {
                "turn_type": "text_only",
                "interrupted": True,
                "model": turn_agent.llm.model,
                "tools_called": list(result.tools_called),
                "routed_tools": routed_tools_snapshot,
            }))
        raise
    finally:
        reset_capability_turn_id(turn_token)
