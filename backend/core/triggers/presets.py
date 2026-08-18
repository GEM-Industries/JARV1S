"""User-facing trigger preset constructors."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    TriggerAction,
    TriggerOrigin,
)
from core.triggers.vocabulary import DECISION_ACT, DECISION_OFFER, DECISION_TELL, TriggerDecision


def _now() -> datetime:
    return datetime.now(timezone.utc)


REMINDER_EXPIRE_AFTER_DUE_S = int(timedelta(hours=2).total_seconds())
TIMER_EXPIRE_AFTER_DUE_S = int(timedelta(minutes=10).total_seconds())
ALARM_EXPIRE_AFTER_DUE_S = int(timedelta(hours=24).total_seconds())

_REMINDER_SOUND_BY_IMPORTANCE = {
    "normal": "chime",
    "urgent": "timer",
}


def reminder_preset(
    *,
    owner_id: str,
    message: str,
    fire_at: datetime,
    recurrence: str | None = None,
    timezone_name: str | None = None,
    original_local_time: str | None = None,
    protocol_name: str | None = None,
    instructions: str | None = None,
    decision: TriggerDecision = DECISION_TELL,
    importance: Literal["normal", "urgent"] = "normal",
    reply_grounding: dict[str, Any] | None = None,
    freshness: FreshnessPolicy | None = None,
) -> dict[str, Any]:
    """A standard user-created reminder."""
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(
            kind="time",
            fire_at=fire_at,
            recurrence=recurrence,
            timezone=timezone_name,
            original_local_time=original_local_time,
        ),
        "action": TriggerAction(
            decision=decision,
            message=message,
            protocol_name=protocol_name,
            instructions=instructions,
            reply_grounding=reply_grounding or {},
        ),
        "attention": AttentionPolicy(
            level=importance,
            sound=_REMINDER_SOUND_BY_IMPORTANCE.get(importance, "chime"),
        ),
        "delivery": DeliveryPlan(),
        "freshness": freshness or FreshnessPolicy(expire_after_due_s=REMINDER_EXPIRE_AFTER_DUE_S),
    }


def timer_preset(
    *,
    owner_id: str,
    message: str,
    duration_s: int,
) -> dict[str, Any]:
    fire_at = _now() + timedelta(seconds=duration_s)
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(kind="interval", fire_at=fire_at, duration_s=duration_s),
        "action": TriggerAction(decision=DECISION_TELL, message=message),
        "attention": AttentionPolicy(level="urgent", sound="timer"),
        "delivery": DeliveryPlan(),
        "freshness": FreshnessPolicy(expire_after_due_s=TIMER_EXPIRE_AFTER_DUE_S),
    }


def deferred_instruction_preset(
    *,
    owner_id: str,
    instruction: str,
    fire_at: datetime,
    message: str | None = None,
    decision: TriggerDecision = DECISION_ACT,
) -> dict[str, Any]:
    """Schedule fire-time work; default act = do the work and stay silent."""
    display_message = message or instruction[:80]
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(kind="time", fire_at=fire_at),
        "action": TriggerAction(
            decision=decision,
            message=display_message,
            instructions=instruction,
        ),
        "attention": AttentionPolicy(level="normal", sound="none"),
        "delivery": DeliveryPlan(),
        "freshness": FreshnessPolicy(),
    }


def alarm_preset(
    *,
    owner_id: str,
    message: str,
    fire_at: datetime,
    recurrence: str | None = None,
    timezone_name: str | None = None,
    original_local_time: str | None = None,
) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(
            kind="time",
            fire_at=fire_at,
            recurrence=recurrence,
            timezone=timezone_name,
            original_local_time=original_local_time,
        ),
        "action": TriggerAction(decision=DECISION_TELL, message=message),
        "attention": AttentionPolicy(
            level="critical",
            requires_ack=True,
            sound="alarm",
        ),
        "delivery": DeliveryPlan(),
        "freshness": FreshnessPolicy(expire_after_due_s=ALARM_EXPIRE_AFTER_DUE_S),
    }


def system_preset(
    *,
    owner_id: str,
    message: str,
    decision: TriggerDecision = DECISION_OFFER,
    instructions: str | None = None,
    reply_grounding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(kind="system"),
        "action": TriggerAction(
            decision=decision,
            message=message,
            instructions=instructions,
            reply_grounding=reply_grounding or {},
        ),
        "attention": AttentionPolicy(level="normal", sound="none"),
        "delivery": DeliveryPlan(),
        "freshness": FreshnessPolicy(),
    }


def automation_preset(
    *,
    owner_id: str,
    message: str,
    source: str,
    event: str,
    decision: TriggerDecision = DECISION_TELL,
    protocol_name: str | None = None,
    instructions: str | None = None,
    content_type: Literal["plain", "event", "task_result"] | None = "event",
    reply_grounding: dict[str, Any] | None = None,
    importance: Literal["normal", "urgent", "critical"] = "normal",
    sound: Literal["none", "chime", "timer", "alarm"] = "chime",
) -> dict[str, Any]:
    return {
        "owner_id": owner_id,
        "origin": TriggerOrigin(kind="external", source=source, event=event),
        "action": TriggerAction(
            decision=decision,
            message=message,
            protocol_name=protocol_name,
            instructions=instructions,
            content_type=content_type,
            reply_grounding=reply_grounding or {},
        ),
        "attention": AttentionPolicy(level=importance, sound=sound),
        "delivery": DeliveryPlan(),
        "freshness": FreshnessPolicy(stale_if_source_event_started=source == "calendar"),
    }
