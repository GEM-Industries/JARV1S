from unittest.mock import MagicMock

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
