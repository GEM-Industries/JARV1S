from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.triggers.scheduler import TriggerScheduler
from services.events import EventType


@pytest.mark.asyncio
async def test_scheduler_requests_due_awaiting_delivery_retries():
    collection = SimpleNamespace(
        distinct=AsyncMock(return_value=["geoff"]),
        find_one=AsyncMock(return_value=None),
    )
    bus = SimpleNamespace(publish=AsyncMock())

    with (
        patch(
            "core.triggers.scheduler.mongodb",
            SimpleNamespace(db=SimpleNamespace(trigger_instances=collection)),
        ),
        patch("core.triggers.scheduler.event_bus", bus),
    ):
        await TriggerScheduler()._process_due()

    _, query = collection.distinct.await_args.args
    assert query["status"] == "awaiting_delivery"
    assert isinstance(query["next_retry_at"]["$lte"], datetime)
    event = bus.publish.await_args.args[0]
    assert event.type == EventType.TRIGGER_RETRY_AWAITING
    assert event.source == "trigger_scheduler.retry_due"
    assert event.data == {"owner_id": "geoff", "retry_due_only": True}
