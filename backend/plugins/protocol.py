"""
Protocol Plugin for JARV1S.
User-defined routines: named lists of natural language steps stored in MongoDB.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from pydantic import BaseModel

from core.decorators import tool
from core.id import generate_id
from core.plugins.types import JarvisPlugin, PluginMetadata
from core.context import get_connection_id, get_node_id, get_owner_id
from core.plugins.capabilities import CapabilityErrorDetail
from services.database.mongodb import mongodb
from services.events import event_bus, Event, EventType


def _fail(message: str, code: str = "tool_error") -> CapabilityErrorDetail:
    return CapabilityErrorDetail(code=code, message=message)


logger = logging.getLogger(__name__)

COLLECTION = "protocols"


class ProtocolView(BaseModel):
    id: str
    name: str
    description: str = ""
    steps: List[str]
    directive: str | None = None
    prefetch_safe: bool = False
    run_count: int = 0
    last_run_at: str | None = None


def _name_query(name: str, owner_id: str) -> dict:
    """Case-insensitive name + owner filter."""
    return {"name": {"$regex": f"^{re.escape(name.strip())}$", "$options": "i"}, "owner_id": owner_id}


def _protocol_query(target: str, owner_id: str) -> dict:
    value = target.strip()
    protocol_id = value.removeprefix("protocol:")
    return {
        "owner_id": owner_id,
        "$or": [
            {"id": protocol_id},
            {"name": {"$regex": f"^{re.escape(value)}$", "$options": "i"}},
        ],
    }


async def protocol_exists(name: str, owner_id: str) -> bool:
    """Return True if a named protocol exists for this owner."""
    if not name or not name.strip():
        return False
    doc = await mongodb.db[COLLECTION].find_one(
        _name_query(name, owner_id),
        projection={"_id": 1},
    )
    return doc is not None


async def delete_protocol(owner_id: str, target: str) -> dict | None:
    """Delete a protocol and cancel every linked trigger artifact."""
    doc = await mongodb.db[COLLECTION].find_one(_protocol_query(target, owner_id))
    if not doc:
        return None
    stored_name = doc["name"]
    name_filter = {"$regex": f"^{re.escape(stored_name)}$", "$options": "i"}
    now = datetime.now(timezone.utc)
    await mongodb.db.trigger_rules.update_many(
        {"owner_id": owner_id, "action.protocol_name": name_filter},
        {"$set": {"enabled": False, "updated_at": now}},
    )
    await mongodb.db.trigger_instances.update_many(
        {
            "owner_id": owner_id,
            "action_snapshot.protocol_name": name_filter,
            "status": {"$in": ["pending", "claimed", "executing", "awaiting_delivery"]},
        },
        {"$set": {
            "status": "cancelled",
            "completed_at": now,
            "updated_at": now,
            "failure_reason": "protocol_deleted",
        }},
    )
    result = await mongodb.db[COLLECTION].delete_one({"_id": doc["_id"]})
    return doc if result.deleted_count == 1 else None


class ProtocolPlugin(JarvisPlugin):
    metadata = PluginMetadata(
        name="protocol",
        version="1.0.0",
        description="Create and manage user-defined routines (protocols).",
        utterances=[
            "run protocol",
            "create protocol",
            "protocol steps",
            "saved protocol",
            "reusable routine",
            "what steps are in this routine",
            "schedule an existing protocol",
            "use my saved routine",
        ],
    )

    @tool
    async def create_protocol(
        self, name: str, description: str, steps: List[str],
        directive: str = None, prefetch_safe: bool = False,
    ) -> str | CapabilityErrorDetail:
        """
        Create a named protocol (routine). Convert intent into actionable steps.
        Each step must be a specific instruction executable with available tools.
        Do NOT read back every step unless asked.

        GOOD: "Get today's weather forecast and summarize temperature and conditions"
        BAD: "weather"

        directive: optional free-text policy applied each run — conditional behavior
        the flat step list cannot express (e.g. "skip the weather step on weekends",
        "if any step fails, stop and announce instead of continuing").

        prefetch_safe: True only for read-only briefings (morning briefing,
        commute report, news digest). When True the protocol is rendered
        before the alarm fires — anything with a side effect (sending,
        playing, writing, deleting) would execute early. Split mixed routines
        into two protocols sharing the trigger time and mark only the
        briefing safe.
        """
        owner_id = get_owner_id()
        name = name.strip()

        if not name:
            return _fail("Protocol name cannot be empty.")
        if not steps:
            return _fail("Protocol must have at least one step.")

        existing = await mongodb.db[COLLECTION].find_one(
            _name_query(name, owner_id)
        )
        if existing:
            return _fail(f"Protocol '{name}' already exists. Use update_protocol to modify it.")

        now = datetime.now(timezone.utc)
        await mongodb.db[COLLECTION].insert_one({
            "id": generate_id("protocol-"),
            "name": name,
            "description": description,
            "steps": steps,
            "directive": directive,
            "prefetch_safe": prefetch_safe,
            "owner_id": owner_id,
            "run_count": 0,
            "last_run_at": None,
            "created_at": now,
            "updated_at": now,
        })

        return f"Protocol '{name}' created with {len(steps)} steps."

    @tool
    async def get_protocol(self, name: str) -> ProtocolView | CapabilityErrorDetail:
        """Get a protocol by stable id or exact name, including all its steps."""
        doc = await mongodb.db[COLLECTION].find_one(
            _protocol_query(name, get_owner_id())
        )
        if not doc:
            return _fail(f"Protocol '{name}' not found.")

        return ProtocolView(
            id=doc["id"],
            name=doc["name"],
            description=doc["description"],
            steps=doc["steps"],
            directive=doc.get("directive"),
            prefetch_safe=doc.get("prefetch_safe", False),
            run_count=doc.get("run_count", 0),
            last_run_at=doc["last_run_at"].isoformat() if doc.get("last_run_at") else None,
        )

    @tool
    async def update_protocol(
        self, name: str, steps: List[str] = None, description: str = None,
        directive: str = None, prefetch_safe: bool = None,
    ) -> str | CapabilityErrorDetail:
        """Replace a protocol's steps, description, directive, and/or prefetch flag.

        directive: pass an empty string to clear; pass None (default) to leave unchanged.
        prefetch_safe: pass True/False to change; pass None (default) to leave unchanged.
        See `create_protocol` for prefetch_safe semantics.
        """
        owner_id = get_owner_id()
        update_fields: Dict[str, Any] = {"updated_at": datetime.now(timezone.utc)}

        if steps is not None:
            update_fields["steps"] = steps
        if description is not None:
            update_fields["description"] = description
        if directive is not None:
            update_fields["directive"] = directive or None
        if prefetch_safe is not None:
            update_fields["prefetch_safe"] = bool(prefetch_safe)

        if len(update_fields) == 1:
            return _fail("Provide steps, description, directive, or prefetch_safe to update.")

        result = await mongodb.db[COLLECTION].update_one(
            _protocol_query(name, owner_id),
            {"$set": update_fields},
        )

        if result.matched_count == 0:
            return _fail(f"Protocol '{name}' not found.")
        return f"Protocol '{name}' updated."

    @tool
    async def delete_protocol(self, name: str) -> str | CapabilityErrorDetail:
        """Delete a protocol permanently. Cancels all trigger rules and pending
        instances that reference this protocol by name."""
        owner_id = get_owner_id()
        doc = await delete_protocol(owner_id, name)
        if not doc:
            return _fail(f"Protocol '{name}' not found.")
        return f"Protocol '{doc['name']}' deleted."

    @tool
    async def run_protocol(self, name: str) -> str | CapabilityErrorDetail:
        """
        Execute a protocol. Triggers a system turn — do not execute the steps yourself.
        """
        name = name.strip()
        doc = await mongodb.db[COLLECTION].find_one(
            _protocol_query(name, get_owner_id())
        )
        if not doc:
            return _fail(f"Protocol '{name}' not found.")

        stored_name = doc["name"]
        owner_id = get_owner_id()

        # Track execution metadata
        await mongodb.db[COLLECTION].update_one(
            _protocol_query(str(doc["id"]), owner_id),
            {"$inc": {"run_count": 1}, "$set": {"last_run_at": datetime.now(timezone.utc)}},
        )

        data: dict[str, Any] = {"owner_id": owner_id, "protocol_name": stored_name}
        connection_id = get_connection_id()
        if connection_id != owner_id:
            data["connection_id"] = connection_id
            data["node_id"] = get_node_id()

        await event_bus.publish(Event(
            type=EventType.PROTOCOL_RUN,
            source="protocol_plugin",
            data=data,
        ))

        return f"Running protocol '{stored_name}'."

    @tool
    async def add_protocol_step(self, name: str, step: str, position: int = None) -> str | CapabilityErrorDetail:
        """
        Add a step to a protocol. Appends by default.

        Args:
            position: 1-indexed insert position. Omit to append.
        """
        owner_id = get_owner_id()
        doc = await mongodb.db[COLLECTION].find_one(_protocol_query(name, owner_id))
        if not doc:
            return _fail(f"Protocol '{name}' not found.")

        steps = doc["steps"]
        if position is not None:
            idx = max(0, min(position - 1, len(steps)))
            steps.insert(idx, step)
        else:
            steps.append(step)

        await mongodb.db[COLLECTION].update_one(
            {"_id": doc["_id"]},
            {"$set": {"steps": steps, "updated_at": datetime.now(timezone.utc)}},
        )
        return f"Step added. '{doc['name']}' now has {len(steps)} steps."

    @tool
    async def remove_protocol_step(self, name: str, step_number: int) -> str | CapabilityErrorDetail:
        """
        Remove a step by position number.

        Args:
            step_number: 1-indexed.
        """
        owner_id = get_owner_id()
        doc = await mongodb.db[COLLECTION].find_one(_protocol_query(name, owner_id))
        if not doc:
            return _fail(f"Protocol '{name}' not found.")

        steps = doc["steps"]
        if step_number < 1 or step_number > len(steps):
            return _fail(f"Step {step_number} out of range (1-{len(steps)}).")

        steps.pop(step_number - 1)
        if not steps:
            return _fail("Cannot remove the last step. Delete the protocol instead.")

        await mongodb.db[COLLECTION].update_one(
            {"_id": doc["_id"]},
            {"$set": {"steps": steps, "updated_at": datetime.now(timezone.utc)}},
        )
        return f"Removed step {step_number}. '{doc['name']}' now has {len(steps)} steps."
