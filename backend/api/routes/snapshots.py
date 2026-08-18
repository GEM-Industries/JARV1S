"""Diagnostic snapshot API route."""

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps.device_auth import require_owner_id
from core.config import settings
from core.plugins.registry import registry
from core.tool_router import tool_router
from services.database.mongodb import mongodb
from services.diagnostics import diagnostics_service
from services.log_buffer import log_buffer
from services.perf import perf

router = APIRouter(prefix="/snapshots", tags=["snapshots"])

logger = logging.getLogger(__name__)

_SNAPSHOTS_DIR = settings.LOGS_DIR / "snapshots"


class SnapshotRequest(BaseModel):
    reason: str


class SnapshotResponse(BaseModel):
    snapshot_id: str
    path: str


def _safe_index_doc(index: dict[str, Any]) -> dict[str, Any]:
    return {
        key: index.get(key)
        for key in ("name", "key", "unique", "sparse", "partialFilterExpression")
        if key in index
    }


async def _capture_trigger_health(owner_id: str) -> dict[str, Any]:
    """Bounded, secret-safe trigger state for diagnostic snapshots."""
    db = mongodb.db

    status_counts_cursor = db.trigger_instances.aggregate([
        {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        {"$sort": {"_id": 1}},
    ])
    status_counts = {
        row["_id"] or "unknown": row["count"]
        for row in await status_counts_cursor.to_list(length=50)
    }

    indexes = []
    async for index in db.trigger_instances.list_indexes():
        indexes.append(_safe_index_doc(index))

    trigger_cursor = db.trigger_instances.find(
        {"owner_id": owner_id},
        {
            "_id": 0,
            "id": 1,
            "rule_id": 1,
            "status": 1,
            "due_at": 1,
            "created_at": 1,
            "claimed_at": 1,
            "completed_at": 1,
            "delivered_at": 1,
            "action_snapshot.kind": 1,
            "failure_reason": 1,
            "dedup_key": 1,
        },
    ).sort("created_at", -1).limit(10)
    recent_instances = await trigger_cursor.to_list(length=10)

    failure_cursor = db.automation_fired.find(
        {"status": "failed"},
        {
            "_id": 0,
            "rule_id": 1,
            "item_id": 1,
            "fired_at": 1,
            "failed_at": 1,
            "error": 1,
        },
    ).sort("failed_at", -1).limit(10)

    return {
        "status_counts": status_counts,
        "dedup_null_count": await db.trigger_instances.count_documents(
            {"dedup_key": {"$type": "null"}}
        ),
        "dedup_present_count": await db.trigger_instances.count_documents(
            {"dedup_key": {"$exists": True}}
        ),
        "indexes": indexes,
        "recent_instances": recent_instances,
        "recent_automation_failures": await failure_cursor.to_list(length=10),
    }


@router.post("/", response_model=SnapshotResponse)
async def capture_snapshot(
    body: SnapshotRequest,
    owner_id: str = Depends(require_owner_id),
) -> SnapshotResponse:
    """Capture a diagnostic snapshot of current system state to a JSON file."""
    from core.setup.llm_config import resolve_llm_config_sync

    now = datetime.now(timezone.utc)
    snapshot_id = now.strftime("%Y%m%d_%H%M%S")

    conversation_history = await mongodb.get_history(
        owner_id,
        limit=20,
        include_timestamps=True,
        include_metadata=True,
    )

    # Snapshot the live catalog once instead of repeating it inside every turn's metadata.
    core_tools: dict[str, list[str]] = {}
    for plugin_name, plugin in sorted(registry.plugins.items()):
        if not registry.is_enabled(plugin_name):
            continue
        tool_names = sorted(plugin.get_tools().keys())
        if tool_names:
            core_tools[plugin_name] = tool_names

    # Exclude mcp_servers to avoid leaking API keys into snapshot files
    task_col = mongodb.get_collection("background_tasks")
    cursor = task_col.find(
        {"owner_id": owner_id},
        {"_id": 0, "events": 0, "mcp_servers": 0},
    ).sort("created_at", -1).limit(5)
    background_tasks = await cursor.to_list(length=5)

    # Capture live session context (location, timezone, etc.) — key for diagnosing
    # tool failures caused by missing context (e.g. location not sent from browser)
    from api.websockets.connection import manager
    session = manager.get_session(owner_id)
    session_ctx = dict(session.context) if session else {}

    snapshot = {
        "snapshot_id": snapshot_id,
        "captured_at": now.isoformat(),
        "reason": body.reason,
        "system": {
            "version": settings.VERSION,
            "model": resolve_llm_config_sync().model,
            "background_agent_model": settings.BACKGROUND_AGENT_MODEL,
            "environment": settings.ENVIRONMENT.value,
            "log_level": settings.LOG_LEVEL.value,
        },
        "session_context": session_ctx,
        "diagnostics": diagnostics_service.snapshot,
        "turn_summary": perf.latest_turn_summary(),
        "active_plugins": list(registry.plugins.keys()),
        "tool_routing": {
            "routable_plugins": sorted(tool_router._utterance_vectors.keys()),
        },
        "core_tools": core_tools,
        "background_tasks": background_tasks,
        "trigger_health": await _capture_trigger_health(owner_id),
        "conversation_history": conversation_history,
        "recent_logs": log_buffer.snapshot(),
    }

    _SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _SNAPSHOTS_DIR / f"{snapshot_id}.json"
    out_path.write_text(json.dumps(snapshot, indent=2, default=str))

    logger.info("Diagnostic snapshot captured: %s (reason=%r)", snapshot_id, body.reason)
    return SnapshotResponse(snapshot_id=snapshot_id, path=str(out_path))
