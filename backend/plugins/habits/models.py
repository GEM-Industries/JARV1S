"""Models for the V0 habits plugin."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.id import generate_id
from core.triggers.vocabulary import TriggerDecision


HabitLogStatus = Literal["done", "missed", "skipped"]
HabitLogSource = Literal["voice", "text", "ui", "system"]
HabitCheckinKind = Literal["habit_checkin", "cue_prompt", "review"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Habit(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("hab-"))
    owner_id: str
    name: str
    name_key: str
    behavior: str
    cue: str | None = None
    minimum_version: str | None = None
    desired_frequency: str | None = None
    active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HabitLogDetails(BaseModel):
    metric: str
    observed_value: str
    target: str | None = None
    delta: str | None = None
    unit: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class HabitLog(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("hlog-"))
    owner_id: str
    habit_id: str
    status: HabitLogStatus
    note: str | None = None
    details: HabitLogDetails | None = None
    source: HabitLogSource = "voice"
    logged_at: datetime = Field(default_factory=utc_now)


class HabitCheckinPlan(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("hchk-"))
    owner_id: str
    habit_id: str
    checkin_kind: HabitCheckinKind = "habit_checkin"
    message: str
    when: str
    timezone: str
    recurrence: str | None = None
    instructions: str | None = None
    decision: TriggerDecision = "tell"
    rule_id: str | None = None
    initial_instance_id: str | None = None
    active: bool = True
    paused_until: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HabitLogSummary(BaseModel):
    status: HabitLogStatus
    note: str | None = None
    details: HabitLogDetails | None = None
    logged_at: datetime


class HabitCheckinSummary(BaseModel):
    id: str
    scope: Literal["plan"]
    checkin_kind: HabitCheckinKind = "habit_checkin"
    message: str
    status: str
    plan_id: str
    rule_id: str | None = None
    instance_id: str | None = None
    recurrence: str | None = None
    decision: TriggerDecision = "tell"
    next_due_at: datetime | None = None
    last_due_at: datetime | None = None


class HabitStatus(BaseModel):
    habit_id: str
    name: str
    behavior: str
    cue: str | None = None
    minimum_version: str | None = None
    desired_frequency: str | None = None
    days: int
    done: int = 0
    missed: int = 0
    skipped: int = 0
    total: int = 0
    last_status: HabitLogStatus | None = None
    last_logged_at: datetime | None = None
    recent_logs: list[HabitLogSummary] = Field(default_factory=list)
    suggested_adjustment: str | None = None


class HabitSetup(BaseModel):
    habit: Habit
    recent_status: HabitStatus
    checkins: list[HabitCheckinSummary] = Field(default_factory=list)
