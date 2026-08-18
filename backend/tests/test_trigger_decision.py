"""Tests for trigger decision-axis delivery resolution and validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from core.triggers.delivery_policy import resolve_trigger_delivery
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    TriggerAction,
    validate_trigger_action_fields,
)
from core.triggers.presets import deferred_instruction_preset


def _policy(level: str = "normal") -> AttentionPolicy:
    return AttentionPolicy(level=level)


def _delivery() -> DeliveryPlan:
    return DeliveryPlan()


def test_act_resolves_to_never() -> None:
    preset = deferred_instruction_preset(
        owner_id="geoff",
        instruction="Turn off the living room light.",
        fire_at=datetime.now(timezone.utc),
    )
    assert preset["action"].decision == "act"

    resolution = resolve_trigger_delivery(
        attention_mode="active",
        attention=_policy("critical"),
        delivery=_delivery(),
        decision=preset["action"].decision,
    )
    assert resolution.agent_execution == "headless"
    assert resolution.presentation == "never"
    assert resolution.reason == "decision_act"


def test_offer_resolves_to_if_content() -> None:
    resolution = resolve_trigger_delivery(
        attention_mode="paused",
        attention=_policy("critical"),
        delivery=_delivery(),
        decision="offer",
    )
    assert resolution.agent_execution == "headless"
    assert resolution.presentation == "if_content"
    assert resolution.delivery_tag == "evaluate"
    assert resolution.reason == "decision_offer"
    assert resolution.blocked_result is None


def test_tell_resolves_to_always() -> None:
    resolution = resolve_trigger_delivery(
        attention_mode="quiet",
        attention=_policy("normal"),
        delivery=_delivery(),
        decision="tell",
    )
    assert resolution.agent_execution == "user_facing"
    assert resolution.presentation == "always"
    assert resolution.blocked_result == "awaiting_delivery"
    assert resolution.reason == "quiet_deferred"


def test_trigger_action_validation_rules() -> None:
    with pytest.raises(ValidationError, match="tell trigger actions require"):
        TriggerAction(decision="tell", message="")

    with pytest.raises(ValidationError, match="offer trigger actions require"):
        TriggerAction(decision="offer", message="")

    with pytest.raises(ValidationError, match="act trigger actions require"):
        TriggerAction(decision="act", message="do work")

    TriggerAction(decision="tell", message="Standup in 5")
    TriggerAction(decision="offer", message="Briefing", instructions="only if free")
    TriggerAction(decision="act", instructions="Archive matching mail")
    TriggerAction(decision="act", protocol_name="nightly_backup")

    with pytest.raises(ValueError, match="tell trigger actions require"):
        validate_trigger_action_fields(
            decision="tell",
            message="",
            instructions=None,
            protocol_name=None,
        )


def test_trigger_models_reject_legacy_fields() -> None:
    with pytest.raises(ValidationError, match="kind"):
        TriggerAction(kind="notify", message="legacy")

    with pytest.raises(ValidationError, match="mode"):
        DeliveryPlan(mode="announce")


def test_rules_facade_rejects_legacy_action_fields() -> None:
    from plugins.rules import _parse_action

    parsed, error = _parse_action({
        "kind": "evaluate",
        "message": "legacy",
        "directive": "old field",
    })

    assert parsed is None
    assert error is not None
    assert error.message == "Unknown action field(s): directive, kind."


@pytest.mark.asyncio
async def test_rules_create_rejects_duplicate_scheduled_name(monkeypatch) -> None:
    from plugins.rules import RulesPlugin

    class Rules:
        async def find_one(self, query, projection):
            assert query["owner_id"] == "owner-1"
            return {
                "id": "rule-morning",
                "name": "Morning Wakeup Lights",
                "origin": {"kind": "time"},
            }

    monkeypatch.setattr("plugins.rules.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(
        "plugins.rules.mongodb",
        type("Mongo", (), {"db": type("DB", (), {"trigger_rules": Rules()})()})(),
    )

    result = await RulesPlugin().create(
        name="Morning Wakeup Lights",
        origin={"kind": "time", "when": "7:30am", "recurrence": "daily"},
        action={"decision": "act", "instructions": "Use warmer lights"},
        confirmed=True,
    )

    assert result.message == (
        "Scheduled rule 'Morning Wakeup Lights' already exists "
        "(series_id='rule-morning'). Update it with "
        "scheduler.replace_alert(series_id=..., ...); do not create a duplicate."
    )
