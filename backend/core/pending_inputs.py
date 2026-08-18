"""Durable approval visibility with process-local execution callbacks."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.id import generate_id
from core.plugins.types import UIEnvelope, WidgetLayout, WidgetSize
from core.plugins.ui import push_ui
from core.plugins.widget_snapshots import register_widget_snapshot_provider
from services.database.mongodb import mongodb
from services.events import Event, EventType, event_bus

PendingDecision = Literal["approved", "denied", "expired", "cancelled"]
PendingCallback = Callable[[], Awaitable[Any]]

PENDING_INPUT_TIMEOUT_SECS = 120

_callbacks: dict[str, PendingCallback] = {}
_waiters: dict[str, asyncio.Future[PendingDecision]] = {}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _collection():
    return mongodb.get_collection("pending_inputs")


def _source_id(source: dict[str, Any]) -> str:
    return str(
        source.get("id")
        or source.get("task_id")
        or source.get("turn_id")
        or source.get("trigger_instance_id")
        or ""
    )


def widget_id_for_input(input_id: str) -> str:
    return f"pending-{input_id}"


def pending_input_summary(doc: dict[str, Any]) -> dict[str, Any]:
    """Small source-embeddable projection for task/trigger rows."""
    return {
        "input_id": doc["input_id"],
        "kind": doc.get("kind", "approval"),
        "status": doc.get("status", "pending"),
        "prompt": doc.get("prompt", ""),
        "detail": doc.get("detail", ""),
        "risk": doc.get("risk"),
        "source": doc.get("source", {}),
        "widget_id": doc.get("widget_id") or widget_id_for_input(doc["input_id"]),
        "created_at": _ms(_coerce_datetime(doc.get("created_at"))),
        "expires_at": _ms(_coerce_datetime(doc.get("expires_at"))) if doc.get("expires_at") else None,
    }


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return _now()


def pending_input_widget(doc: dict[str, Any], *, result: str | None = None) -> UIEnvelope:
    input_id = doc["input_id"]
    expires_at = doc.get("expires_at")
    expires_ms = _ms(_coerce_datetime(expires_at)) if expires_at else None
    return UIEnvelope(
        widget_id=doc.get("widget_id") or widget_id_for_input(input_id),
        component="PendingInputWidget",
        title="Approval Required",
        data={
            "input_id": input_id,
            "kind": doc.get("kind", "approval"),
            "status": doc.get("status", "pending"),
            "prompt": doc.get("prompt", ""),
            "detail": doc.get("detail", ""),
            "risk": doc.get("risk"),
            "source": doc.get("source", {}),
            "result": result if result is not None else (doc.get("response") or {}).get("result"),
            "options": doc.get("options") or [
                {"id": "approve", "label": "Approve"},
                {"id": "deny", "label": "Deny"},
            ],
        },
        layout=WidgetLayout(size=WidgetSize.WIDE, priority=100),
        expires_at=expires_ms,
    )


async def publish_pending_input(owner_id: str, doc: dict[str, Any], *, result: str | None = None) -> None:
    """Push a pending-input widget directly over the event bus."""
    await event_bus.publish(
        Event(
            type=EventType.UI_UPDATE,
            source="pending_inputs",
            data={
                "session_id": owner_id,
                "envelope": pending_input_widget(doc, result=result).model_dump(),
            },
        )
    )


async def pending_input_snapshot_widgets(owner_id: str) -> list[UIEnvelope]:
    """Rebuild live pending-input widgets for a reconnecting display."""
    now = _now()
    cursor = _collection().find(
        {
            "owner_id": owner_id,
            "kind": "approval",
            "status": "pending",
            "$or": [
                {"expires_at": None},
                {"expires_at": {"$gt": now}},
                {"expires_at": {"$exists": False}},
            ],
        },
        {"_id": 0},
    )
    docs = await cursor.to_list(length=50)
    return [pending_input_widget(doc) for doc in docs]


async def create_pending_input(
    *,
    owner_id: str,
    prompt: str,
    detail: str = "",
    source: dict[str, Any],
    risk: Literal["low", "medium", "high"] | None = None,
    timeout_s: int = PENDING_INPUT_TIMEOUT_SECS,
    callback: PendingCallback | None = None,
    create_waiter: bool = False,
    publish: Literal["push_ui", "event_bus", "none"] = "push_ui",
) -> dict[str, Any]:
    now = _now()
    expires_at = now + timedelta(seconds=timeout_s)
    normalized_source = dict(source)
    normalized_source.setdefault("id", _source_id(normalized_source))
    duplicate = await _collection().find_one(
        {
            "owner_id": owner_id,
            "kind": "approval",
            "status": "pending",
            "prompt": prompt,
            "detail": detail,
            "source": normalized_source,
        },
        {"_id": 0},
    )
    duplicate_expires_at = duplicate.get("expires_at") if duplicate else None
    duplicate_is_live = (
        duplicate is not None
        and (duplicate_expires_at is None or _coerce_datetime(duplicate_expires_at) >= now)
    )
    if duplicate_is_live:
        input_id = duplicate["input_id"]
        if callback is not None:
            _callbacks[input_id] = callback
        if create_waiter and input_id not in _waiters:
            _waiters[input_id] = asyncio.get_running_loop().create_future()
        if publish == "push_ui":
            push_ui(pending_input_widget(duplicate))
        elif publish == "event_bus":
            await publish_pending_input(owner_id, duplicate)
        return duplicate

    input_id = generate_id("inp-")
    doc: dict[str, Any] = {
        "input_id": input_id,
        "owner_id": owner_id,
        "kind": "approval",
        "status": "pending",
        "prompt": prompt,
        "detail": detail,
        "options": [
            {"id": "approve", "label": "Approve"},
            {"id": "deny", "label": "Deny"},
        ],
        "risk": risk,
        "source": normalized_source,
        "response": None,
        "widget_id": widget_id_for_input(input_id),
        "runtime_bound": True,
        "created_at": now,
        "resolved_at": None,
        "expires_at": expires_at,
    }
    await _collection().insert_one(doc)
    doc.pop("_id", None)

    if callback is not None:
        _callbacks[input_id] = callback
    if create_waiter:
        _waiters[input_id] = asyncio.get_running_loop().create_future()

    if publish == "push_ui":
        push_ui(pending_input_widget(doc))
    elif publish == "event_bus":
        await publish_pending_input(owner_id, doc)

    return doc


register_widget_snapshot_provider("pending_inputs", pending_input_snapshot_widgets)


async def get_pending_input(input_id: str, *, owner_id: str | None = None) -> dict[str, Any] | None:
    filt: dict[str, Any] = {"input_id": input_id}
    if owner_id:
        filt["owner_id"] = owner_id
    doc = await _collection().find_one(filt, {"_id": 0})
    return doc


async def get_latest_pending_input(owner_id: str) -> dict[str, Any] | None:
    return await _collection().find_one(
        {"owner_id": owner_id, "kind": "approval", "status": "pending"},
        {"_id": 0},
        sort=[("created_at", -1)],
    )


async def resolve_pending_input(
    *,
    owner_id: str,
    input_id: str | None = None,
    decision: Literal["approve", "deny"],
) -> Any:
    doc = (
        await get_pending_input(input_id, owner_id=owner_id)
        if input_id
        else await get_latest_pending_input(owner_id)
    )
    if not doc:
        return "No pending action to approve." if decision == "approve" else "No pending action to deny."

    if doc.get("status") != "pending":
        return f"Pending input is already {doc.get('status')}."

    expires_at = doc.get("expires_at")
    if expires_at and _coerce_datetime(expires_at) < _now():
        await _set_resolved(doc, "expired", result="The pending action expired.")
        _finish_waiter(doc["input_id"], "expired")
        await _publish_resolution_widget(doc, "expired", result="The pending action expired.")
        return "The pending action expired. Please try again."

    if decision == "deny":
        _callbacks.pop(doc["input_id"], None)
        await _set_resolved(doc, "denied", result="Denied.")
        _finish_waiter(doc["input_id"], "denied")
        await _publish_resolution_widget(doc, "denied", result="Denied.")
        return "Cancelled."

    callback = _callbacks.pop(doc["input_id"], None)
    waiter = _waiters.get(doc["input_id"])
    if callback is None and waiter is None:
        await _set_resolved(doc, "cancelled", result="Pending action is no longer available.")
        await _publish_resolution_widget(doc, "cancelled", result="Pending action is no longer available.")
        return "The pending action is no longer available. Please try again."

    await _set_resolved(doc, "approved", result="Approved.")
    _finish_waiter(doc["input_id"], "approved")

    if callback is None:
        await _publish_resolution_widget(doc, "approved", result="Approved.")
        return "Approved."

    try:
        result = await _execute_callback(callback)
    except BaseException as exc:
        message = str(exc) or type(exc).__name__
        await _collection().update_one(
            {"input_id": doc["input_id"]},
            {"$set": {"response.result": message, "response.execution_status": "failed"}},
        )
        await _publish_resolution_widget(
            {**doc, "response": {"result": message}},
            "approved",
            result=message,
        )
        raise

    text = result if isinstance(result, str) else getattr(result, "content", None) or getattr(result, "message", None) or str(result)
    await _collection().update_one(
        {"input_id": doc["input_id"]},
        {"$set": {"response.result": text, "response.execution_status": "approved"}},
    )
    await _publish_resolution_widget(
        {**doc, "response": {"result": text}},
        "approved",
        result=text,
    )
    return result


async def wait_for_pending_input(input_id: str, *, timeout_s: int = PENDING_INPUT_TIMEOUT_SECS) -> PendingDecision:
    future = _waiters.get(input_id)
    if future is None:
        return "cancelled"
    try:
        return await asyncio.wait_for(future, timeout=timeout_s)
    except asyncio.TimeoutError:
        doc = await get_pending_input(input_id)
        if doc and doc.get("status") == "pending":
            await _set_resolved(doc, "expired", result="The pending action expired.")
            await _publish_resolution_widget(doc, "expired", result="The pending action expired.")
        return "expired"
    finally:
        _waiters.pop(input_id, None)


async def cancel_orphaned_pending_inputs() -> int:
    """Cancel pending rows whose process-local callback/waiter cannot exist after startup."""
    now = _now()
    result = await _collection().update_many(
        {"status": "pending", "runtime_bound": True},
        {
            "$set": {
                "status": "cancelled",
                "resolved_at": now,
                "response": {"result": "Cancelled after backend restart.", "decision": "cancelled"},
            }
        },
    )
    await mongodb.get_collection("background_tasks").update_many(
        {"attention": {"$ne": "none"}, "pending_input.input_id": {"$exists": True}},
        {
            "$set": {
                "attention": "none",
                "pending_input": None,
                "live_status": None,
            }
        },
    )
    _callbacks.clear()
    for future in _waiters.values():
        if not future.done():
            future.set_result("cancelled")
    _waiters.clear()
    return int(getattr(result, "modified_count", 0) or 0)


async def _set_resolved(doc: dict[str, Any], status: PendingDecision, *, result: str) -> None:
    await _collection().update_one(
        {"input_id": doc["input_id"], "status": "pending"},
        {
            "$set": {
                "status": status,
                "resolved_at": _now(),
                "response": {"decision": status, "result": result},
            }
        },
    )


async def _publish_resolution_widget(doc: dict[str, Any], status: PendingDecision, *, result: str) -> None:
    resolved_doc = {**doc, "status": status}
    if (doc.get("source") or {}).get("type") == "background_task":
        await publish_pending_input(doc["owner_id"], resolved_doc, result=result)
    else:
        push_ui(pending_input_widget(resolved_doc, result=result))


def _finish_waiter(input_id: str, decision: PendingDecision) -> None:
    future = _waiters.get(input_id)
    if future is not None and not future.done():
        future.set_result(decision)


async def _execute_callback(callback: PendingCallback) -> Any:
    result = callback()
    return await result if inspect.isawaitable(result) else result
