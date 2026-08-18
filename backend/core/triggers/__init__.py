"""Trigger pipeline.

Provides unified models, presets, service, and scheduler for all proactive
delivery: reminders, timers, alarms, automation-driven actions, system
pulse findings, and background-agent completions.
"""

from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    TriggerAction,
    TriggerCondition,
    TriggerInstance,
    TriggerRule,
    TriggerOrigin,
)
from core.triggers.delivery_policy import (
    TriggerDeliveryResolution,
    resolve_proactive_speech_delivery,
    resolve_trigger_delivery,
)
from core.triggers.service import TriggerService

__all__ = [
    "AttentionPolicy",
    "DeliveryPlan",
    "FreshnessPolicy",
    "TriggerAction",
    "TriggerCondition",
    "TriggerInstance",
    "TriggerRule",
    "TriggerOrigin",
    "TriggerDeliveryResolution",
    "TriggerService",
    "resolve_proactive_speech_delivery",
    "resolve_trigger_delivery",
]
