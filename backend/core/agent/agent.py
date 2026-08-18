import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Dict, List, Optional, Union

from core.context import RuntimeIdentity, ToolRuntimeContext, bind_tool_context, reset_tool_context
from core.context_manager import compact_history, cap_tool_result
from core.llm.providers import LOCAL_LLM_PROVIDERS
from core.llm.service import LLMFirstTokenTimeoutError, LLMService, LLMStreamIdleTimeoutError
from core.llm.types import (
    LLMStreamEvent,
    ReasoningEvent,
    TextEvent,
    ToolCallEvent,
    ToolCallStarted,
    assistant_tool_message,
    tool_result_message,
)
from core.plugins.capabilities import (
    CapabilityCall,
    CapabilityOutcome,
    InvocationLedger,
    InvocationStatus,
    bind_invocation_ledger,
    capability_call_preview,
    reset_invocation_ledger,
    reset_invocation_source,
    set_invocation_source,
    stash_execution_invocations,
)
from core.plugins.dispatcher import dispatcher
from core.plugins.registry import registry
from core.prompts import PromptBuilder
from core.prompts.builder import PromptMode
from core.tool_router import active_tool_fqns, search_result_fqns
from services.perf import perf

logger = logging.getLogger(__name__)

_SEARCH_TOOLS_FQN = "system.search_tools"
_LLM_STALL_TEXT = "I'm sorry, the model stream stalled before I could finish."
_LLM_LOOP_TEXT = "I'm sorry, sir. I seem to have encountered a logical loop."


def first_turn_llm_error_text(exc: BaseException, llm: LLMService) -> str:
    """User-facing copy for a first-iteration provider/runtime failure.

    Not model TEXT — callers emit this as ``AgentEventType.ERROR``.
    """
    from openai import AuthenticationError

    error_type = type(exc)
    is_litellm_auth_error = (
        error_type.__name__ == "AuthenticationError"
        and error_type.__module__.startswith("litellm")
    )
    if isinstance(exc, AuthenticationError) or is_litellm_auth_error or "401" in str(exc):
        return (
            "Your language model API key was rejected. "
            "Open Jarvis Host setup and update your provider key."
        )
    if isinstance(exc, LLMFirstTokenTimeoutError):
        if llm.provider_name in LOCAL_LLM_PROVIDERS:
            return (
                "The on-device model is still loading. "
                "The first reply after idle can take a minute — try again."
            )
        return "The language model took too long to respond. Try again in a moment."
    if not llm.is_initialized:
        return (
            "Jarvis Host setup is incomplete. "
            "Configure your language model provider before chatting."
        )
    return (
        "I'm having trouble reaching my language model. "
        "Check your provider settings and try again."
    )


class AgentEventType(Enum):
    TEXT = auto()
    ERROR = auto()
    REASONING = auto()
    TOOL_COMPOSING = auto()
    TOOL_CALL = auto()
    TOOL_OUTPUT = auto()
    UI_UPDATE = auto()
    UI_DELETE = auto()
    CONTEXT_METRICS = auto()


@dataclass
class AgentEvent:
    type: AgentEventType
    content: str
    tool_call_id: str | None = None
    capability: str | None = None
    provider_name: str | None = None
    arguments: dict[str, Any] | None = None
    outcome: CapabilityOutcome | None = None


class JarvisAgent:
    """Agent loop: stream model events, dispatch CapabilityCalls, continue until final text."""

    def __init__(self, llm_service: LLMService):
        self.llm = llm_service
        self.prompt_builder = PromptBuilder()

    async def process_stream(
        self,
        user_input: Union[str, list[dict]],
        conversation_history: List[Dict],
        session_id: str,
        context: Optional[Dict[str, Any]] = None,
        max_iterations: int = 10,
        prompt_mode: PromptMode = PromptMode.FULL,
        reasoning_effort: str | None = None,
    ):
        local_history = list(conversation_history)
        if user_input:
            local_history.append({"role": "user", "content": user_input})

        context = dict(context or {})
        owner_id = context.get("owner_id")
        if not owner_id:
            raise RuntimeError("JarvisAgent.process_stream requires context['owner_id']")

        user_profile = context.pop("user_profile", None)
        routed_tools = set(context.pop("routed_tools", None) or ())
        action_capable = bool(context.pop("action_capable", True))
        active_fqns = active_tool_fqns(routed_tools)

        perf.start("prompt_build", session_id)
        context_prompt = self.prompt_builder.build(
            runtime_context=context,
            user_profile=user_profile,
            mode=prompt_mode,
            action_capable=action_capable,
        )
        perf.end("prompt_build", session_id)

        perf.start("ctx_budget", session_id)
        local_history, ctx_stats = await compact_history(
            local_history, context_prompt,
            llm_service=self.llm,
            session_id=session_id,
        )
        perf.end("ctx_budget", session_id)
        yield AgentEvent(type=AgentEventType.CONTEXT_METRICS, content=json.dumps(ctx_stats))

        runtime_token = bind_tool_context(
            ToolRuntimeContext(
                identity=RuntimeIdentity(
                    owner_id=str(owner_id),
                    connection_id=session_id,
                    node_id=context.get("node_id"),
                    location_ref=context.get("location_ref"),
                    device_kind=context.get("device_kind"),
                ),
                timezone=context.get("timezone", "UTC"),
                location=context.get("location"),
            )
        )
        try:
            async for event in self._run_loop(
                local_history=local_history,
                context_prompt=context_prompt,
                session_id=session_id,
                active_fqns=active_fqns,
                action_capable=action_capable,
                max_iterations=max_iterations,
                reasoning_effort=reasoning_effort,
            ):
                yield event
        finally:
            reset_tool_context(runtime_token)

    async def _run_loop(
        self,
        *,
        local_history: list[dict],
        context_prompt,
        session_id: str,
        active_fqns: set[str],
        action_capable: bool,
        max_iterations: int,
        reasoning_effort: str | None,
    ):
        for turn in range(max_iterations):
            llm_stream_meta: dict[str, Any] = {}
            llm_started_at = time.monotonic()

            def on_llm_stream_event(event: LLMStreamEvent) -> None:
                llm_stream_meta.update({
                    "attempt": event.attempt,
                    "retry_count": event.retry_count,
                    "timeout_ms": event.timeout_ms,
                    "status": event.status,
                })
                perf.log(
                    "llm_stream_event",
                    session=session_id,
                    iteration=turn,
                    model=self.llm.model,
                    attempt=event.attempt,
                    max_attempts=event.max_attempts,
                    retry_count=event.retry_count,
                    timeout_ms=event.timeout_ms,
                    status=event.status,
                )

            tools = registry.provider_tools(active_fqns) if action_capable else None
            logger.info(
                "Capability-call iteration %d tools=%s",
                turn,
                "none" if tools is None else len(tools),
            )
            perf.start("llm", session_id, iteration=turn, model=self.llm.model)
            stream_gen = self.llm.chat_stream(
                user_message="",
                conversation_history=local_history,
                system_prompt=context_prompt,
                dump_tag=f"{session_id}_iter{turn}",
                on_stream_event=on_llm_stream_event,
                reasoning_effort=reasoning_effort,
                tools=tools,
            )

            text_parts: list[str] = []
            tool_calls: list[ToolCallEvent] = []
            tool_composing_emitted = False
            is_first_chunk = True
            try:
                async for event in stream_gen:
                    if is_first_chunk:
                        retry_count = int(llm_stream_meta.get("retry_count") or 0)
                        perf.end(
                            "llm",
                            session_id,
                            iteration=turn,
                            model=self.llm.model,
                            attempt=llm_stream_meta.get("attempt"),
                            retry_count=retry_count,
                            timeout_ms=llm_stream_meta.get("timeout_ms"),
                            status="retry_ok" if retry_count else "ok",
                        )
                        is_first_chunk = False

                    if isinstance(event, ReasoningEvent):
                        yield AgentEvent(type=AgentEventType.REASONING, content=event.text)
                        continue
                    if isinstance(event, TextEvent):
                        text_parts.append(event.text)
                        yield AgentEvent(type=AgentEventType.TEXT, content=event.text)
                        continue
                    if isinstance(event, ToolCallStarted) and not tool_composing_emitted:
                        yield AgentEvent(type=AgentEventType.TOOL_COMPOSING, content="")
                        tool_composing_emitted = True
                        continue
                    if isinstance(event, ToolCallEvent):
                        tool_calls.append(event)
                        continue
            except Exception as e:
                if is_first_chunk:
                    perf.end(
                        "llm",
                        session_id,
                        iteration=turn,
                        model=self.llm.model,
                        attempt=llm_stream_meta.get("attempt"),
                        retry_count=llm_stream_meta.get("retry_count"),
                        timeout_ms=llm_stream_meta.get("timeout_ms"),
                        status=llm_stream_meta.get("status") or "error",
                    )
                logger.warning("LLM call failed on iteration %d: %s", turn, e)
                if isinstance(e, LLMStreamIdleTimeoutError):
                    yield AgentEvent(type=AgentEventType.ERROR, content=_LLM_STALL_TEXT)
                    return
                if turn == 0 and not text_parts and not tool_calls:
                    yield AgentEvent(
                        type=AgentEventType.ERROR,
                        content=first_turn_llm_error_text(e, self.llm),
                    )
                return

            full_response = "".join(text_parts)
            if not full_response and not tool_calls:
                logger.warning("Agent turn %d: LLM returned empty response", turn)
                return

            if not tool_calls:
                return

            logger.info(
                "Structured tool calls | session=%s iteration=%d elapsed_ms=%.1f count=%d",
                session_id,
                turn,
                (time.monotonic() - llm_started_at) * 1000,
                len(tool_calls),
            )

            resolved_calls: list[tuple[ToolCallEvent, str]] = []
            for wire in tool_calls:
                definition = registry.resolve_provider_name(wire.name)
                capability = definition.fqn if definition is not None else wire.name
                resolved_calls.append((wire, capability))
                if not tool_composing_emitted:
                    yield AgentEvent(type=AgentEventType.TOOL_COMPOSING, content="")
                    tool_composing_emitted = True
                yield AgentEvent(
                    type=AgentEventType.TOOL_CALL,
                    content=capability_call_preview(capability, wire.arguments),
                    tool_call_id=wire.call_id,
                    capability=capability,
                    provider_name=wire.name,
                    arguments=dict(wire.arguments),
                )

            ledger = InvocationLedger()
            ledger_token = bind_invocation_ledger(ledger)
            source_token = set_invocation_source("structured")
            outcomes: list[CapabilityOutcome] = []
            try:
                async def _run(wire: ToolCallEvent, capability: str) -> CapabilityOutcome:
                    return await dispatcher.dispatch(
                        CapabilityCall(
                            capability=capability,
                            arguments=dict(wire.arguments),
                            call_id=wire.call_id,
                        )
                    )

                perf.start("capability_dispatch", session_id, iteration=turn)
                try:
                    async with asyncio.TaskGroup() as group:
                        tasks = [
                            group.create_task(_run(wire, capability))
                            for wire, capability in resolved_calls
                        ]
                    outcomes = [task.result() for task in tasks]
                except ExceptionGroup as exc:
                    ledger.close_open(InvocationStatus.INTERRUPTED, error_type="CancelledError")
                    stash_execution_invocations(ledger.records)
                    cancelled = next(
                        (item for item in exc.exceptions if isinstance(item, asyncio.CancelledError)),
                        None,
                    )
                    if cancelled is not None:
                        raise cancelled from exc
                    raise
                finally:
                    perf.end("capability_dispatch", session_id, iteration=turn)
            except asyncio.CancelledError:
                ledger.close_open(InvocationStatus.INTERRUPTED, error_type="CancelledError")
                stash_execution_invocations(ledger.records)
                raise
            finally:
                reset_invocation_source(source_token)
                reset_invocation_ledger(ledger_token)

            local_history.append(assistant_tool_message(full_response, tool_calls))
            for (wire, capability), outcome in zip(resolved_calls, outcomes):
                for envelope in outcome.ui_events:
                    yield AgentEvent(
                        type=AgentEventType.UI_UPDATE,
                        content=json.dumps(envelope.model_dump()),
                    )
                observation = cap_tool_result(outcome.observation())
                yield AgentEvent(
                    type=AgentEventType.TOOL_OUTPUT,
                    content=observation,
                    tool_call_id=wire.call_id,
                    capability=outcome.capability or capability,
                    outcome=outcome,
                )
                local_history.append(
                    tool_result_message(wire.call_id, observation, wire.name)
                )
                if outcome.capability == _SEARCH_TOOLS_FQN:
                    active_fqns |= search_result_fqns(outcome.data)

        yield AgentEvent(type=AgentEventType.ERROR, content=_LLM_LOOP_TEXT)
