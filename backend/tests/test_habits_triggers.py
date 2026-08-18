from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from plugins.habits import HabitsPlugin
from plugins.habits.models import Habit
from plugins.habits.triggers import checkin_reply_grounding, schedule_checkin


@pytest.mark.asyncio
async def test_schedule_checkin_creates_recurring_rule_and_instance(monkeypatch) -> None:
    trigger_time = datetime(2026, 5, 24, 10, 0, tzinfo=timezone.utc)
    create_rule = AsyncMock(return_value=SimpleNamespace(id="rule-1"))
    create_instance = AsyncMock(return_value=SimpleNamespace(id="trg-1"))
    habit = Habit(
        id="hab-1",
        owner_id="owner-1",
        name="Reading",
        name_key="reading",
        behavior="read after dinner",
        cue="after dinner",
        minimum_version="one page",
    )

    monkeypatch.setattr("plugins.habits.triggers.parse_schedule_time", lambda *_, **__: trigger_time)
    monkeypatch.setattr(
        "plugins.habits.triggers.trigger_service",
        SimpleNamespace(create_rule=create_rule, create_instance=create_instance),
    )

    scheduled = await schedule_checkin(
        owner_id="owner-1",
        timezone_name="UTC",
        habit=habit,
        when="today 10:00",
        recurrence="daily",
        checkin_kind="review",
        plan_id="hchk-1",
    )

    assert scheduled.rule_id == "rule-1"
    assert scheduled.instance_id == "trg-1"
    rule_kwargs = create_rule.await_args.kwargs
    assert rule_kwargs["name"] == "Habit check-in: Reading"
    assert rule_kwargs["origin"].recurrence == "daily"
    assert rule_kwargs["action"].reply_grounding == {
        "habit_id": "hab-1",
        "habit_name": "Reading",
        "checkin_kind": "review",
    }
    assert rule_kwargs["management"].resource_id == "hchk-1"
    instance_kwargs = create_instance.await_args.kwargs
    assert instance_kwargs["rule_id"] == "rule-1"
    assert instance_kwargs["action"].message == (
        "How did your Reading habit go? A short answer is enough."
    )


def test_cue_prompt_grounding_is_semantic_only() -> None:
    habit = Habit(
        id="hab-1",
        owner_id="owner-1",
        name="Reading",
        name_key="reading",
        behavior="read after dinner",
    )

    assert checkin_reply_grounding(habit, checkin_kind="cue_prompt") == {
        "habit_id": "hab-1",
        "habit_name": "Reading",
        "checkin_kind": "cue_prompt",
    }


@pytest.mark.asyncio
async def test_schedule_checkin_tool_rejects_bad_recurrence(invoke_tool, tool_context) -> None:
    result = await invoke_tool(
        HabitsPlugin(),
        "schedule_habit_checkin",
        habit_id="hab-1",
        when="today 10:00",
        recurrence="every someday",
    )

    assert result.message.startswith("Invalid recurrence")
