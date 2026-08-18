from unittest.mock import AsyncMock

import pytest

from core.operations.projection import ManagedSetup
from plugins.setups import SetupsPlugin


@pytest.mark.asyncio
async def test_setups_find_delegates_to_projection(monkeypatch):
    sample = [
        ManagedSetup(
            kind="deferred_instruction",
            name="Morning Wakeup Lights",
            resource_ref="scheduler:schedule:rule-1",
            resource_id="rule-1",
            managed_by="scheduler",
            setup_type="schedule",
            supported_actions=["pause", "resume", "delete"],
        )
    ]
    find = AsyncMock(return_value=sample)
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.find_managed_setups", find)

    result = await SetupsPlugin().find(query="morning")

    assert result == sample
    find.assert_awaited_once_with("owner-1", query="morning", status=None, setup_type=None)


@pytest.mark.asyncio
async def test_setups_get_returns_ambiguity_message(monkeypatch):
    rows = [
        ManagedSetup(
            kind="reminder",
            name="Morning Wakeup Lights AM",
            resource_ref="scheduler:schedule:a",
            resource_id="a",
            managed_by="scheduler",
            setup_type="schedule",
        ),
        ManagedSetup(
            kind="reminder",
            name="Morning Wakeup Lights PM",
            resource_ref="scheduler:schedule:b",
            resource_id="b",
            managed_by="scheduler",
            setup_type="schedule",
        ),
    ]
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.resolve_managed_setup", AsyncMock(return_value=rows))

    result = await SetupsPlugin().get("morning lights")

    assert result.message.startswith("Ambiguous setup")
