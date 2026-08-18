"""Trigger helpers for habit check-ins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.scheduling import coerce_timezone, format_local_when, parse_schedule_time
from core.triggers.models import ManagementOwnership, TriggerOrigin
from core.triggers.presets import reminder_preset
from core.triggers.service import trigger_service
from core.triggers.vocabulary import TriggerDecision

from .models import Habit, HabitCheckinKind


@dataclass(frozen=True, slots=True)
class HabitCheckinSchedule:
    instance_id: str | None
    rule_id: str | None
    when_label: str
    recurrence: str | None
    message: str


async def schedule_checkin(
    *,
    owner_id: str,
    timezone_name: str,
    habit: Habit,
    when: str,
    message: str | None = None,
    recurrence: str | None = None,
    checkin_kind: HabitCheckinKind = "habit_checkin",
    plan_id: str | None = None,
    instructions: str | None = None,
    decision: TriggerDecision = "tell",
) -> HabitCheckinSchedule:
    trigger_time = parse_schedule_time(when, timezone_name=timezone_name)
    recurrence = recurrence.lower().strip() if recurrence else None
    checkin_message = message or default_checkin_message(habit, checkin_kind=checkin_kind)
    reply_grounding = checkin_reply_grounding(
        habit,
        checkin_kind=checkin_kind,
    )

    kwargs = reminder_preset(
        owner_id=owner_id,
        message=checkin_message,
        fire_at=trigger_time,
        recurrence=recurrence,
        timezone_name=timezone_name,
        importance="normal",
        instructions=instructions,
        decision=decision,
        reply_grounding=reply_grounding,
    )

    rule_id: str | None = None
    management = ManagementOwnership(
        provider="habits",
        resource_id=plan_id or habit.id,
    )
    if recurrence:
        tz = coerce_timezone(timezone_name)
        original_local_time = trigger_time.astimezone(tz).strftime("%H:%M")
        rule = await trigger_service.create_rule(
            owner_id=owner_id,
            name=f"Habit check-in: {habit.name}"[:80],
            origin=TriggerOrigin(
                kind="time",
                fire_at=trigger_time,
                recurrence=recurrence,
                timezone=timezone_name,
                original_local_time=original_local_time,
            ),
            action=kwargs["action"],
            attention=kwargs["attention"],
            delivery=kwargs["delivery"],
            freshness=kwargs["freshness"],
            surface=False,
            management=management,
        )
        rule_id = rule.id

    instance = await trigger_service.create_instance(
        rule_id=rule_id,
        due_at=trigger_time,
        management=management,
        **kwargs,
    )
    return HabitCheckinSchedule(
        instance_id=instance.id,
        rule_id=rule_id,
        when_label=format_local_when(trigger_time),
        recurrence=recurrence,
        message=checkin_message,
    )


def checkin_reply_grounding(
    habit: Habit,
    *,
    checkin_kind: HabitCheckinKind,
) -> dict[str, Any]:
    return {
        "habit_id": habit.id,
        "habit_name": habit.name,
        "checkin_kind": checkin_kind,
    }


def default_checkin_message(
    habit: Habit,
    *,
    checkin_kind: HabitCheckinKind = "habit_checkin",
) -> str:
    target = habit.minimum_version or habit.behavior
    if checkin_kind == "review":
        return f"How did your {habit.name} habit go? A short answer is enough."
    if checkin_kind == "cue_prompt":
        return f"Time for {target} for {habit.name}."
    return f"Did you do {target} for {habit.name}? If not, you can say what got in the way."
