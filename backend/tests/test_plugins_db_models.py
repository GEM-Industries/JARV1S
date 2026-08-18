"""Unit tests for `plugins.db.load_models` / `save_models` typed helpers.

Covers: round-trip, custom `key=`, empty-state, and tolerance of legacy
dict payloads already stored via `store_tool_data`.

Run from backend/: `pytest tests/test_plugins_db_models.py`
"""

import pytest
from pydantic import BaseModel

from plugins import db


class _Fixture(BaseModel):
    id: str
    value: int = 0


@pytest.mark.asyncio
async def test_load_empty_returns_empty_list(fake_tool_data_store):
    assert await db.load_models("fx", _Fixture) == []


@pytest.mark.asyncio
async def test_save_then_load_roundtrip(fake_tool_data_store):
    items = [_Fixture(id="a", value=1), _Fixture(id="b", value=2)]
    await db.save_models("fx", items)

    loaded = await db.load_models("fx", _Fixture)
    assert loaded == items


@pytest.mark.asyncio
async def test_custom_key(fake_tool_data_store):
    items = [_Fixture(id="x")]
    await db.save_models("profile", items, key="facts")

    assert "facts" in fake_tool_data_store.data["profile"]
    assert "items" not in fake_tool_data_store.data["profile"]

    assert await db.load_models("profile", _Fixture, key="facts") == items
    assert await db.load_models("profile", _Fixture) == []  # wrong key → empty


@pytest.mark.asyncio
async def test_overwrites_prior_list(fake_tool_data_store):
    await db.save_models("fx", [_Fixture(id="a")])
    await db.save_models("fx", [_Fixture(id="b"), _Fixture(id="c")])

    loaded = await db.load_models("fx", _Fixture)
    assert [f.id for f in loaded] == ["b", "c"]


@pytest.mark.asyncio
async def test_load_tolerates_legacy_dict_payload(fake_tool_data_store):
    fake_tool_data_store.data["fx"] = {"items": [{"id": "legacy", "value": 7}]}

    loaded = await db.load_models("fx", _Fixture)
    assert loaded == [_Fixture(id="legacy", value=7)]
