"""Dispatch common setup lifecycle operations to domain owners."""

from __future__ import annotations

from datetime import datetime

from core.attention.service import attention_service
from core.operations.projection import ManagedSetup
from core.operations.setups import (
    SetupPatch,
    definition_pause_patch,
    delete_scheduler_rule,
    patch_rule_lifecycle,
)
from core.plugins.capabilities import CapabilityErrorDetail
from core.plugins.consent import require_consent
from core.triggers.service import trigger_service
from plugins.habits import store as habit_store


def _require_action(action: str, row: ManagedSetup) -> None:
    if action not in row.supported_actions:
        raise ValueError(
            f"{row.name} does not support {action}. "
            f"Use {row.edit_tool or row.managed_by} instead."
        )


def _require_id(value: str | None, label: str, row: ManagedSetup) -> str:
    if value:
        return value
    raise ValueError(f"{row.name} is missing its {label}; run setups.find again.")


async def pause_managed_setup(owner_id: str, row: ManagedSetup, *, until: datetime | None = None) -> str:
    _require_action("pause", row)
    if row.managed_by in {"scheduler", "automations"}:
        rule_id = _require_id(row.series_id or row.rule_id, "rule identifier", row)
        await patch_rule_lifecycle(owner_id, f"rule:{rule_id}", definition_pause_patch(until))
        return f"Paused {row.name}."
    if row.managed_by == "habits":
        await habit_store.pause_checkin_plan(owner_id, row.resource_id, until=until)
        return f"Paused {row.name}."
    raise ValueError(f"{row.name} cannot be paused through setups.")


async def resume_managed_setup(owner_id: str, row: ManagedSetup) -> str:
    _require_action("resume", row)
    if row.managed_by in {"scheduler", "automations"}:
        rule_id = _require_id(row.series_id or row.rule_id, "rule identifier", row)
        setup_id = f"rule:{rule_id}"
        await patch_rule_lifecycle(
            owner_id,
            setup_id,
            SetupPatch(enabled=True, paused_until=None),
        )
        return f"Resumed {row.name}."
    if row.managed_by == "habits":
        await habit_store.resume_checkin_plan(owner_id, row.resource_id)
        return f"Resumed {row.name}."
    raise ValueError(f"{row.name} cannot be resumed through setups.")


async def delete_managed_setup(owner_id: str, row: ManagedSetup) -> str:
    _require_action("delete", row)
    if row.managed_by == "scheduler":
        if row.scope == "occurrence":
            instance_id = row.instance_id or row.resource_id
            await trigger_service.cancel_instance(instance_id, reason="user_deleted")
            return f"Deleted pending occurrence for {row.name}."
        setup_id = f"rule:{_require_id(row.series_id, 'series_id', row)}"
        await delete_scheduler_rule(owner_id, setup_id)
        return f"Deleted setup {row.name}."
    if row.managed_by == "automations":
        from plugins.automations import delete_automation_rule

        rule_id = row.rule_id or row.resource_id
        deleted = await delete_automation_rule(owner_id, rule_id)
        if deleted is None:
            raise ValueError(f"No automation found with id {rule_id!r}")
        return f"Deleted automation {row.name}."
    if row.managed_by == "habits":
        await habit_store.delete_checkin_plan(owner_id, row.resource_id)
        return f"Deleted {row.name}."
    if row.managed_by == "protocol":
        from plugins.protocol import delete_protocol

        deleted = await delete_protocol(owner_id, row.resource_id)
        if deleted is None:
            raise ValueError(f"Protocol {row.name!r} not found")
        return f"Deleted protocol {row.name}."
    if row.managed_by == "attention":
        deleted = await attention_service.delete_quiet_window(
            owner_id,
            row.resource_id,
        )
        if not deleted:
            raise ValueError(f"Quiet window {row.name!r} not found")
        return f"Deleted quiet window {row.name}."
    raise ValueError(f"{row.name} cannot be deleted through setups.")


async def delete_with_consent(owner_id: str, row: ManagedSetup) -> str | CapabilityErrorDetail:
    label = f"{row.name} ({row.resource_ref}, {row.status})"

    async def _do() -> str:
        return await delete_managed_setup(owner_id, row)

    return await require_consent(f"Permanently delete {label}?", _do)
