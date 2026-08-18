"""Shared activity feed contract for UI and capability tools."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from core.triggers.vocabulary import DeliveryTraceTag

ActivityKind = Literal["headless", "task", "trigger", "automation", "user"]
ActivityOutcome = Literal["completed", "failed", "awaiting", "running"]
ActivityCategory = Literal["conversation", "reminder", "automation", "task", "system"]
ActivityPageOutcome = Literal["succeeded", "failed", "waiting", "running", "suppressed", "cancelled"]
ActivityDetailKind = Literal["turn", "trigger_instance", "background_task"]


class ActivityTraceLine(BaseModel):
    role: str
    content: str
    turn_type: str | None = None


class ActivityItem(BaseModel):
    kind: ActivityKind
    id: str
    summary: str
    when: str
    sort_at: str
    outcome: ActivityOutcome
    delivery: DeliveryTraceTag | None = None
    source: str | None = None
    failure_label: str | None = Field(
        default=None,
        description="Human-readable failure reason for failed runs; omitted otherwise.",
    )
    trace: list[ActivityTraceLine] | None = Field(
        default=None,
        description="Compact execution lines for headless turns; omitted for tasks.",
    )


class ActivityDetailRef(BaseModel):
    kind: ActivityDetailKind
    id: str


class ActivityEntry(BaseModel):
    """Compact, read-only timeline pointer; canonical detail stays in domain stores."""

    activity_id: str
    category: ActivityCategory
    occurred_at: datetime
    updated_at: datetime | None = None
    outcome: ActivityPageOutcome
    title: str
    summary: str | None = None
    source_key: str | None = None
    source_label: str | None = None
    delivery: DeliveryTraceTag | None = None
    detail_ref: ActivityDetailRef
    turn_id: str | None = None
    instance_id: str | None = None
    task_id: str | None = None
    rule_id: str | None = None
    node_id: str | None = None
    failure_label: str | None = None


class ActivityPage(BaseModel):
    items: list[ActivityEntry]
    next_cursor: str | None = None
    has_more: bool = False
