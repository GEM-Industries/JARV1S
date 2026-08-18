from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent.agent import AgentEventType, JarvisAgent
from core.llm.types import TextEvent, ToolCallEvent
from core.plugins.capabilities import (
    CapabilityCall,
    CapabilityErrorDetail,
    CapabilityOutcome,
    InvocationRecord,
    InvocationStatus,
)


class ScriptedLLM:
    def __init__(self, iterations):
        self.iterations = [list(item) for item in iterations]
        self.tools_seen: list = []
        self.model = "test-model"

    async def chat_stream(self, **kwargs):
        self.tools_seen.append(kwargs.get("tools"))
        for event in self.iterations.pop(0):
            yield event


def _outcome(
    call: CapabilityCall,
    data,
    *,
    status=InvocationStatus.SUCCEEDED,
    error: CapabilityErrorDetail | None = None,
) -> CapabilityOutcome:
    if error is None and status != InvocationStatus.SUCCEEDED:
        error = CapabilityErrorDetail(code=status.value, message=str(data))
    return CapabilityOutcome(
        call_id=call.call_id,
        capability=call.capability,
        status=status,
        data=data,
        error=error,
        invocation=InvocationRecord(
            invocation_id=f"inv-{call.call_id}",
            capability=call.capability,
            status=status,
            source="structured",
            tool_call_id=call.call_id,
            args_preview=dict(call.arguments),
        ),
    )


async def _run(agent, **context):
    with (
        patch("core.agent.agent.compact_history", AsyncMock(return_value=([], {}))),
        patch("core.agent.agent.registry.provider_tools", return_value=[{"type": "function"}]),
    ):
        return [
            event
            async for event in agent.process_stream(
                "do it",
                [],
                "conn-1",
                context={"owner_id": "geoff", **context},
                max_iterations=4,
            )
        ]


@pytest.mark.asyncio
async def test_ordered_parallel_results_and_chaining():
    llm = ScriptedLLM([
        [
            ToolCallEvent(call_id="a", name="weather__get", arguments={"city": "Sydney"}),
            ToolCallEvent(call_id="b", name="system__think", arguments={"thought": "plan"}),
        ],
        [ToolCallEvent(call_id="c", name="calendar__get_events", arguments={"start_date": "today"})],
        [TextEvent(text="Done.")],
    ])
    order: list[str] = []

    async def fake_dispatch(call: CapabilityCall) -> CapabilityOutcome:
        order.append(call.call_id)
        return _outcome(call, f"ok-{call.call_id}")

    def resolve(name: str):
        return SimpleNamespace(fqn=name.replace("__", "."))

    agent = JarvisAgent(llm)
    agent.prompt_builder.build = MagicMock(return_value="")

    with (
        patch("core.agent.agent.dispatcher.dispatch", side_effect=fake_dispatch),
        patch("core.agent.agent.registry.resolve_provider_name", side_effect=resolve),
    ):
        events = await _run(agent)

    outputs = [event for event in events if event.type == AgentEventType.TOOL_OUTPUT]
    assert [event.tool_call_id for event in outputs] == ["a", "b", "c"]
    assert order[:2] == ["a", "b"]
    assert any(event.type == AgentEventType.TEXT and event.content == "Done." for event in events)


@pytest.mark.asyncio
async def test_discovery_promotes_fqns_into_active_set():
    llm = ScriptedLLM([
        [ToolCallEvent(call_id="s", name="system__search_tools", arguments={"query": "gmail"})],
        [ToolCallEvent(call_id="g", name="gmail__search", arguments={"q": "launch"})],
        [TextEvent(text="Found two.")],
    ])
    seen_tools: list = []

    async def fake_dispatch(call: CapabilityCall) -> CapabilityOutcome:
        if call.capability == "system.search_tools":
            return _outcome(call, {"tools": [{"fqn": "gmail.search", "name": "search"}]})
        return _outcome(call, "ok")

    def resolve(name: str):
        return SimpleNamespace(fqn=name.replace("__", "."))

    def provider_tools(fqns):
        seen_tools.append(set(fqns))
        return [{"type": "function"}]

    agent = JarvisAgent(llm)
    agent.prompt_builder.build = MagicMock(return_value="")

    with (
        patch("core.agent.agent.compact_history", AsyncMock(return_value=([], {}))),
        patch("core.agent.agent.dispatcher.dispatch", side_effect=fake_dispatch),
        patch("core.agent.agent.registry.resolve_provider_name", side_effect=resolve),
        patch("core.agent.agent.registry.provider_tools", side_effect=provider_tools),
        patch(
            "core.agent.agent.active_tool_fqns",
            side_effect=lambda routed: set(routed or ()) | {"system.search_tools"},
        ),
    ):
        events = [
            event
            async for event in agent.process_stream(
                "find launch mail",
                [],
                "conn-1",
                context={"owner_id": "geoff", "routed_tools": set()},
                max_iterations=4,
            )
        ]

    assert any("system.search_tools" in fqns for fqns in seen_tools)
    assert any(event.capability == "gmail.search" for event in events if event.type == AgentEventType.TOOL_CALL)
    assert any("gmail.search" in fqns for fqns in seen_tools[1:])


@pytest.mark.asyncio
async def test_blocked_outcome_is_returned_without_fake_success():
    llm = ScriptedLLM([
        [ToolCallEvent(call_id="d", name="files__delete", arguments={"path": "README.md"})],
        [TextEvent(text="Need approval first.")],
    ])

    async def fake_dispatch(call: CapabilityCall) -> CapabilityOutcome:
        return _outcome(
            call,
            None,
            status=InvocationStatus.BLOCKED,
            error=CapabilityErrorDetail(
                code="approval_needed",
                message="Approval needed: delete README.md. The action has not executed yet.",
            ),
        )

    agent = JarvisAgent(llm)
    agent.prompt_builder.build = MagicMock(return_value="")

    with (
        patch("core.agent.agent.dispatcher.dispatch", side_effect=fake_dispatch),
        patch("core.agent.agent.registry.resolve_provider_name", return_value=SimpleNamespace(fqn="files.delete")),
    ):
        events = await _run(agent)

    output = next(event for event in events if event.type == AgentEventType.TOOL_OUTPUT)
    assert output.content.startswith("Approval needed:")
    assert "has not executed yet" in output.content
    assert output.outcome is not None
    assert output.outcome.status == InvocationStatus.BLOCKED


@pytest.mark.asyncio
async def test_chat_only_model_does_not_send_tools():
    llm = ScriptedLLM([[TextEvent(text="Hello.")]])
    agent = JarvisAgent(llm)
    agent.prompt_builder.build = MagicMock(return_value="")

    with patch("core.agent.agent.compact_history", AsyncMock(return_value=([], {}))):
        events = [
            event
            async for event in agent.process_stream(
                "hi",
                [],
                "conn-1",
                context={"owner_id": "geoff", "action_capable": False},
                max_iterations=1,
            )
        ]

    assert llm.tools_seen == [None]
    assert any(event.type == AgentEventType.TEXT and event.content == "Hello." for event in events)


@pytest.mark.asyncio
async def test_llm_transport_failure_yields_error_not_text():
    class FailingLLM:
        model = "test-model"
        is_initialized = True
        provider_name = "cerebras"

        async def chat_stream(self, **kwargs):
            if False:
                yield
            raise ConnectionError("offline")

    agent = JarvisAgent(FailingLLM())
    agent.prompt_builder.build = MagicMock(return_value="")
    events = await _run(agent)

    errors = [event for event in events if event.type == AgentEventType.ERROR]
    assert len(errors) == 1
    assert "language model" in errors[0].content.lower()
    assert not any(event.type == AgentEventType.TEXT for event in events)


def test_first_turn_llm_error_text_by_failure_class():
    from core.agent.agent import first_turn_llm_error_text
    from core.llm.service import LLMFirstTokenTimeoutError

    cloud = SimpleNamespace(provider_name="cerebras", is_initialized=True)
    local = SimpleNamespace(provider_name="ollama", is_initialized=True)
    unset = SimpleNamespace(provider_name="cerebras", is_initialized=False)

    assert "API key" in first_turn_llm_error_text(Exception("401 invalid"), cloud)
    assert "still loading" in first_turn_llm_error_text(LLMFirstTokenTimeoutError(), local)
    assert "too long" in first_turn_llm_error_text(LLMFirstTokenTimeoutError(), cloud)
    assert "setup is incomplete" in first_turn_llm_error_text(ConnectionError("offline"), unset)
    assert "reaching my language model" in first_turn_llm_error_text(ConnectionError("offline"), cloud)
