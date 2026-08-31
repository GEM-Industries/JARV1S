from unittest.mock import AsyncMock, MagicMock

import pytest

from services.database.mongodb import MongoDBService


class _EmptyCursor:
    def sort(self, *_args):
        return self

    def limit(self, *_args):
        return self

    async def to_list(self, *, length):
        return []


@pytest.mark.asyncio
async def test_completed_user_turn_is_embedded_for_later_recall(monkeypatch):
    collection = MagicMock()
    collection.update_one = AsyncMock()
    collection.find_one = AsyncMock(
        return_value={
            "_id": "message-1",
            "content": "I normally buy refurbished technology through online marketplaces.",
        }
    )

    service = MongoDBService()
    monkeypatch.setattr(service, "get_collection", MagicMock(return_value=collection))
    monkeypatch.setattr(
        "services.embeddings.embedding_service.embed_one",
        lambda text: [float(len(text))],
    )

    await service.mark_user_turn_status("geoff", "turn-1", "completed")

    assert collection.update_one.await_count == 2
    embedding_update = collection.update_one.await_args_list[1]
    assert embedding_update.args[0] == {
        "_id": "message-1",
        "embedding": {"$exists": False},
    }
    assert embedding_update.args[1]["$set"]["embedding"]


@pytest.mark.asyncio
async def test_embedding_failure_does_not_fail_turn_completion(monkeypatch):
    collection = MagicMock()
    collection.update_one = AsyncMock()
    collection.find_one = AsyncMock(side_effect=RuntimeError("embedding lookup failed"))

    service = MongoDBService()
    monkeypatch.setattr(service, "get_collection", MagicMock(return_value=collection))

    await service.mark_user_turn_status("geoff", "turn-1", "completed")

    collection.update_one.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_history_skip_tool_results_only_excludes_tool_result_rows(monkeypatch):
    collection = MagicMock()
    collection.find.return_value = _EmptyCursor()

    service = MongoDBService()
    monkeypatch.setattr(service, "get_collection", MagicMock(return_value=collection))

    await service.get_history("geoff", source_filter=["user"], skip_tool_results=True)

    query = collection.find.call_args.args[0]
    assert query["owner_id"] == "geoff"
    assert query["source"] == {"$in": ["user"]}
    assert query["$nor"] == [
        {"metadata.turn_type": "tool_result"},
        {"content": {"$regex": r"^\s*<tool_result>"}},
    ]


@pytest.mark.asyncio
async def test_get_history_can_exclude_current_turn(monkeypatch):
    collection = MagicMock()
    collection.find.return_value = _EmptyCursor()

    service = MongoDBService()
    monkeypatch.setattr(service, "get_collection", MagicMock(return_value=collection))

    await service.get_history("geoff", source_filter=["user"], exclude_turn_id="turn-active")

    query = collection.find.call_args.args[0]
    assert query["owner_id"] == "geoff"
    assert query["source"] == {"$in": ["user"]}
    assert query["metadata.turn_id"] == {"$ne": "turn-active"}


@pytest.mark.asyncio
async def test_get_history_excludes_suppressed_user_rows(monkeypatch):
    collection = MagicMock()
    collection.find.return_value = _EmptyCursor()

    service = MongoDBService()
    monkeypatch.setattr(service, "get_collection", MagicMock(return_value=collection))

    await service.get_history(
        "geoff",
        source_filter=["user"],
        exclude_deliveries=["silent", "suppressed"],
    )

    query = collection.find.call_args.args[0]
    assert query["metadata.delivery"] == {"$nin": ["silent", "suppressed"]}
