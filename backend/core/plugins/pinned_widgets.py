"""Durable pinned widget storage.

Pinning promotes a widget from ephemeral UI state into reconnectable state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.plugins.types import UIEnvelope
from core.plugins.widget_snapshots import register_widget_snapshot_provider
from services.database.mongodb import mongodb


def _collection() -> Any:
    return mongodb.get_collection("pinned_widgets")


async def pin_widget(owner_id: str, envelope: UIEnvelope) -> UIEnvelope:
    """Persist a pinned widget envelope for reconnect snapshots."""
    pinned = envelope.model_copy(update={"pinned": True, "expires_at": None})
    now = datetime.now(timezone.utc)
    await _collection().update_one(
        {"owner_id": owner_id, "widget_id": pinned.widget_id},
        {
            "$set": {
                "owner_id": owner_id,
                "widget_id": pinned.widget_id,
                "envelope": pinned.model_dump(mode="json"),
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return pinned


async def unpin_widget(owner_id: str, widget_id: str) -> None:
    """Remove a widget from durable pinned storage."""
    await _collection().delete_one({"owner_id": owner_id, "widget_id": widget_id})


async def pinned_widget_snapshot_widgets(owner_id: str) -> list[UIEnvelope]:
    """Return pinned widgets saved for this owner."""
    cursor = _collection().find({"owner_id": owner_id}, {"_id": 0, "envelope": 1})
    docs = await cursor.to_list(length=50)
    widgets: list[UIEnvelope] = []
    for doc in docs:
        envelope = doc.get("envelope")
        if envelope:
            widgets.append(UIEnvelope.model_validate(envelope))
    return widgets


register_widget_snapshot_provider("pinned_widgets", pinned_widget_snapshot_widgets)
