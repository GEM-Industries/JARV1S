import asyncio
import logging
import time
from typing import Any, Dict, List

from core.id import generate_id
from core.plugins.types import UIEnvelope, WidgetLayout, WidgetSize
from core.context import get_owner_id
from services.events import event_bus, Event, EventType

logger = logging.getLogger(__name__)


def push_ui(envelope: Any):
    """Push a UI update to the user's interface via the event bus."""
    if isinstance(envelope, UIEnvelope):
        data = envelope.model_dump()
    elif isinstance(envelope, dict):
        data = envelope
    else:
        data = {"error": "Invalid UI payload", "raw": str(envelope)}
    _publish_ui_event("UI_UPDATE", data)


def content_envelope(
    title: str,
    sections: List[Dict[str, Any]],
    *,
    size: str = WidgetSize.WIDE,
    pinned: bool = False,
    widget_id: str | None = None,
) -> UIEnvelope:
    """Build a ContentWidget envelope with structured sections.

    Each section dict must have a ``type`` key. Supported types:
    - ``{"type": "markdown", "content": "..."}``
    - ``{"type": "table", "headers": [...], "rows": [[...]]}``
    - ``{"type": "list", "items": [...], "ordered": False}``
    - ``{"type": "code", "language": "python", "content": "..."}``
    - ``{"type": "kv", "pairs": {"Key": "Value"}}``
    - ``{"type": "metric", "items": [{"label", "value", "percent", "status", "sublabel"}]}``
    """
    return UIEnvelope(
        widget_id=widget_id or generate_id("content-"),
        component="ContentWidget",
        data={"title": title, "sections": sections},
        layout=WidgetLayout(size=size),
        title=title,
        pinned=pinned,
    )


def receipt_envelope(
    title: str,
    line: str,
    *,
    sublabel: str | None = None,
    ttl_ms: int | None = 45 * 1000,
    widget_id: str | None = None,
    pinned: bool = False,
    extra_data: Dict[str, Any] | None = None,
) -> UIEnvelope:
    """Build a compact receipt-style ContentWidget envelope for the review rail."""
    now_ms = int(time.time() * 1000)
    data: Dict[str, Any] = {
        "display": "receipt",
        "title": title,
        "line": line,
        "sublabel": sublabel,
        "sections": [
            {
                "type": "kv",
                "pairs": {
                    "Summary": line,
                    **({"Detail": sublabel} if sublabel else {}),
                },
            }
        ],
    }
    if extra_data:
        data.update(extra_data)
    return UIEnvelope(
        widget_id=widget_id or generate_id("receipt-"),
        component="ContentWidget",
        data=data,
        layout=WidgetLayout(size=WidgetSize.WIDE),
        title=title,
        expires_at=now_ms + ttl_ms if ttl_ms is not None else None,
        pinned=pinned,
    )


def progress_receipt_envelope(
    *,
    widget_id: str,
    title: str,
    line: str,
    sublabel: str | None = None,
    kind: str,
    ref_id: str,
    status: str,
    attention: str = "none",
    created_at_ms: int | None = None,
    ttl_ms: int | None = None,
    action: Dict[str, Any] | None = None,
) -> UIEnvelope:
    """Build a stable, upsertable progress receipt for long-running work on the review rail."""
    now_ms = int(time.time() * 1000)
    return receipt_envelope(
        title=title,
        line=line,
        sublabel=sublabel,
        widget_id=widget_id,
        ttl_ms=ttl_ms,
        extra_data={
            "receipt_kind": kind,
            "ref_id": ref_id,
            "status": status,
            "attention": attention,
            **({"action": action} if action else {}),
        },
    ).model_copy(
        update={
            "created_at": created_at_ms or now_ms,
            "layout": WidgetLayout(size=WidgetSize.WIDE, priority=60),
        }
    )


def push_receipt(
    title: str,
    line: str,
    *,
    sublabel: str | None = None,
    ttl_ms: int | None = 10 * 60 * 1000,
    widget_id: str | None = None,
    pinned: bool = False,
) -> None:
    """Push a compact receipt-style ContentWidget to the review rail."""
    push_ui(receipt_envelope(
        title=title,
        line=line,
        sublabel=sublabel,
        ttl_ms=ttl_ms,
        widget_id=widget_id,
        pinned=pinned,
    ))


def push_content(
    title: str,
    sections: List[Dict[str, Any]],
    *,
    size: str = WidgetSize.WIDE,
    pinned: bool = False,
    widget_id: str | None = None,
) -> None:
    """Push a ContentWidget with structured sections."""
    push_ui(content_envelope(
        title=title,
        sections=sections,
        size=size,
        pinned=pinned,
        widget_id=widget_id,
    ))


def delete_ui(widget_id: str):
    """Remove a widget from the user's interface via the event bus."""
    _publish_ui_event("UI_DELETE", {"widget_id": widget_id})


def _publish_ui_event(event_name: str, data: Dict[str, Any]):
    try:
        owner_id = get_owner_id()
        event_type = getattr(EventType, event_name)
        
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.create_task(event_bus.publish(
                    Event(
                        type=event_type,
                        source="plugin",
                        data={
                            "owner_id": owner_id,
                            "session_id": owner_id,
                            "envelope": data if event_name == "UI_UPDATE" else None,
                            "widget_id": data.get("widget_id") if event_name == "UI_DELETE" else None
                        }
                    )
                ))
        except RuntimeError:
            pass
            
    except Exception as e:
        logger.error(f"Failed to publish {event_name} event: {e}")
