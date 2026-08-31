"""Owner-scoped mutations for surfaced proactive setups."""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel

from core.operations.definitions import SetupSummary, list_setups
from core.operations.events import OperationScope, publish_operations_changed
from core.triggers.lifecycle import (
    cancel_open_instances_for_rule,
    is_scheduler_managed,
    materialize_after_pause,
    resolved_pause_until,
    rule_allows_dispatch,
    rule_management,
)
from core.triggers.models import TriggerRule
from services.database.mongodb import mongodb


class SetupPatch(BaseModel):
    enabled: bool | None = None
    paused_until: datetime | None = None


class SetupMutationError(ValueError):
    pass


def definition_pause_patch(until: datetime | None = None) -> SetupPatch:
    """Pause a definition without disabling it. Omit until for indefinite."""
    return SetupPatch(enabled=True, paused_until=resolved_pause_until(until))


def _rule_id_from_setup_id(setup_id: str) -> str:
    if setup_id.startswith("protocol:"):
        raise SetupMutationError("Saved routines are read-only here")
    rule_id = setup_id.removeprefix("rule:")
    if not rule_id:
        raise SetupMutationError("A rule setup id is required")
    return rule_id


async def _load_surfaced_rule(owner_id: str, setup_id: str) -> TriggerRule:
    rule_id = _rule_id_from_setup_id(setup_id)
    raw = await mongodb.db.trigger_rules.find_one(
        {"owner_id": owner_id, "id": rule_id, "surface": True},
        {"_id": 0},
    )
    if raw is None:
        raise SetupMutationError(f"Setup {setup_id} not found")
    return TriggerRule.model_validate(raw)


def _scope_for_rule(rule: TriggerRule) -> OperationScope:
    return "automations" if rule.origin.kind == "external" else "schedules"


async def patch_rule_lifecycle(
    owner_id: str,
    setup_id: str,
    patch: SetupPatch,
) -> SetupSummary:
    """Validate and update one TriggerRule, then return its projected summary."""
    if not patch.model_fields_set:
        raise SetupMutationError("No setup changes were provided")

    rule = await _load_surfaced_rule(owner_id, setup_id)
    if rule_management(rule.model_dump(mode="python")).provider not in {
        "scheduler",
        "automations",
    }:
        raise SetupMutationError(f"Setup {setup_id} is managed by another domain")
    rule_id = rule.id
    updates: dict[str, object] = {"updated_at": datetime.now(timezone.utc)}
    if "enabled" in patch.model_fields_set:
        if patch.enabled is None:
            raise SetupMutationError("enabled cannot be null")
        updates["enabled"] = patch.enabled
    if "paused_until" in patch.model_fields_set:
        updates["paused_until"] = patch.paused_until

    candidate = {**rule.model_dump(mode="python"), **updates}
    resulting = TriggerRule.model_validate(candidate)
    result = await mongodb.db.trigger_rules.update_one(
        {"owner_id": owner_id, "id": rule_id},
        {"$set": updates},
    )
    if result.matched_count != 1:
        raise SetupMutationError(f"Setup {setup_id} not found")

    should_cancel = False
    if "enabled" in patch.model_fields_set and patch.enabled is False:
        should_cancel = True
    if "paused_until" in patch.model_fields_set and patch.paused_until is not None:
        should_cancel = True
    if should_cancel:
        await cancel_open_instances_for_rule(
            owner_id,
            rule_id,
            reason="parent_rule_paused_or_disabled",
        )
    if patch.paused_until is not None:
        await materialize_after_pause(resulting, patch.paused_until)
    elif rule_allows_dispatch(resulting):
        await materialize_after_pause(resulting, datetime.now(timezone.utc))

    await publish_operations_changed(owner_id, _scope_for_rule(rule))
    rows = await list_setups(owner_id)
    summary = next((row for row in rows if row.id == f"rule:{rule_id}"), None)
    if summary is None:
        raise SetupMutationError(f"Setup {setup_id} is no longer visible")
    return summary


async def delete_scheduler_rule(owner_id: str, setup_id: str) -> TriggerRule:
    """Delete one surfaced time-based TriggerRule and cancel open instances."""
    rule = await _load_surfaced_rule(owner_id, setup_id)
    if not is_scheduler_managed(rule.model_dump(mode="python")):
        raise SetupMutationError("Only scheduler-managed rules can be deleted here")
    await mongodb.db.trigger_rules.update_one(
        {"owner_id": owner_id, "id": rule.id},
        {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}},
    )
    await cancel_open_instances_for_rule(owner_id, rule.id, reason="user_deleted")
    result = await mongodb.db.trigger_rules.delete_one(
        {"owner_id": owner_id, "id": rule.id, "surface": True},
    )
    if result.deleted_count != 1:
        raise SetupMutationError(f"Setup {setup_id} not found")

    await publish_operations_changed(owner_id, _scope_for_rule(rule))
    return rule
