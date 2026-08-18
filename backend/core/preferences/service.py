"""Durable owner preference helpers."""

from __future__ import annotations

from datetime import datetime, timezone

from pymongo import ReturnDocument

from services.database.mongodb import mongodb

from .models import AudioPreferences, UserPreferences, UserPreferencesPatch

PREFERENCES_COLLECTION = "user_preferences"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _collection():
    return mongodb.get_collection(PREFERENCES_COLLECTION)


def _to_preferences(owner_id: str, doc: dict | None) -> UserPreferences:
    audio = (doc or {}).get("audio") if doc else None
    return UserPreferences(
        owner_id=owner_id,
        audio=AudioPreferences.model_validate(audio or {}),
    )


async def get_user_preferences(owner_id: str) -> UserPreferences:
    doc = await _collection().find_one({"owner_id": owner_id})
    return _to_preferences(owner_id, doc)


async def patch_user_preferences(owner_id: str, patch: UserPreferencesPatch) -> UserPreferences:
    update: dict[str, object] = {"updated_at": _now()}
    if patch.audio is not None and patch.audio.tool_cues_enabled is not None:
        update["audio.tool_cues_enabled"] = patch.audio.tool_cues_enabled

    if len(update) == 1:
        return await get_user_preferences(owner_id)

    doc = await _collection().find_one_and_update(
        {"owner_id": owner_id},
        {
            "$set": update,
            "$setOnInsert": {
                "owner_id": owner_id,
                "created_at": _now(),
            },
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return _to_preferences(owner_id, doc)
