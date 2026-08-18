from unittest.mock import AsyncMock

import pytest

import api.routes.history as history_route
from api.routes.history import _to_history_items


def test_history_projects_user_suppressed_turn_as_input_plus_notice():
    items = _to_history_items(
        {
            "role": "user",
            "content": "Don't respond to this message.",
            "timestamp": "2026-06-03T01:47:00+00:00",
            "metadata": {
                "delivery": "suppressed",
                "turn_status": "completed",
                "turn_id": "turn-1",
            },
        },
    )

    assert items == [
        {
            "role": "user",
            "content": "Don't respond to this message.",
            "timestamp": "2026-06-03T01:47:00+00:00",
            "type": "text",
            "turn_id": "turn-1",
        },
        {
            "role": "system",
            "content": "Jarvis didn't reply.",
            "timestamp": "2026-06-03T01:47:00+00:00",
            "type": "notice",
        },
    ]


def test_history_drops_hidden_assistant_no_reply_trace():
    items = _to_history_items(
        {
            "role": "assistant",
            "content": "NO_REPLY",
            "timestamp": "2026-06-03T01:47:01+00:00",
            "metadata": {
                "delivery": "suppressed",
                "turn_type": "text_only",
                "turn_id": "turn-1",
            },
        },
    )

    assert items == []


def test_history_projects_reasoning_row():
    items = _to_history_items(
        {
            "role": "assistant",
            "content": "Checking calendar conflicts first.",
            "timestamp": "2026-06-03T01:47:02+00:00",
            "metadata": {
                "turn_type": "reasoning",
                "response_id": "resp-1",
                "turn_id": "turn-1",
            },
        },
    )

    assert items == [
        {
            "role": "assistant",
            "content": "Checking calendar conflicts first.",
            "timestamp": "2026-06-03T01:47:02+00:00",
            "type": "reasoning",
            "response_id": "resp-1",
            "turn_id": "turn-1",
        },
    ]


def test_history_pairs_tool_result_with_tool_call():
    code_items_by_tool_call_id: dict[str, dict] = {}

    tool_call_items = _to_history_items(
        {
            "role": "assistant",
            "content": "thinking\n```python\nawait jarvis.todo.add_task('x')\n```",
            "timestamp": "2026-05-25T11:00:00+00:00",
            "metadata": {
                "turn_type": "tool_call",
                "tool_call_id": "tc-1",
                "spoken": "",
                "capability": "todo.add_task",
                "arguments": {"title": "x"},
            },
        },
        code_items_by_tool_call_id,
    )

    tool_result_items = _to_history_items(
        {
            "role": "user",
            "content": "<tool_result>\nAdded task.\n</tool_result>",
            "timestamp": "2026-05-25T11:00:01+00:00",
            "metadata": {
                "turn_type": "tool_result",
                "tool_call_id": "tc-1",
                "output": "Added task.",
            },
        },
        code_items_by_tool_call_id,
    )

    assert tool_result_items == []
    assert tool_call_items == [
        {
            "role": "assistant",
            "content": "",
            "timestamp": "2026-05-25T11:00:00+00:00",
            "type": "code",
            "code": 'todo.add_task({"title":"x"})',
            "code_result": "Added task.",
            "tool_call_id": "tc-1",
        }
    ]


@pytest.mark.asyncio
async def test_history_fetches_extra_raw_rows_then_trims_visible_items(monkeypatch):
    raw_messages = [
        {"role": "user", "content": "first", "timestamp": "2026-05-25T11:00:00+00:00"},
        {"role": "user", "content": "second", "timestamp": "2026-05-25T11:00:01+00:00"},
        {"role": "user", "content": "third", "timestamp": "2026-05-25T11:00:02+00:00"},
    ]
    get_history = AsyncMock(return_value=raw_messages)
    monkeypatch.setattr(history_route.mongodb, "get_history", get_history)

    items = await history_route.get_history(limit=2)

    assert get_history.await_args.kwargs["limit"] == 6
    assert "exclude_deliveries" not in get_history.await_args.kwargs
    assert [item["content"] for item in items] == ["second", "third"]
