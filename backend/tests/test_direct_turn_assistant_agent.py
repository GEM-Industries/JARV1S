"""Direct turns always use the configured assistant agent."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.agent.agent import AgentEvent, AgentEventType
from core.turns.delivery import HeadlessDelivery, TurnResult
from core.turns.orchestrator import AssistantOrchestrator


@pytest.mark.asyncio
async def test_headless_direct_turn_uses_assistant_agent(monkeypatch):
    assistant = MagicMock()
    assistant.llm = SimpleNamespace(model="assistant-model", supports_reasoning_effort=False)

    async def _stream(*_args, **_kwargs):
        yield AgentEvent(type=AgentEventType.TEXT, content="ok")
        if False:
            yield None

    assistant.process_stream = _stream

    orch = AssistantOrchestrator(
        stt=MagicMock(),
        llm=MagicMock(),
        agent=assistant,
        tts=MagicMock(),
    )
    monkeypatch.setattr("core.turns.execution.require_llm_ready", lambda: None)
    monkeypatch.setattr(
        "core.turns.history.load_turn_history",
        AsyncMock(
            return_value=SimpleNamespace(
                messages=[],
                reply_grounding=None,
                reply_tools=[],
            )
        ),
    )
    monkeypatch.setattr(
        "core.turns.execution.diagnostics_service",
        SimpleNamespace(record_turn_model=lambda *_: None),
    )
    monkeypatch.setattr("core.turns.execution.perf.start", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.turns.execution.perf.end", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.turns.execution.perf.log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("core.turns.execution.get_profile_block", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "core.turns.execution.build_turn_time_context",
        lambda *_args, **_kwargs: {
            "local_time": "Tuesday, 2026-07-21 00:00",
            "local_time_iso": "2026-07-21T00:00:00+00:00",
            "local_time_clock": "12:00 AM",
            "utc_time": "2026-07-21T00:00:00+00:00",
            "timezone": "UTC",
            "today_date": "2026-07-21",
            "tomorrow_date": "2026-07-22",
            "week_dates": "Tuesday=2026-07-21",
        },
    )

    result = TurnResult()
    await orch._execute_turn(
        "check calendar quietly",
        source="system",
        connection_id="conn-1",
        owner_id="owner-1",
        session_context={"timezone": "UTC"},
        text_input=False,
        attachments=None,
        delivery=HeadlessDelivery(),
        result=result,
    )

    assert result.model == "assistant-model"
