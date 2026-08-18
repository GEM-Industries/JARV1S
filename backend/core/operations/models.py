"""Read models for Operations run drill-down."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


OperationRunKind = Literal["trigger", "automation", "user", "system"]


class OperationTraceLine(BaseModel):
    timestamp: datetime
    role: str
    content: str
    turn_type: str | None = None
    tool_call_id: str | None = None
    code: str | None = None
    output: str | None = None
    response_id: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    focus_tools: list[str] = Field(default_factory=list)
    invocations: list[dict[str, Any]] = Field(default_factory=list)


class OperationPerfStage(BaseModel):
    key: str
    label: str | None = None
    detail: str | None = None
    ms: float | None = None
    group: str | None = None
    status: str | None = None


class OperationPerfSummary(BaseModel):
    status: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    response_ms: float | None = None
    total_ms: float | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_chars: int | None = None
    stages: list[OperationPerfStage] = Field(default_factory=list)
    stt: dict[str, Any] | None = None
    turn_detection: dict[str, Any] | None = None
    voice: dict[str, Any] | None = None
    tool_routing: dict[str, Any] | None = None


class OperationProtocolRun(BaseModel):
    protocol_name: str
    triggered_by: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: str


class OperationTurnAttempt(BaseModel):
    turn_id: str
    trace: list[OperationTraceLine] = Field(default_factory=list)
    perf: OperationPerfSummary | None = None
    protocols: list[OperationProtocolRun] = Field(default_factory=list)


class OperationRunDetail(BaseModel):
    id: str
    kind: OperationRunKind
    owner_id: str
    status: str
    rule_id: str | None = None
    source: str | None = None
    due_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    completed_at: datetime | None = None
    started_at: datetime | None = None
    result_text: str | None = None
    failure_reason: str | None = None
    node_id: str | None = None
    node_label: str | None = None
    modality: str | None = None
    origin_snapshot: dict[str, Any] = Field(default_factory=dict)
    action_snapshot: dict[str, Any] = Field(default_factory=dict)
    source_event: dict[str, Any] = Field(default_factory=dict)
    turn_ids: list[str] = Field(default_factory=list)
    attempts: list[OperationTurnAttempt] = Field(default_factory=list)
