from unittest.mock import AsyncMock

import pytest

from core.operations.projection import ManagedSetup
from core.operations.lifecycle_dispatch import pause_managed_setup
from core.plugins.read_evidence import MatchStatus, ReadCoverage
from core.triggers.lifecycle import is_indefinite_pause
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

    assert result.setups == sample
    assert result.match_status == MatchStatus.SINGLE
    assert result.coverage == ReadCoverage.COMPLETE
    assert result.hold is None
    find.assert_awaited_once_with("owner-1", query="morning", status=None, setup_type=None)


@pytest.mark.asyncio
async def test_setups_find_empty_is_complete_none(monkeypatch):
    find = AsyncMock(return_value=[])
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.find_managed_setups", find)

    result = await SetupsPlugin().find(query="meeting")

    assert result.setups == []
    assert result.match_status == MatchStatus.NONE
    assert result.coverage == ReadCoverage.COMPLETE
    assert result.hold is None


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


_AUTOMATION_HOLD = (
    "External automations are globally paused. Matching rules stay active "
    "but will not fire. Call automations.resume_all."
)


def _paused_engine(monkeypatch) -> None:
    monkeypatch.setattr(
        "services.automation.automation_service.pause_observation",
        lambda now=None: _AUTOMATION_HOLD,
    )


@pytest.mark.asyncio
async def test_setups_find_hold_keeps_automation_status_active(monkeypatch):
    sample = [
        ManagedSetup(
            kind="automation",
            name="Meeting Reminder",
            resource_ref="automations:automation:rule-1",
            resource_id="rule-1",
            managed_by="automations",
            setup_type="automation",
            status="active",
            supported_actions=["pause", "resume", "delete"],
        )
    ]
    find = AsyncMock(return_value=sample)
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.find_managed_setups", find)
    _paused_engine(monkeypatch)

    result = await SetupsPlugin().find(query="meeting")

    assert result.setups[0].status == "active"
    assert result.hold == _AUTOMATION_HOLD


@pytest.mark.asyncio
async def test_setups_find_paused_filter_is_unchanged(monkeypatch):
    paused = [
        ManagedSetup(
            kind="automation",
            name="Meeting Reminder",
            resource_ref="automations:automation:rule-1",
            resource_id="rule-1",
            managed_by="automations",
            setup_type="automation",
            status="paused",
            supported_actions=["pause", "resume", "delete"],
        )
    ]
    find = AsyncMock(return_value=paused)
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.find_managed_setups", find)
    _paused_engine(monkeypatch)

    result = await SetupsPlugin().find(status="paused")

    find.assert_awaited_once_with(
        "owner-1", query=None, status="paused", setup_type=None
    )
    assert [row.status for row in result.setups] == ["paused"]
    assert result.hold == _AUTOMATION_HOLD


@pytest.mark.asyncio
async def test_setups_find_scheduler_query_has_no_hold_when_engine_paused(monkeypatch):
    sample = [
        ManagedSetup(
            kind="deferred_instruction",
            name="Morning Wakeup Lights",
            resource_ref="scheduler:schedule:rule-1",
            resource_id="rule-1",
            managed_by="scheduler",
            setup_type="schedule",
            status="active",
            supported_actions=["pause", "resume", "delete"],
        )
    ]
    find = AsyncMock(return_value=sample)
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr("plugins.setups.find_managed_setups", find)
    _paused_engine(monkeypatch)

    result = await SetupsPlugin().find(query="morning lights")

    assert result.hold is None
    assert result.setups[0].status == "active"


@pytest.mark.asyncio
async def test_setups_get_returns_one_setup(monkeypatch):
    row = ManagedSetup(
        kind="automation",
        name="Meeting Reminder",
        resource_ref="automations:automation:rule-1",
        resource_id="rule-1",
        managed_by="automations",
        setup_type="automation",
        status="active",
        supported_actions=["pause", "resume", "delete"],
    )
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(
        "plugins.setups.resolve_managed_setup",
        AsyncMock(return_value=row),
    )

    result = await SetupsPlugin().get("meeting")

    assert result == row


@pytest.mark.asyncio
async def test_setups_pause_applies_to_matching_set(monkeypatch):
    rows = [
        ManagedSetup(
            kind="deferred_instruction",
            name="Morning Lights AM",
            resource_ref="scheduler:schedule:a",
            resource_id="a",
            series_id="a",
            managed_by="scheduler",
            setup_type="schedule",
            supported_actions=["pause", "resume", "delete"],
        ),
        ManagedSetup(
            kind="deferred_instruction",
            name="Morning Lights PM",
            resource_ref="scheduler:schedule:b",
            resource_id="b",
            series_id="b",
            managed_by="scheduler",
            setup_type="schedule",
            supported_actions=["pause", "resume", "delete"],
        ),
    ]
    pause = AsyncMock(side_effect=lambda _owner, row, until=None: f"Paused {row.name}.")
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(
        "plugins.setups.resolve_managed_setup",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr("plugins.setups.pause_managed_setup", pause)

    result = await SetupsPlugin().pause("lights")

    assert result == "Paused Morning Lights AM, Morning Lights PM."
    assert pause.await_count == 2


@pytest.mark.asyncio
async def test_setups_pause_mixed_set_is_fail_closed(monkeypatch):
    rows = [
        ManagedSetup(
            kind="deferred_instruction",
            name="Morning Lights",
            resource_ref="scheduler:schedule:a",
            resource_id="a",
            series_id="a",
            managed_by="scheduler",
            setup_type="schedule",
            supported_actions=["pause", "resume", "delete"],
        ),
        ManagedSetup(
            kind="protocol",
            name="Lights protocol",
            resource_ref="protocol:protocol:p1",
            resource_id="p1",
            managed_by="protocol",
            setup_type="protocol",
            supported_actions=["delete"],
        ),
    ]
    pause = AsyncMock()
    monkeypatch.setattr("plugins.setups.get_owner_id", lambda: "owner-1")
    monkeypatch.setattr(
        "plugins.setups.resolve_managed_setup",
        AsyncMock(return_value=rows),
    )
    monkeypatch.setattr("plugins.setups.pause_managed_setup", pause)

    result = await SetupsPlugin().pause("lights")

    assert result.message.startswith("Cannot pause")
    pause.assert_not_awaited()


@pytest.mark.asyncio
async def test_pause_managed_setup_indefinite_keeps_enabled(monkeypatch):
    row = ManagedSetup(
        kind="deferred_instruction",
        name="Morning Lights",
        resource_ref="scheduler:schedule:rule-1",
        resource_id="rule-1",
        series_id="rule-1",
        managed_by="scheduler",
        setup_type="schedule",
        supported_actions=["pause", "resume", "delete"],
    )
    patch_lifecycle = AsyncMock()
    monkeypatch.setattr(
        "core.operations.lifecycle_dispatch.patch_rule_lifecycle",
        patch_lifecycle,
    )

    result = await pause_managed_setup("owner-1", row)

    assert result == "Paused Morning Lights."
    patch = patch_lifecycle.await_args.args[2]
    assert patch.enabled is True
    assert is_indefinite_pause(patch.paused_until)
