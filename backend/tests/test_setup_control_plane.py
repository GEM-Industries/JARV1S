from unittest.mock import AsyncMock

import pytest

from core.operations.definitions import SetupSummary
from core.operations.lifecycle_dispatch import delete_managed_setup
from core.operations.projection import ManagedSetup, find_managed_setups


@pytest.mark.asyncio
async def test_projection_returns_compact_downstream_contract(monkeypatch) -> None:
    schedule = SetupSummary(
        id="rule:rule-1",
        source="trigger_rule",
        kind="reminder",
        name="Morning lights",
        series_id="rule-1",
        status="active",
        trigger_label="At 07:30",
    )
    list_setups = AsyncMock(side_effect=[[schedule], []])
    monkeypatch.setattr("core.operations.projection.list_setups", list_setups)
    monkeypatch.setattr("core.operations.projection._habit_checkin_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.operations.projection._quiet_window_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.operations.projection._scheduler_occurrence_rows", AsyncMock(return_value=[]))

    rows = await find_managed_setups("owner-1", query="morning")

    assert rows == [
        ManagedSetup(
            resource_ref="scheduler:schedule:rule-1",
            resource_id="rule-1",
            setup_type="schedule",
            managed_by="scheduler",
            kind="reminder",
            name="Morning lights",
            series_id="rule-1",
            status="active",
            trigger_label="At 07:30",
            supported_actions=["pause", "resume", "delete"],
            edit_tool="scheduler.replace_alert",
        )
    ]
    payload = rows[0].model_dump()
    assert "origin" not in payload
    assert "delivery" not in payload
    assert "attention" not in payload


@pytest.mark.asyncio
async def test_automation_delete_delegates_to_domain_cleanup(monkeypatch) -> None:
    row = ManagedSetup(
        resource_ref="automations:automation:rule-1",
        resource_id="rule-1",
        setup_type="automation",
        managed_by="automations",
        kind="automation",
        name="Slack mentions",
        rule_id="rule-1",
        supported_actions=["delete"],
    )
    delete_rule = AsyncMock(return_value=object())
    monkeypatch.setattr("plugins.automations.delete_automation_rule", delete_rule)

    result = await delete_managed_setup("owner-1", row)

    assert result == "Deleted automation Slack mentions."
    delete_rule.assert_awaited_once_with("owner-1", "rule-1")


@pytest.mark.asyncio
async def test_protocol_delete_delegates_to_domain_cascade(monkeypatch) -> None:
    row = ManagedSetup(
        resource_ref="protocol:protocol:protocol-1",
        resource_id="protocol-1",
        setup_type="protocol",
        managed_by="protocol",
        kind="protocol",
        name="Morning briefing",
        supported_actions=["delete"],
    )
    delete_protocol = AsyncMock(return_value={"name": row.name})
    monkeypatch.setattr("plugins.protocol.delete_protocol", delete_protocol)

    result = await delete_managed_setup("owner-1", row)

    assert result == "Deleted protocol Morning briefing."
    delete_protocol.assert_awaited_once_with("owner-1", "protocol-1")
