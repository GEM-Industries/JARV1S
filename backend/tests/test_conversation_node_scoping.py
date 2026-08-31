"""Node-scoped short-term history loading."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.database.mongodb import MongoDBService


@pytest.mark.asyncio
async def test_get_history_filters_by_node_id():
    db = MongoDBService()
    collection = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])
    collection.find.return_value.sort.return_value.limit.return_value = cursor

    with patch.object(db, "get_collection", return_value=collection):
        await db.get_history("home", node_id="bedroom")

    query = collection.find.call_args[0][0]
    assert query["metadata.node_id"] == "bedroom"


@pytest.mark.asyncio
async def test_get_history_filters_by_since():
    db = MongoDBService()
    collection = MagicMock()
    cursor = MagicMock()
    cursor.to_list = AsyncMock(return_value=[])
    collection.find.return_value.sort.return_value.limit.return_value = cursor
    since = datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", return_value=collection):
        await db.get_history("home", node_id="bedroom", since=since)

    query = collection.find.call_args[0][0]
    assert query["metadata.node_id"] == "bedroom"
    assert query["timestamp"] == {"$gte": since}


def _cursor_for(docs: list[dict]) -> MagicMock:
    cursor = MagicMock()
    cursor.sort.return_value = cursor
    cursor.limit.return_value = cursor
    cursor.to_list = AsyncMock(return_value=docs)
    return cursor


@pytest.mark.asyncio
async def test_conversation_window_empty_history_starts_now():
    db = MongoDBService()
    collection = MagicMock()
    collection.find.return_value = _cursor_for([])
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", return_value=collection):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == now


@pytest.mark.asyncio
async def test_conversation_window_keeps_contiguous_recent_block():
    db = MongoDBService()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    collection = MagicMock()
    collection.find.return_value = _cursor_for([
        {"timestamp": now - timedelta(minutes=30)},
        {"timestamp": now - timedelta(minutes=70)},
        {"timestamp": now - timedelta(minutes=95)},
    ])

    with patch.object(db, "get_collection", return_value=collection):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == now - timedelta(minutes=95)


@pytest.mark.asyncio
async def test_conversation_window_starts_after_large_gap():
    db = MongoDBService()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    collection = MagicMock()
    collection.find.return_value = _cursor_for([
        {"timestamp": now - timedelta(minutes=20)},
        {"timestamp": now - timedelta(minutes=50)},
        {"timestamp": now - timedelta(hours=3)},
    ])

    with patch.object(db, "get_collection", return_value=collection):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == now - timedelta(minutes=50)


@pytest.mark.asyncio
async def test_conversation_window_fresh_when_newest_is_stale():
    db = MongoDBService()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    collection = MagicMock()
    collection.find.return_value = _cursor_for([
        {"timestamp": now - timedelta(hours=3)},
    ])

    with patch.object(db, "get_collection", return_value=collection):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == now


@pytest.mark.asyncio
async def test_conversation_window_query_is_node_scoped_and_excludes_current_turn():
    db = MongoDBService()
    collection = MagicMock()
    collection.find.return_value = _cursor_for([])
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", return_value=collection):
        await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
            exclude_turn_id="turn-current",
        )

    query = collection.find.call_args[0][0]
    assert query["owner_id"] == "home"
    assert query["metadata.node_id"] == "bedroom"
    assert query["metadata.turn_id"] == {"$ne": "turn-current"}
    assert {"source": "user"} in query["$or"]


@pytest.mark.asyncio
async def test_conversation_window_query_includes_visible_system_activity():
    db = MongoDBService()
    collection = MagicMock()
    collection.find.return_value = _cursor_for([])
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", return_value=collection):
        await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
            visible_deliveries=["announce", "evaluate", "prefetched"],
        )

    query = collection.find.call_args[0][0]
    assert {
        "source": "system",
        "role": "assistant",
        "metadata.delivery": {"$in": ["announce", "evaluate", "prefetched"]},
    } in query["$or"]


@pytest.mark.asyncio
async def test_conversation_window_failure_starts_fresh_window():
    db = MongoDBService()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", side_effect=RuntimeError("db down")):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == now


@pytest.mark.asyncio
async def test_conversation_window_reset_floors_inactivity_start():
    db = MongoDBService()
    now = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)
    reset_at = now - timedelta(minutes=10)
    collection = MagicMock()
    collection.find.return_value = _cursor_for([
        {"timestamp": now - timedelta(minutes=30)},
        {"timestamp": now - timedelta(minutes=70)},
    ])

    with (
        patch.object(db, "get_collection", return_value=collection),
        patch.object(db, "get_conversation_window_reset", AsyncMock(return_value=reset_at)),
    ):
        since = await db.resolve_conversation_window_start(
            "home",
            "bedroom",
            gap=timedelta(hours=2),
            now=now,
        )

    assert since == reset_at


@pytest.mark.asyncio
async def test_conversation_window_reset_is_node_scoped():
    db = MongoDBService()
    stored: dict[tuple[str, str], dict] = {}
    collection = MagicMock()

    async def update_one(query, update, upsert=False):
        stored[(query["owner_id"], query["node_id"])] = dict(update["$set"])

    async def find_one(query, projection=None):
        return stored.get((query["owner_id"], query["node_id"]))

    collection.update_one = update_one
    collection.find_one = find_one
    reset_at = datetime(2026, 6, 13, 12, 0, tzinfo=timezone.utc)

    with patch.object(db, "get_collection", return_value=collection):
        await db.set_conversation_window_reset("home", "bedroom", at=reset_at)
        assert await db.get_conversation_window_reset("home", "bedroom") == reset_at
        assert await db.get_conversation_window_reset("home", "office") is None
