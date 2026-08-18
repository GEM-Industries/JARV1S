"""
Agents Plugin — SDK interaction layer.

All opencode/claude-agent-sdk calls live here.
Nothing else in JARV1S should import the SDK directly.
"""

import asyncio
import logging
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pymongo import ReturnDocument  # type: ignore[import]

from core.agent.sdk import (
    SDKClient,
    AgentOptions,
    ResultMessage,
    AssistantMessage,
    UserMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from core.config import settings
from core.credentials.store import credential_store
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
from services.database.mongodb import mongodb
from services.events import event_bus, Event, EventType

logger = logging.getLogger(__name__)

SDK_CLEANUP_TIMEOUT = 5.0  # seconds to wait for subprocess teardown
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

    if attention == "approval":
        title = "Needs approval"
        line = str((pending or {}).get("prompt") or live)[:96]
        sublabel = "Waiting on you"
        widget_id = (pending or {}).get("widget_id")
        action = (
            {"type": "activate_widget", "widget_id": widget_id, "task_id": task_id}
            if isinstance(widget_id, str) and widget_id
            else {"type": "open_background_task", "task_id": task_id}
        )
    elif status == "completed":
        title = "Task complete"
        line = str(task_doc.get("result") or live)[:96]
        sublabel = "Done"
        action = {"type": "open_background_task", "task_id": task_id}
    elif status == "failed":
        title = "Task failed"
        line = str(task_doc.get("result") or live)[:96]
        sublabel = "Needs review"
        action = {"type": "open_background_task", "task_id": task_id}
    else:
        title = "Working"
        line = str(live)[:96]
        sublabel = "Running"
        action = {"type": "open_background_task", "task_id": task_id}

    ttl_ms = TASK_RECEIPT_TERMINAL_TTL_MS if status in {"completed", "failed"} else None
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


def _extract_pid(client: SDKClient) -> int | None:
    """Best-effort extraction of the underlying subprocess PID from the SDK."""
    try:
        proc = getattr(client, "_transport", None)
        proc = getattr(proc, "_process", None)
        return proc.pid if proc is not None else None
    except Exception:
        return None


def _mutating_tool_paths(tool_name: str, inp: dict[str, Any]) -> list[str]:
    """Return file paths from SDK tools that can mutate files."""
    if tool_name not in ("Write", "Edit", "MultiEdit"):
        return []
    paths: list[str] = []
    if inp.get("file_path"):
        paths.append(str(inp["file_path"]))
    for edit in inp.get("edits") or []:
        if isinstance(edit, dict) and edit.get("file_path"):
            paths.append(str(edit["file_path"]))
    return list(dict.fromkeys(paths))


async def _graceful_kill_pid(pid: int) -> None:
    """Two-phase kill: SIGTERM → 1s grace period → SIGKILL → verify dead."""
    try:
        os.kill(pid, signal.SIGTERM)
        logger.debug("Sent SIGTERM to subprocess pid=%d", pid)
    except ProcessLookupError:
        return
    except Exception as exc:
        logger.debug("SIGTERM pid=%d failed: %s", pid, exc)
        return

    await asyncio.sleep(1)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return

    try:
        os.kill(pid, signal.SIGKILL)
        logger.warning("SIGKILL required for subprocess pid=%d (did not exit after SIGTERM)", pid)
    except ProcessLookupError:
        pass
    except Exception as exc:
        logger.debug("SIGKILL pid=%d failed: %s", pid, exc)


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
            "status": task_doc.get("status", "running"),
            "progress_summary": task_doc.get("progress_summary", ""),
            "live_status": task_doc.get("live_status"),
            "attention": task_doc.get("attention", "none"),
            "pending_input": task_doc.get("pending_input"),
            "source": task_doc.get("source", "voice"),
            "mode": task_doc.get("mode"),
            "created_at": task_doc.get("created_at", 0),
            "artifacts": task_doc.get("artifacts", []),
            "activity": task_doc.get("activity", []),
        },
        layout=WidgetLayout(size=WidgetSize.WIDE, priority=50),
        title="Background Task",
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
) -> None:
    """Create the voice follow-up for a completed background task."""
    voice_msg = summary or "Done."
    instance = await trigger_service.create_instance(
        owner_id=owner_id,
        origin=TriggerOrigin(kind="system"),
        action=TriggerAction(
            decision=DECISION_TELL,
            message=f"Finished. {voice_msg}{budget_warning}",
            content_type="task_result",
        ),
        attention=AttentionPolicy(level="normal", sound="chime"),
        delivery=DeliveryPlan(),
        freshness=FreshnessPolicy(),
        source_event={"task_id": task_id, "owner_id": owner_id},
        dedup_key=f"task-complete:{task_id}",
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
    completion_fields: dict[str, Any] = {
        "status": "completed",
        "result": result[:10_000],
        "session_id": session_id,
        "cost_usd": cost_usd if cost_usd and cost_usd > 0 else None,
        "attention": "none",
        "pending_input": None,
        "live_status": None,
        "completed_at": now,
        "expires_at": now + timedelta(days=30),
    }
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


async def _fail_task(task_id: str, owner_id: str, error: str) -> None:
    now = datetime.now(timezone.utc)
    col = mongodb.get_collection("background_tasks")
    doc = await col.find_one_and_update(
        {"task_id": task_id},
        {
            "$set": {
                "status": "failed",
                "result": error,
                "attention": "none",
                "pending_input": None,
                "live_status": None,
                "completed_at": now,
                "expires_at": now + timedelta(days=30),
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if doc:
        from core.activity_events import publish_activity_changed

        await publish_activity_changed(owner_id)
        await _push_widget(owner_id, task_id, doc)
        await _push_task_progress_receipt(owner_id, task_id, force=True)
        _last_task_receipt_push.pop(task_id, None)


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
) -> None:
    """Drain the agent SDK subprocess, translating events to MongoDB + WebSocket updates."""
    col = mongodb.get_collection("background_tasks")

    # AgentOptions.mcp_servers expects {name: {type, url, headers?, ...}}
    mcp_dict = {}
    for s in mcp_servers:
        entry: dict[str, Any] = {"type": s.get("type", "http"), "url": s["url"]}
        if s.get("headers"):
            entry["headers"] = s["headers"]
        mcp_dict[s["name"]] = entry

    # claude-agent-sdk takes a bare model name (no "provider/" prefix)
    model = settings.BACKGROUND_AGENT_MODEL
    if "/" in model:
        model = model.split("/", 1)[1]

    anthropic_key = credential_store.get_stored_secret("ANTHROPIC_API_KEY")
    if not anthropic_key:
        await _fail_task(task_id, owner_id, "Anthropic API key is not configured.")
        return

    options = AgentOptions(
        model=model,
        effort=settings.LLM_HEADLESS_REASONING_EFFORT,
        max_turns=max_turns,
        cwd=cwd,
        permission_mode="bypassPermissions",
        system_prompt=system_prompt,
        mcp_servers=mcp_dict,
        env={"ANTHROPIC_API_KEY": anthropic_key},
        resume=resume_session_id or None,
        setting_sources=["user"],
        max_budget_usd=max_budget_usd,
    )

    client = SDKClient(options=options)
    text_parts: list[str] = []
    last_text: str = ""
    result_session_id: Optional[str] = None
    result_cost: Optional[float] = None
    result_text: Optional[str] = None
    result_duration_ms: Optional[int] = None
    result_num_turns: Optional[int] = None
    result_usage: Optional[dict[str, Any]] = None
    child_pid: int | None = None
    artifact_candidates: dict[str, dict[str, Any] | None] = {}
    trace: list[dict[str, Any]] = []
    tool_use_to_span: dict[str, tuple[str, str]] = {}

    try:
        await client.connect()
        child_pid = _extract_pid(client)
        if child_pid:
            from plugins.agents import register_child_pid
            register_child_pid(child_pid)
            logger.debug("Agent task %s running as pid=%d", task_id, child_pid)

        await client.query(prompt)

        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                # Capture session_id from the first message so it's persisted
                # even if the task is cancelled or fails before ResultMessage.
                if not result_session_id and msg.session_id:
                    result_session_id = msg.session_id
                    await col.update_one(
                        {"task_id": task_id},
                        {"$set": {"session_id": result_session_id}},
                    )

                for block in msg.content:
                    ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                    if isinstance(block, TextBlock) and block.text:
                        text_parts.append(block.text)
                        last_text = block.text
                        payload: dict[str, Any] = {
                            "ts": ts,
                            "event_type": "text",
                            "text": block.text[:500],
                        }
                        trace = append_trace(
                            trace,
                            task_trace_item(
                                kind="text",
                                ts=ts,
                                text_preview=block.text,
                                status="completed",
                            ),
                        )
                        await col.update_one(
                            {"task_id": task_id},
                            {
                                "$push": {"events": {"$each": [payload], "$slice": -50}},
                                "$set": {
                                    "progress_summary": block.text[:100],
                                    "live_status": block.text[:100],
                                    "trace": trace,
                                },
                            },
                        )
                        await _push_task_event(owner_id, task_id, payload)

                    elif isinstance(block, ToolUseBlock):
                        inp = block.input or {}
                        span_id = new_span_id()
                        tool_use_id = str(block.id)
                        if block.name == "Bash" and inp.get("command"):
                            tool_detail = f"Bash: {str(inp['command'])[:80]}"
                        elif block.name in ("Read", "Write", "Edit", "MultiEdit") and inp.get("file_path"):
                            for path in _mutating_tool_paths(block.name, inp):
                                artifact_candidates.setdefault(path, file_snapshot(path, cwd=cwd))
                            tool_detail = f"{block.name}: {inp['file_path']}"
                        elif block.name == "MultiEdit" and inp.get("edits"):
                            for path in _mutating_tool_paths(block.name, inp):
                                artifact_candidates.setdefault(path, file_snapshot(path, cwd=cwd))
                            tool_detail = block.name
                        else:
                            tool_detail = block.name
                        payload = {
                            "ts": ts,
                            "event_type": "tool_start",
                            "tool": tool_detail,
                        }
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
                        tool_use_to_span[tool_use_id] = (span_id, tool_detail)
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

            elif isinstance(msg, UserMessage):
                ts = int(datetime.now(timezone.utc).timestamp() * 1000)
                for block in msg.content:
                    if not isinstance(block, ToolResultBlock):
                        continue
                    parent = tool_use_to_span.pop(block.tool_use_id, None)
                    if parent:
                        span_id, tool_detail = parent
                    else:
                        span_id, tool_detail = new_span_id(), "tool"
                    result_body = format_tool_result_content(block.content)
                    status = "failed" if block.is_error else "completed"
                    trace = append_trace(
                        trace,
                        task_trace_item(
                            kind="tool_result",
                            ts=ts,
                            span_id=new_span_id(),
                            parent_id=span_id,
                            tool=tool_detail,
                            result_preview=result_body,
                            status=status,
                        ),
                    )
                if msg.tool_use_result is not None:
                    result_body = preview_text(msg.tool_use_result)
                    trace = append_trace(
                        trace,
                        task_trace_item(
                            kind="tool_result",
                            ts=ts,
                            text_preview=result_body,
                            status="completed",
                        ),
                    )
                if trace:
                    await col.update_one({"task_id": task_id}, {"$set": {"trace": trace}})

            elif isinstance(msg, ResultMessage):
                result_session_id = msg.session_id or result_session_id
                result_cost = msg.total_cost_usd if msg.total_cost_usd > 0 else None
                if msg.result:
                    result_text = msg.result
                result_duration_ms = msg.duration_ms
                result_num_turns = msg.num_turns
                if msg.usage is not None:
                    result_usage = (
                        dict(msg.usage)
                        if isinstance(msg.usage, dict)
                        else {"raw": msg.usage}
                    )

        verified_artifacts = [
            verify_file_artifact(path, cwd=cwd, source="code", before=before)
            for path, before in artifact_candidates.items()
        ]
        tool_call_count = sum(1 for item in trace if item.get("kind") == "tool_call")
        full_result = (result_text or "".join(text_parts) or last_text).strip()
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
        if result_cost and max_budget_usd and result_cost > max_budget_usd:
            logger.warning(
                "Task %s exceeded budget: $%.4f spent vs $%.2f limit",
                task_id, result_cost, max_budget_usd,
            )
            budget_warning = f" (Note: task exceeded budget — spent ${result_cost:.4f} of ${max_budget_usd:.2f} limit)"

        await _complete_task(
            task_id, owner_id,
            result=full_result,
            summary=last_text or full_result[:500],
            session_id=result_session_id,
            cost_usd=result_cost,
            budget_warning=budget_warning,
            duration_ms=result_duration_ms,
            num_turns=result_num_turns,
            usage=result_usage,
        )

    except asyncio.CancelledError:
        await _fail_task(task_id, owner_id, "Task was cancelled.")
        raise
    except Exception as e:
        logger.error("Agent task %s failed: %s", task_id, e, exc_info=True)
        await _fail_task(task_id, owner_id, str(e))
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=SDK_CLEANUP_TIMEOUT)
        except Exception:
            if child_pid:
                await _graceful_kill_pid(child_pid)
        finally:
            if child_pid:
                from plugins.agents import unregister_child_pid
                unregister_child_pid(child_pid)
