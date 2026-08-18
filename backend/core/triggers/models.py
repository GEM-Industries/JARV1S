"""Pydantic models for the trigger pipeline."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.triggers.priority import AttentionLevel
from core.triggers.vocabulary import ContentType, TriggerDecision


def _has_content_source(*, message: str, instructions: str | None, protocol_name: str | None) -> bool:
    return bool((message or "").strip() or (instructions or "").strip() or protocol_name)


def validate_trigger_action_fields(
    *,
    decision: TriggerDecision,
    message: str,
    instructions: str | None,
    protocol_name: str | None,
) -> None:
    if decision in {"tell", "offer"} and not _has_content_source(
        message=message, instructions=instructions, protocol_name=protocol_name
    ):
        raise ValueError(
            f"{decision} trigger actions require message, instructions, or protocol_name"
        )
    if decision == "act" and not (instructions or "").strip() and not protocol_name:
        raise ValueError("act trigger actions require instructions or protocol_name")


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


class TriggerOrigin(BaseModel):
    kind: Literal["time", "interval", "external", "manual", "system"]

    # time / interval
    fire_at: datetime | None = None
    duration_s: int | None = None
    recurrence: str | None = None
    timezone: str | None = None
    original_local_time: str | None = None

    # external
    source: str | None = None   # "calendar", "gmail", "slack"
    event: str | None = None    # normalized event type
    offset_minutes: int = 0


# ---------------------------------------------------------------------------
# Condition
# ---------------------------------------------------------------------------


class TriggerCondition(BaseModel):
    """Cheap deterministic filter evaluated before agent work."""
    kind: str
    parameters: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


class TriggerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: TriggerDecision = "tell"
    message: str = ""
    protocol_name: str | None = None

    # Fire-time policy and prompt classification.
    instructions: str | None = None
    content_type: ContentType | None = None
    # Scalar semantic frame for the proactive utterance and its first reply.
    # Ownership belongs in management; fire data in source_event.
    reply_grounding: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_action(self) -> "TriggerAction":
        validate_trigger_action_fields(
            decision=self.decision,
            message=self.message,
            instructions=self.instructions,
            protocol_name=self.protocol_name,
        )
        return self


# ---------------------------------------------------------------------------
# Attention policy
# ---------------------------------------------------------------------------


class AttentionPolicy(BaseModel):
    # Priority axis only: how hard this tries to reach the user. Orthogonal to
    # decision (whether to speak) and presentation (sound / requires_ack).
    level: AttentionLevel = "normal"
    requires_ack: bool = False
    sound: Literal["none", "chime", "timer", "alarm"] = "chime"


# ---------------------------------------------------------------------------
# Delivery plan
# ---------------------------------------------------------------------------


class DeliveryTargetHint(BaseModel):
    """Author-time hint for where a proactive delivery should be routed."""

    node_id: str | None = None
    # Same keys as ``LocationRef.model_dump()``.
    location_ref: dict[str, str | None] | None = None


DeliveryFallback = Literal["none", "follow_me_if_target_unavailable"]


class DeliveryPlan(BaseModel):
    """Physical delivery routing for a trigger fire.

    Whether the user hears from JARV1S is ``TriggerAction.decision``. This plan
    holds channel/target hints and will grow fire-time resolved endpoints.
    """
    model_config = ConfigDict(extra="forbid")

    channel: Literal["voice"] = "voice"
    target: DeliveryTargetHint | None = None
    fallback: DeliveryFallback = "none"


# ---------------------------------------------------------------------------
# Freshness policy
# ---------------------------------------------------------------------------


class FreshnessPolicy(BaseModel):
    """Deterministic staleness contract for trigger delivery/replay."""

    expires_at: datetime | None = None
    expire_after_due_s: int | None = None
    stale_if_source_event_started: bool = False
    on_expiry: Literal["expire", "force_deliver"] = "expire"


# ---------------------------------------------------------------------------
# Management ownership
# ---------------------------------------------------------------------------


class ManagementOwnership(BaseModel):
    """Domain that owns mutation of a trigger artifact."""

    provider: str
    resource_id: str | None = None


# ---------------------------------------------------------------------------
# Rule
# ---------------------------------------------------------------------------


class TriggerRule(BaseModel):
    id: str
    owner_id: str
    name: str
    description: str | None = None
    enabled: bool = True
    surface: bool = True
    created_at: datetime
    updated_at: datetime

    origin: TriggerOrigin
    conditions: list[TriggerCondition] = Field(default_factory=list)
    action: TriggerAction
    attention: AttentionPolicy
    delivery: DeliveryPlan
    freshness: FreshnessPolicy

    paused_until: datetime | None = None
    exceptions: list[str] = Field(default_factory=list)
    suppressed_event_ids: list[str] = Field(default_factory=list)
    management: ManagementOwnership

    @model_validator(mode="after")
    def _validate_management_resource(self) -> "TriggerRule":
        if not self.management.resource_id:
            raise ValueError("trigger rule management requires resource_id")
        return self


# ---------------------------------------------------------------------------
# Instance (one concrete fire of a rule, or a standalone one-shot)
# ---------------------------------------------------------------------------


TriggerStatus = Literal[
    "pending",
    "claimed",
    "executing",
    "awaiting_delivery",
    "completed",
    "delivered",
    "acknowledged",
    "snoozed",
    "suppressed",
    "expired",
    "cancelled",
    "failed",
]


class TriggerInstance(BaseModel):
    id: str
    rule_id: str | None = None
    owner_id: str
    status: TriggerStatus

    due_at: datetime
    created_at: datetime
    claimed_at: datetime | None = None
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None
    completed_at: datetime | None = None

    origin_snapshot: TriggerOrigin
    action_snapshot: TriggerAction
    attention_snapshot: AttentionPolicy
    delivery_snapshot: DeliveryPlan
    freshness_snapshot: FreshnessPolicy

    source_event: dict[str, Any] = Field(default_factory=dict)
    turn_ids: list[str] = Field(default_factory=list)
    next_retry_at: datetime | None = None
    dedup_key: str | None = None
    result_text: str | None = None
    failure_reason: str | None = None
    management: ManagementOwnership

    @model_validator(mode="after")
    def _validate_management_resource(self) -> "TriggerInstance":
        if not self.management.resource_id:
            raise ValueError("trigger instance management requires resource_id")
        return self

