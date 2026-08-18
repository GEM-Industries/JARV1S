from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from plugins.agents import client


@pytest.mark.asyncio
async def test_complete_task_keeps_completed_when_completion_trigger_fails():
    task_doc = {"task_id": "task-1", "status": "completed"}
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(return_value=task_doc),
        update_one=AsyncMock(),
    )

    with (
        patch("plugins.agents.client.mongodb") as mock_mongo,
        patch("plugins.agents.client._push_widget", new=AsyncMock()) as push_widget,
        patch("plugins.agents.client._push_task_progress_receipt", new=AsyncMock()),
        patch(
            "plugins.agents.client._publish_completion_trigger",
            new=AsyncMock(side_effect=RuntimeError("trigger insert failed")),
        ) as publish,
    ):
        mock_mongo.get_collection.return_value = collection

        await client._complete_task(
            "task-1",
            "geoff",
            "result",
            "summary",
            session_id=None,
            cost_usd=None,
        )

    push_widget.assert_awaited_once_with("geoff", "task-1", task_doc)
    publish.assert_awaited_once()
    collection.update_one.assert_awaited_once_with(
        {"task_id": "task-1"},
        {"$set": {"completion_notification_error": "trigger insert failed"}},
    )
    update = collection.find_one_and_update.await_args.args[1]["$set"]
    assert update["status"] == "completed"


@pytest.mark.asyncio
async def test_complete_task_skips_completion_trigger_when_update_misses():
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(return_value=None),
        update_one=AsyncMock(),
    )

    with (
        patch("plugins.agents.client.mongodb") as mock_mongo,
        patch("plugins.agents.client._push_widget", new=AsyncMock()) as push_widget,
        patch(
            "plugins.agents.client._publish_completion_trigger", new=AsyncMock()
        ) as publish,
    ):
        mock_mongo.get_collection.return_value = collection

        await client._complete_task(
            "task-missing",
            "geoff",
            "result",
            "summary",
            session_id=None,
            cost_usd=None,
        )

    push_widget.assert_not_awaited()
    publish.assert_not_awaited()
    collection.update_one.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_completion_trigger_uses_task_dedup_key():
    trigger_instance = SimpleNamespace(id="trg-1")

    with (
        patch("plugins.agents.client.trigger_service") as trigger_service,
        patch("plugins.agents.client.event_bus") as event_bus,
    ):
        trigger_service.create_instance = AsyncMock(return_value=trigger_instance)
        event_bus.publish = AsyncMock()

        await client._publish_completion_trigger(
            owner_id="geoff",
            task_id="task-1",
            summary="done",
        )

    assert (
        trigger_service.create_instance.await_args.kwargs["dedup_key"]
        == "task-complete:task-1"
    )
    event_bus.publish.assert_awaited_once()
