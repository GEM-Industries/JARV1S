"""Background task API routes."""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from api.deps.device_auth import require_owner_id
from services.database.mongodb import mongodb

router = APIRouter(prefix="/tasks", tags=["tasks"])

VALID_STATUSES = {"running", "completed", "failed", "cancelled"}


class TaskEvent(BaseModel):
    ts: int
    event_type: str
    text: str | None = None
    tool: str | None = None


class TaskArtifact(BaseModel):
    path: str
    source: str
    exists_verified: bool
    exists: bool | None = None
    size_bytes: int | None = None
    changed: bool | None = None


class TaskActivity(BaseModel):
    source: str
    status: str
    summary: str


class TaskTraceItem(BaseModel):
    kind: str
    ts: int
    span_id: str | None = None
    parent_id: str | None = None
    tool: str | None = None
    code: str | None = None
    args_preview: dict[str, object] | None = None
    text_preview: str | None = None
    result_preview: str | None = None
    status: str | None = None


class TaskSummary(BaseModel):
    task_id: str
    status: str
    mode: str | None = None
    source: str
    prompt: str
    progress_summary: str
    live_status: str | None
    attention: str
    pending_input: dict[str, object] | None
    cost_usd: float | None
    created_at: datetime
    completed_at: datetime | None
    title: str | None = None
    work_id: str | None = None
    worker_kind: str | None = None


class TaskDetail(TaskSummary):
    cwd: str
    max_turns: int
    max_budget_usd: float
    result: str | None
    session_id: str | None
    external_run_id: str | None = None
    duration_ms: int | None = None
    num_turns: int | None = None
    usage: dict[str, object] | None = None
    events: list[TaskEvent]
    artifacts: list[TaskArtifact]
    activity: list[TaskActivity]
    trace: list[TaskTraceItem]


@router.get("/", response_model=list[TaskSummary])
async def list_tasks(
    status: str | None = Query(default=None, description="Filter: running, completed, failed, or cancelled"),
    owner_id: str = Depends(require_owner_id),
):
    """List background agent tasks for the authenticated owner, ordered newest first."""
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid status '{status}'. Must be one of: {', '.join(sorted(VALID_STATUSES))}")

    filt: dict[str, Any] = {"owner_id": owner_id}
    if status:
        filt["status"] = status

    col = mongodb.get_collection("background_tasks")
    cursor = col.find(filt, {"events": 0, "_id": 0}).sort("created_at", -1).limit(50)
    return await cursor.to_list(length=50)


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task(task_id: str, owner_id: str = Depends(require_owner_id)):
    """Return a single task document including the full events array."""
    col = mongodb.get_collection("background_tasks")
    doc = await col.find_one(
        {"task_id": task_id, "owner_id": owner_id},
        {"_id": 0},
    )
    if not doc:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found.")
    return doc
