import pytest

import api.routes.automations as automations_route
from core.operations import AutomationDefinitionSummary
from core.config import settings


@pytest.mark.asyncio
async def test_list_automations_delegates_to_operations_read_model(monkeypatch):
    seen_owner_ids: list[str] = []

    async def fake_list_automation_definitions(owner_id: str):
        seen_owner_ids.append(owner_id)
        return [
            AutomationDefinitionSummary(
                id="auto-1",
                name="Meeting Reminder",
                enabled=True,
                importance="normal",
                trigger={"source": "calendar", "event": "starting", "offset": -1},
                decision="offer",
            )
        ]

    monkeypatch.setattr(
        automations_route,
        "list_automation_definitions",
        fake_list_automation_definitions,
    )
    result = await automations_route.list_automations(owner_id=settings.DEFAULT_USER_ID)

    assert len(result) == 1
    assert result[0].id == "auto-1"
    assert seen_owner_ids == [settings.DEFAULT_USER_ID]
