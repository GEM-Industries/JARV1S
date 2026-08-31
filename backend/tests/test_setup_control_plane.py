from unittest.mock import AsyncMock

import pytest

from core.operations.definitions import SetupSummary
from core.operations.lifecycle_dispatch import delete_managed_setup
from core.operations.projection import ManagedSetup, find_managed_setups, resolve_managed_setup


def _stub_projection_extras(monkeypatch) -> None:
    monkeypatch.setattr("core.operations.projection._habit_checkin_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.operations.projection._quiet_window_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr("core.operations.projection._scheduler_occurrence_rows", AsyncMock(return_value=[]))


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
    _stub_projection_extras(monkeypatch)

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
async def test_projection_omits_indefinite_pause_date(monkeypatch) -> None:
    from core.triggers.lifecycle import INDEFINITE_PAUSE

    schedule = SetupSummary(
        id="rule:rule-1",
        source="trigger_rule",
        kind="reminder",
        name="Morning lights",
        series_id="rule-1",
        status="paused",
        paused_until=INDEFINITE_PAUSE,
        trigger_label="At 07:30",
    )
    async def list_setups(_owner_id, kind=None):
        return [] if kind == "protocol" else [schedule]

    monkeypatch.setattr("core.operations.projection.list_setups", list_setups)
    _stub_projection_extras(monkeypatch)

    rows = await find_managed_setups("owner-1")

    assert rows[0].status == "paused"
    assert rows[0].paused_until is None


@pytest.mark.asyncio
async def test_projection_matches_each_query_token(monkeypatch) -> None:
    schedule = SetupSummary(
        id="rule:rule-1",
        source="trigger_rule",
        kind="reminder",
        name="Morning Wakeup Lights",
        series_id="rule-1",
        status="active",
        trigger_label="At 07:30",
    )
    async def list_setups(_owner_id, kind=None):
        return [] if kind == "protocol" else [schedule]

    monkeypatch.setattr("core.operations.projection.list_setups", list_setups)
    _stub_projection_extras(monkeypatch)

    matched = await find_managed_setups("owner-1", query="wakeup lights")
    missed = await find_managed_setups("owner-1", query="lights 7am")
    colloquial = await find_managed_setups("owner-1", query="light automations")
    spoken = await find_managed_setups("owner-1", query="all the light automations")

    assert [row.name for row in matched] == ["Morning Wakeup Lights"]
    assert missed == []
    assert [row.name for row in colloquial] == ["Morning Wakeup Lights"]
    assert [row.name for row in spoken] == ["Morning Wakeup Lights"]


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


@pytest.mark.asyncio
async def test_resolve_managed_setup_filters_by_setup_type(monkeypatch) -> None:
    schedule = SetupSummary(
        id="rule:rule-sched",
        source="trigger_rule",
        kind="reminder",
        name="Email from Helen McCosker",
        series_id="rule-sched",
        status="active",
        trigger_label="At 07:30",
    )
    automation = SetupSummary(
        id="rule:rule-helen",
        source="trigger_rule",
        kind="automation",
        name="Email from Helen McCosker",
        rule_id="rule-helen",
        status="active",
        trigger_label="gmail.new_email",
    )

    async def list_setups(_owner_id, kind=None):
        return [] if kind == "protocol" else [schedule, automation]

    monkeypatch.setattr("core.operations.projection.list_setups", list_setups)
    _stub_projection_extras(monkeypatch)

    hit = await resolve_managed_setup(
        "owner-1",
        "Email from Helen McCosker",
        setup_type="automation",
    )
    assert isinstance(hit, ManagedSetup)
    assert hit.rule_id == "rule-helen"
    assert hit.setup_type == "automation"

    schedule_hit = await resolve_managed_setup(
        "owner-1",
        "Email from Helen McCosker",
        setup_type="schedule",
    )
    assert isinstance(schedule_hit, ManagedSetup)
    assert schedule_hit.series_id == "rule-sched"

    missed = await resolve_managed_setup(
        "owner-1",
        "rule-sched",
        setup_type="automation",
    )
    assert missed is None


@pytest.mark.asyncio
async def test_resolve_managed_setup_ambiguous_query_returns_candidates(monkeypatch) -> None:
    first = SetupSummary(
        id="rule:rule-helen",
        source="trigger_rule",
        kind="automation",
        name="Email from Helen McCosker",
        rule_id="rule-helen",
        status="active",
        trigger_label="gmail.new_email",
    )
    second = SetupSummary(
        id="rule:rule-helen-cal",
        source="trigger_rule",
        kind="automation",
        name="Helen calendar digest",
        rule_id="rule-helen-cal",
        status="active",
        trigger_label="calendar.starting",
    )

    async def list_setups(_owner_id, kind=None):
        return [] if kind == "protocol" else [first, second]

    monkeypatch.setattr("core.operations.projection.list_setups", list_setups)
    _stub_projection_extras(monkeypatch)

    resolved = await resolve_managed_setup("owner-1", "Helen", setup_type="automation")
    assert isinstance(resolved, list)
    assert {row.rule_id for row in resolved} == {"rule-helen", "rule-helen-cal"}
