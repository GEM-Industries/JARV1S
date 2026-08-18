"""Pure trigger delivery routing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from core.attention.models import AttentionMode
from core.triggers.priority import breaks_through
from core.triggers.vocabulary import (
    DELIVERY_ANNOUNCE,
    TRACE_EVALUATE,
    TriggerDecision,
    TriggerDeliveryTag,
)

if TYPE_CHECKING:
    from core.triggers.models import AttentionPolicy, DeliveryPlan


TriggerAgentExecution = Literal["user_facing", "headless"]
TriggerPresentation = Literal["always", "never", "if_content"]
TriggerBlockedResult = Literal["awaiting_delivery", "suppressed", "completed"]


@dataclass(frozen=True, slots=True)
class TriggerDeliveryResolution:
    agent_execution: TriggerAgentExecution
    presentation: TriggerPresentation
    delivery_tag: TriggerDeliveryTag
    reason: str
    blocked_result: TriggerBlockedResult | None = None

    @property
    def blocked(self) -> bool:
        return self.blocked_result is not None


def resolve_trigger_delivery(
    *,
    attention_mode: AttentionMode,
    attention: "AttentionPolicy",
    delivery: "DeliveryPlan",
    decision: TriggerDecision = "tell",
) -> TriggerDeliveryResolution:
    """Resolve agent execution, user presentation, and blocked settlement for one trigger fire."""
    if decision == "act":
        return TriggerDeliveryResolution(
            agent_execution="headless",
            presentation="never",
            delivery_tag=DELIVERY_ANNOUNCE,
            reason="decision_act",
        )

    if decision == "offer":
        return TriggerDeliveryResolution(
            agent_execution="headless",
            presentation="if_content",
            delivery_tag=TRACE_EVALUATE,
            reason="decision_offer",
        )

    speech_resolution = resolve_proactive_speech_delivery(
        attention_mode=attention_mode,
        attention=attention,
        delivery_tag=DELIVERY_ANNOUNCE,
    )
    return TriggerDeliveryResolution(
        agent_execution="user_facing",
        presentation="always",
        delivery_tag=DELIVERY_ANNOUNCE,
        reason=speech_resolution.reason,
        blocked_result=speech_resolution.blocked_result,
    )


def with_target_fallback_for_critical(
    delivery: "DeliveryPlan",
    attention: "AttentionPolicy",
) -> "DeliveryPlan":
    """Allow critical pinned deliveries to fall back to follow-me routing."""
    if attention.level == "critical" and delivery.target is not None:
        return delivery.model_copy(update={"fallback": "follow_me_if_target_unavailable"})
    return delivery


def resolve_proactive_speech_delivery(
    *,
    attention_mode: AttentionMode,
    attention: "AttentionPolicy",
    delivery_tag: TriggerDeliveryTag = TRACE_EVALUATE,
) -> TriggerDeliveryResolution:
    """Resolve whether ready proactive presentation may interrupt right now."""
    if breaks_through(attention_mode, attention.level):
        reason = "active" if attention_mode == "active" else f"{attention_mode}_passthrough"
        return TriggerDeliveryResolution(
            agent_execution="user_facing",
            presentation="always",
            delivery_tag=delivery_tag,
            reason=reason,
        )

    return TriggerDeliveryResolution(
        agent_execution="user_facing",
        presentation="always",
        delivery_tag=delivery_tag,
        reason=f"{attention_mode}_deferred",
        blocked_result="awaiting_delivery",
    )
