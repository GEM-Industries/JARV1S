"""
Agents Plugin — task lifecycle and worker orchestration.

Vendor SDK connect/stream/dispose lives in `workers/`. This module owns Mongo
settlement, receipts, artifacts, and triggers.
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import ReturnDocument  # type: ignore[import]

from core.plugins.widget_snapshots import register_widget_snapshot_provider
from core.plugins.types import UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import progress_receipt_envelope
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerOrigin,
)
from core.triggers.service import trigger_service
from core.triggers.vocabulary import DECISION_TELL
from plugins.agents.task_review import (
    append_trace,
    file_snapshot,
    format_tool_result_content,
    merge_artifacts,
    new_span_id,
    preview_args,
    preview_text,
    task_trace_item,
    verify_file_artifact,
)
from plugins.agents.workers import (
    CodeWorkSpec,
    WorkerEvent,
    WorkerKind,
    WorkerRunError,
    WorkerStartupError,
    lineage_worker_kind,
    worker_for_kind,
)
from services.database.mongodb import mongodb
from services.events import event_bus, Event, EventType

logger = logging.getLogger(__name__)

TASK_RECEIPT_THROTTLE_SEC = 2.0
TASK_RECEIPT_TERMINAL_TTL_MS = 45 * 1000
_last_task_receipt_push: dict[str, float] = {}


def _task_created_at_ms(task_doc: dict) -> int:
    created = task_doc.get("created_at")
    if isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return int(created.timestamp() * 1000)
    if isinstance(created, (int, float)):
        return int(created)
    return int(time.time() * 1000)


def task_progress_receipt_envelope(task_id: str, task_doc: dict) -> UIEnvelope:
    """Compact review-rail receipt for an active or recently finished background task."""
    status = str(task_doc.get("status") or "running")
    attention = str(task_doc.get("attention") or "none")
    live = (
        task_doc.get("live_status")
        or task_doc.get("progress_summary")
        or "Starting…"
    )
    pending = task_doc.get("pending_input") if isinstance(task_doc.get("pending_input"), dict) else None

    named = str(task_doc.get("title") or "").strip()
    if attention == "approval":
        title = "Needs approval"
        line = str((pending or {}).get("prompt") or live)[:96]
        sublabel = named or "Waiting on you"
        widget_id = (pending or {}).get("widget_id")
        action = (
            {"type": "activate_widget", "widget_id": widget_id, "task_id": task_id}
            if isinstance(widget_id, str) and widget_id
            else {"type": "open_background_task", "task_id": task_id}
        )
    elif status == "completed":
        title = named or "Task complete"
        line = str(task_doc.get("result") or live)[:96]
        sublabel = "Done"
        action = {"type": "open_background_task", "task_id": task_id}
    elif status == "failed":
        title = named or "Task failed"
        line = str(task_doc.get("result") or live)[:96]
        sublabel = "Needs review"
        action = {"type": "open_background_task", "task_id": task_id}
    elif status == "cancelled":
        title = named or "Task cancelled"
        line = str(task_doc.get("result") or "Cancelled")[:96]
        sublabel = "Cancelled"
        action = {"type": "open_background_task", "task_id": task_id}
    else:
        title = named or "Working"
        line = str(live)[:96]
        sublabel = "Running"
        action = {"type": "open_background_task", "task_id": task_id}

    ttl_ms = TASK_RECEIPT_TERMINAL_TTL_MS if status in {"completed", "failed", "cancelled"} else None
    return progress_receipt_envelope(
        widget_id=f"task-receipt-{task_id}",
        title=title,
        line=line,
        sublabel=sublabel,
        kind="task_progress",
        ref_id=task_id,
        status=status,
        attention=attention,
        created_at_ms=_task_created_at_ms(task_doc),
        ttl_ms=ttl_ms,
        action=action,
    )


def _mutating_tool_paths(tool_name: str, inp: dict[str, Any]) -> list[str]:
    """Return file paths from SDK tools that can mutate files."""
    paths: list[str] = []
    if tool_name in ("Write", "Edit", "MultiEdit"):
        if inp.get("file_path"):
            paths.append(str(inp["file_path"]))
        for edit in inp.get("edits") or []:
            if isinstance(edit, dict) and edit.get("file_path"):
                paths.append(str(edit["file_path"]))
        return list(dict.fromkeys(paths))
    for key in ("file_path", "path", "target_file"):
        if inp.get(key):
            paths.append(str(inp[key]))
    return list(dict.fromkeys(paths))


async def _resolve_tools_for_dispatch(connected_apps: list[str]) -> list[dict]:
    """Resolve MCP server configs for the given Composio apps concurrently."""
    from core.integrations.composio_gateway import get_composio_gateway

    composio_gw = get_composio_gateway()
    if not composio_gw or not connected_apps:
        return []

    async def _get_url(app: str) -> Optional[dict]:
        try:
            url = await composio_gw.get_mcp_url(app)
            if url:
                return {
                    "type": "http",
                    "url": url,
                    "name": app,
                    "headers": {"x-api-key": composio_gw._api_key},
                }
        except Exception as e:
            logger.warning("Failed to get MCP URL for %s: %s", app, e)
        return None

    results = await asyncio.gather(*[_get_url(app) for app in connected_apps])
    return [r for r in results if r is not None]


async def _push_task_event(owner_id: str, task_id: str, payload: dict) -> None:
    """Publish a real-time task event to the frontend via the event bus."""
    await event_bus.publish(
        Event(
            type=EventType.TASK_EVENT,
            source="agents",
            data={
                "session_id": owner_id,
                "payload": {"task_id": task_id, **payload},
            },
        )
    )
    await _push_task_progress_receipt(owner_id, task_id)


async def _push_task_progress_receipt(
    owner_id: str,
    task_id: str,
    *,
    force: bool = False,
) -> None:
    """Upsert the review-rail receipt for a background task (throttled while running)."""
    now = time.monotonic()
    if not force and now - _last_task_receipt_push.get(task_id, 0) < TASK_RECEIPT_THROTTLE_SEC:
        return

    col = mongodb.get_collection("background_tasks")
    doc = await col.find_one({"task_id": task_id}, {"_id": 0})
    if not doc:
        return

    _last_task_receipt_push[task_id] = now
    envelope = task_progress_receipt_envelope(task_id, doc)
    await _push_ui_envelope(owner_id, envelope.model_dump())


async def _push_ui_envelope(owner_id: str, envelope: dict[str, Any]) -> None:
    """Forward a ContentWidget (or other) envelope to connected clients."""
    await event_bus.publish(
        Event(
            type=EventType.UI_UPDATE,
            source="agents",
            data={"session_id": owner_id, "envelope": envelope},
        )
    )


async def _push_ui_delete(owner_id: str, widget_id: str) -> None:
    await event_bus.publish(
        Event(
            type=EventType.UI_DELETE,
            source="agents",
            data={"session_id": owner_id, "widget_id": widget_id},
        )
    )


def task_widget_envelope(task_id: str, task_doc: dict) -> UIEnvelope:
    """Build the BackgroundTaskWidget envelope from the task document."""
    return UIEnvelope(
        widget_id=f"task-{task_id}",
        component="BackgroundTaskWidget",
        data={
            "task_id": task_id,
            "title": str(task_doc.get("title") or "").strip() or None,
            "status": task_doc.get("status", "running"),
            "progress_summary": task_doc.get("progress_summary", ""),
            "live_status": task_doc.get("live_status"),
            "attention": task_doc.get("attention", "none"),
            "pending_input": task_doc.get("pending_input"),
            "source": task_doc.get("source", "voice"),
            "mode": task_doc.get("mode"),
            "session_id": task_doc.get("session_id"),
            "worker_kind": task_doc.get("worker_kind"),
            "created_at": task_doc.get("created_at", 0),
            "cwd": task_doc.get("cwd"),
            "artifacts": task_doc.get("artifacts", []),
            "activity": task_doc.get("activity", []),
        },
        layout=WidgetLayout(size=WidgetSize.WIDE, priority=50),
        title=str(task_doc.get("title") or "").strip() or "Working",
    )


async def background_task_snapshot_widgets(owner_id: str) -> list[UIEnvelope]:
    """Rebuild active background task progress receipts for a reconnecting display."""
    col = mongodb.get_collection("background_tasks")
    cursor = col.find(
        {"owner_id": owner_id, "status": "running"},
        {"_id": 0},
    )
    docs = await cursor.to_list(length=50)
    return [
        task_progress_receipt_envelope(doc["task_id"], doc)
        for doc in docs
        if doc.get("task_id")
    ]


async def _push_widget(owner_id: str, task_id: str, task_doc: dict) -> None:
    """Push/refresh the BackgroundTaskWidget envelope via the event bus."""
    envelope = task_widget_envelope(task_id, task_doc)
    await event_bus.publish(
        Event(
            type=EventType.UI_UPDATE,
            source="agents",
            data={"session_id": owner_id, "envelope": envelope.model_dump()},
        )
    )


register_widget_snapshot_provider("background_tasks", background_task_snapshot_widgets)


async def _publish_completion_trigger(
    *,
    owner_id: str,
    task_id: str,
    summary: str,
    budget_warning: str = "",
    title: str = "",
    outcome: str = "completed",
) -> None:
    """Create the voice follow-up for a completed or failed background task."""
    named = str(title or "").strip() or "The task"
    voice_msg = (summary or "").strip()
    if outcome == "failed":
        message = f"{named} failed. {voice_msg or 'Needs review.'}".strip()
        dedup_key = f"task-failed:{task_id}"
    else:
        message = f"{named} is done. {voice_msg or 'Done.'}{budget_warning}".strip()
        dedup_key = f"task-complete:{task_id}"
    instance = await trigger_service.create_instance(
        owner_id=owner_id,
        origin=TriggerOrigin(kind="system"),
        action=TriggerAction(
            decision=DECISION_TELL,
            message=message,
            content_type="task_result",
        ),
        attention=AttentionPolicy(level="normal", sound="chime"),
        delivery=DeliveryPlan(),
        freshness=FreshnessPolicy(),
        source_event={"task_id": task_id, "owner_id": owner_id, "outcome": outcome},
        dedup_key=dedup_key,
        management=ManagementOwnership(provider="agents", resource_id=task_id),
    )
    await event_bus.publish(
        Event(
            type=EventType.TRIGGER_DUE,
            source="agents",
            data={"instance_id": instance.id, "owner_id": owner_id},
        )
    )


async def _publish_approval_needed_trigger(
    *,
    owner_id: str,
    task_id: str,
    input_id: str,
    prompt: str,
) -> None:
    """Create the voice follow-up for a background task waiting on approval."""
    instance = await trigger_service.create_instance(
        owner_id=owner_id,
        origin=TriggerOrigin(kind="system"),
        action=TriggerAction(
            decision=DECISION_TELL,
            message=f"A background task needs your approval. {prompt}",
            content_type="task_result",
        ),
        attention=AttentionPolicy(level="urgent", sound="chime"),
        delivery=DeliveryPlan(),
        freshness=FreshnessPolicy(),
        source_event={"task_id": task_id, "input_id": input_id, "owner_id": owner_id},
        dedup_key=f"task-approval:{input_id}",
        management=ManagementOwnership(provider="agents", resource_id=task_id),
    )
    await event_bus.publish(
        Event(
            type=EventType.TRIGGER_DUE,
            source="agents",
            data={"instance_id": instance.id, "owner_id": owner_id},
        )
    )


async def _complete_task(
    task_id: str,
    owner_id: str,
    result: str,
    summary: str,
    session_id: Optional[str],
    cost_usd: Optional[float],
    budget_warning: str = "",
    *,
    duration_ms: Optional[int] = None,
    num_turns: Optional[int] = None,
    usage: Optional[dict[str, Any]] = None,
) -> None:
    now = datetime.now(timezone.utc)
    col = mongodb.get_collection("background_tasks")
    current = await col.find_one({"task_id": task_id}, {"open": 1, "title": 1})
    completion_fields: dict[str, Any] = {
        "status": "completed",
        "result": result[:10_000],
        "session_id": session_id,
        "cost_usd": cost_usd if cost_usd and cost_usd > 0 else None,
        "attention": "none",
        "pending_input": None,
        "live_status": None,
        "completed_at": now,
    }
    if not (current and current.get("open") is True):
        completion_fields["expires_at"] = now + timedelta(days=30)
    if duration_ms is not None:
        completion_fields["duration_ms"] = duration_ms
    if num_turns is not None:
        completion_fields["num_turns"] = num_turns
    if usage is not None:
        completion_fields["usage"] = usage
    doc = await col.find_one_and_update(
        {"task_id": task_id},
        {
            "$set": completion_fields,
            "$unset": {"completion_notification_error": ""},
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        from core.activity_events import publish_activity_changed

        await publish_activity_changed(owner_id)
        await _push_widget(owner_id, task_id, doc)
        await _push_task_progress_receipt(owner_id, task_id, force=True)
        _last_task_receipt_push.pop(task_id, None)
    else:
        logger.warning("Task %s completion update missed; skipping completion trigger", task_id)
        return

    try:
        await _publish_completion_trigger(
            owner_id=owner_id,
            task_id=task_id,
            summary=summary,
            budget_warning=budget_warning,
            title=str((doc or current or {}).get("title") or ""),
            outcome="completed",
        )
    except Exception as exc:
        logger.warning(
            "Task %s completed but completion trigger enqueue failed: %s",
            task_id,
            exc,
        )
        await col.update_one(
            {"task_id": task_id},
            {"$set": {"completion_notification_error": str(exc)[:500]}},
        )


async def _settle_unsuccessful(
    task_id: str,
    owner_id: str,
    *,
    status: str,
    result: str,
    notify: bool,
) -> None:
    now = datetime.now(timezone.utc)
    col = mongodb.get_collection("background_tasks")
    current = await col.find_one({"task_id": task_id}, {"open": 1, "title": 1})
    fields: dict[str, Any] = {
        "status": status,
        "result": result,
        "attention": "none",
        "pending_input": None,
        "live_status": None,
        "completed_at": now,
    }
    if not (current and current.get("open") is True):
        fields["expires_at"] = now + timedelta(days=30)
    doc = await col.find_one_and_update(
        {"task_id": task_id},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )
    if not doc:
        return
    from core.activity_events import publish_activity_changed

    await publish_activity_changed(owner_id)
    await _push_widget(owner_id, task_id, doc)
    await _push_task_progress_receipt(owner_id, task_id, force=True)
    _last_task_receipt_push.pop(task_id, None)
    if not notify:
        return
    try:
        await _publish_completion_trigger(
            owner_id=owner_id,
            task_id=task_id,
            summary=result,
            title=str((doc or current or {}).get("title") or ""),
            outcome="failed",
        )
    except Exception as exc:
        logger.warning(
            "Task %s failed but failure trigger enqueue failed: %s",
            task_id,
            exc,
        )
        await col.update_one(
            {"task_id": task_id},
            {"$set": {"completion_notification_error": str(exc)[:500]}},
        )


async def _fail_task(task_id: str, owner_id: str, error: str) -> None:
    await _settle_unsuccessful(
        task_id, owner_id, status="failed", result=error, notify=True
    )


async def _cancel_task(task_id: str, owner_id: str, error: str = "Task was cancelled.") -> None:
    await _settle_unsuccessful(
        task_id, owner_id, status="cancelled", result=error, notify=False
    )


async def _run_agent(
    task_id: str,
    owner_id: str,
    prompt: str,
    cwd: str,
    max_turns: int,
    mcp_servers: list[dict],
    system_prompt: str,
    resume_session_id: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    worker_kind: Optional[WorkerKind] = None,
    title: str = "",
) -> None:
    """Run a code worker and persist progress onto the existing task row."""
    col = mongodb.get_collection("background_tasks")
    if worker_kind is None:
        existing = await col.find_one({"task_id": task_id}, {"worker_kind": 1, "title": 1})
        worker_kind = lineage_worker_kind(existing)
        if not title:
            title = str((existing or {}).get("title") or "")
    worker = worker_for_kind(worker_kind)

    artifact_candidates: dict[str, dict[str, Any] | None] = {}
    trace: list[dict[str, Any]] = []
    tool_use_to_span: dict[str, tuple[str, str]] = {}

    async def emit(event: WorkerEvent) -> None:
        nonlocal trace
        ts = int(datetime.now(timezone.utc).timestamp() * 1000)
        if event.kind == "external_handle":
            fields: dict[str, Any] = {}
            if event.session_id:
                fields["session_id"] = event.session_id
            if event.external_run_id:
                fields["external_run_id"] = event.external_run_id
            if fields:
                await col.update_one({"task_id": task_id}, {"$set": fields})
            return

        if event.kind == "text" and event.text:
            payload: dict[str, Any] = {
                "ts": ts,
                "event_type": "text",
                "text": event.text[:500],
            }
            trace = append_trace(
                trace,
                task_trace_item(
                    kind="text",
                    ts=ts,
                    text_preview=event.text,
                    status="completed",
                ),
            )
            await col.update_one(
                {"task_id": task_id},
                {
                    "$push": {"events": {"$each": [payload], "$slice": -50}},
                    "$set": {
                        "progress_summary": event.text[:100],
                        "live_status": event.text[:100],
                        "trace": trace,
                    },
                },
            )
            await _push_task_event(owner_id, task_id, payload)
            return

        if event.kind == "tool_start":
            inp = event.tool_input or {}
            tool_name = event.tool_name or event.tool or "tool"
            tool_detail = event.tool or tool_name
            for path in _mutating_tool_paths(tool_name, inp):
                artifact_candidates.setdefault(path, file_snapshot(path, cwd=cwd))
            payload = {
                "ts": ts,
                "event_type": "tool_start",
                "tool": tool_detail,
            }
            span_id = new_span_id()
            trace = append_trace(
                trace,
                task_trace_item(
                    kind="tool_call",
                    ts=ts,
                    span_id=span_id,
                    tool=tool_detail,
                    args_preview=preview_args(inp),
                    status="running",
                ),
            )
            if event.tool_use_id:
                tool_use_to_span[event.tool_use_id] = (span_id, tool_detail)
            await col.update_one(
                {"task_id": task_id},
                {
                    "$push": {"events": {"$each": [payload], "$slice": -50}},
                    "$set": {
                        "progress_summary": f"Running {tool_detail}…",
                        "live_status": f"Running {tool_detail}…",
                        "trace": trace,
                    },
                },
            )
            await _push_task_event(owner_id, task_id, payload)
            return

        if event.kind == "tool_result":
            parent = tool_use_to_span.pop(event.tool_use_id, None) if event.tool_use_id else None
            if parent:
                span_id, tool_detail = parent
            else:
                span_id, tool_detail = new_span_id(), event.tool or "tool"
            if event.tool_use_id:
                result_body = format_tool_result_content(event.result_content)
            else:
                result_body = preview_text(event.result_content)
            status = "failed" if event.is_error else "completed"
            item_kwargs: dict[str, Any] = {
                "kind": "tool_result",
                "ts": ts,
                "status": status,
            }
            if event.tool_use_id:
                item_kwargs.update(
                    span_id=new_span_id(),
                    parent_id=span_id,
                    tool=tool_detail,
                    result_preview=result_body,
                )
            else:
                item_kwargs["text_preview"] = result_body
            trace = append_trace(trace, task_trace_item(**item_kwargs))
            await col.update_one({"task_id": task_id}, {"$set": {"trace": trace}})

    spec = CodeWorkSpec(
        prompt=prompt,
        cwd=cwd,
        max_turns=max_turns,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
        resume_session_id=resume_session_id,
        max_budget_usd=max_budget_usd,
        title=title,
    )

    try:
        result = await worker.execute(spec, emit)
        verified_artifacts = [
            verify_file_artifact(path, cwd=cwd, source="code", before=before)
            for path, before in artifact_candidates.items()
        ]
        tool_call_count = sum(1 for item in trace if item.get("kind") == "tool_call")
        full_result = (result.text or result.summary or "").strip()
        if not full_result:
            changed_count = sum(1 for a in verified_artifacts if a.get("changed"))
            if tool_call_count or changed_count:
                full_result = f"Ran {tool_call_count} tool(s), changed {changed_count} file(s)."
            else:
                full_result = "Task completed."
                logger.warning(
                    "Agent task %s completed with empty result — check model/provider config",
                    task_id,
                )

        artifacts = merge_artifacts(
            [],
            [artifact for artifact in verified_artifacts if artifact.get("changed")],
        )
        if artifacts:
            for artifact in artifacts:
                trace = append_trace(
                    trace,
                    task_trace_item(
                        kind="artifact",
                        ts=int(datetime.now(timezone.utc).timestamp() * 1000),
                        text_preview=artifact["path"],
                        status="completed" if artifact.get("exists_verified") else "failed",
                    ),
                )
            await col.update_one(
                {"task_id": task_id},
                {"$set": {"artifacts": artifacts, "trace": trace}},
            )

        budget_warning = ""
        if result.cost_usd and max_budget_usd and result.cost_usd > max_budget_usd:
            logger.warning(
                "Task %s exceeded budget: $%.4f spent vs $%.2f limit",
                task_id, result.cost_usd, max_budget_usd,
            )
            budget_warning = (
                f" (Note: task exceeded budget — spent ${result.cost_usd:.4f} "
                f"of ${max_budget_usd:.2f} limit)"
            )

        await _complete_task(
            task_id, owner_id,
            result=full_result,
            summary=result.summary or full_result[:500],
            session_id=result.session_id,
            cost_usd=result.cost_usd,
            budget_warning=budget_warning,
            duration_ms=result.duration_ms,
            num_turns=result.num_turns,
            usage=result.usage,
        )

    except asyncio.CancelledError:
        await _cancel_task(task_id, owner_id)
        raise
    except WorkerStartupError as exc:
        logger.error("Agent task %s failed to start: %s", task_id, exc)
        await _fail_task(task_id, owner_id, str(exc))
    except WorkerRunError as exc:
        logger.error("Agent task %s failed: %s", task_id, exc)
        await _fail_task(task_id, owner_id, str(exc))
    except Exception as e:
        logger.error("Agent task %s failed: %s", task_id, e, exc_info=True)
        await _fail_task(task_id, owner_id, str(e))
