from unittest.mock import AsyncMock

import pytest

import main


@pytest.mark.asyncio
async def test_optional_service_failure_does_not_skip_later_services(monkeypatch):
    trigger_start = AsyncMock(side_effect=RuntimeError("scheduler unavailable"))
    automation_start = AsyncMock()
    monkeypatch.setattr(main.trigger_scheduler, "start", trigger_start)
    monkeypatch.setattr(main.automation_service, "start", automation_start)
    monkeypatch.setattr(main.push_registry, "start", AsyncMock())
    monkeypatch.setattr(main.inbound_event_service, "start", AsyncMock())
    monkeypatch.setattr(main.diagnostics_service, "start", AsyncMock())
    monkeypatch.setattr(main.attention_reconcile_service, "start", AsyncMock())
    monkeypatch.setattr(main.settings, "SYSTEM_PULSE_ENABLED", False)
    monkeypatch.setattr(main.settings, "PREFETCH_ENABLED", False)
    monkeypatch.setattr(main.mongodb, "db", None)

    await main._start_local_background_services()

    trigger_start.assert_awaited_once()
    automation_start.assert_awaited_once()
