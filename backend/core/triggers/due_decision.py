"""Pure pre-execution decision for a due trigger instance.

Collapses the expiry and attention branches of
``AssistantOrchestrator._handle_trigger_due`` into a single typed decision so
the orchestrator can read as a short coordinator. Endpoint routing remains
after the executing transition to preserve the original ordering. This module
performs no I/O and writes no trigger state; ``TriggerService`` remains the
only lifecycle writer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from core.attention.models import AttentionMode
from core.triggers.delivery_policy import TriggerDeliveryResolution, resolve_trigger_delivery
from core.triggers.freshness import freshness_forces_delivery, trigger_expiry_reason
from core.triggers.models import TriggerInstance
from core.triggers.vocabulary import TRACE_SUPPRESSED

TriggerDueKind = Literal[
    "expire",
    "suppress",
    "complete",
    "awaiting_delivery",
    "execute",
]


@dataclass(frozen=True, slots=True)
class TriggerDueDecision:
    """What the orchestrator should do with a claimed trigger instance.

    ``expire`` / ``suppress`` / ``complete`` / ``awaiting_delivery`` settle the
    instance without entering execution. ``execute`` means the attention gate
    passed and the orchestrator should call ``mark_executing`` before routing.
    """

    kind: TriggerDueKind
    reason: str | None = None
    delivery_resolution: TriggerDeliveryResolution | None = None
    force_delivery_reason: str | None = None


def resolve_trigger_due_decision(
    *,
    instance: TriggerInstance,
    attention_mode: AttentionMode,
    now: datetime | None = None,
) -> TriggerDueDecision:
    """Resolve the pre-execution fate of a claimed trigger instance.

    Composes the existing pure freshness and delivery-policy modules. Caller
    supplies ``attention_mode`` so this stays free of service coupling.
    """
    expiry_reason = trigger_expiry_reason(instance, now=now)
    force_delivery_reason = (
        expiry_reason if freshness_forces_delivery(instance, expiry_reason) else None
    )
    if expiry_reason and not force_delivery_reason:
        return TriggerDueDecision(kind="expire", reason=expiry_reason)

    action = instance.action_snapshot
    delivery_resolution = resolve_trigger_delivery(
        attention_mode=attention_mode,
        attention=instance.attention_snapshot,
        delivery=instance.delivery_snapshot,
        decision=action.decision,
    )
    blocked = delivery_resolution.blocked_result
    if blocked == "awaiting_delivery":
        return TriggerDueDecision(
            kind="awaiting_delivery",
            reason=delivery_resolution.reason,
            force_delivery_reason=force_delivery_reason,
        )
    if blocked == TRACE_SUPPRESSED:
        return TriggerDueDecision(
            kind="suppress",
            reason=delivery_resolution.reason,
            force_delivery_reason=force_delivery_reason,
        )
    if blocked == "completed":
        return TriggerDueDecision(
            kind="complete", force_delivery_reason=force_delivery_reason
        )

    return TriggerDueDecision(
        kind="execute",
        delivery_resolution=delivery_resolution,
        force_delivery_reason=force_delivery_reason,
    )
