"""Shared read-side projection helpers for trigger instance documents.

Both the activity timeline and the operations read model walk
``trigger_instances`` docs and derive the same ``automation`` vs ``trigger``
classification and source label from the frozen snapshots. Keep these pure
and free of I/O so either read path can call them without coupling to the
other service.
"""

from __future__ import annotations

from typing import Any, Literal

RunKind = Literal["trigger", "automation"]


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def trigger_run_kind(doc: dict[str, Any]) -> RunKind:
    """Classify a trigger instance doc as ``automation`` or ``trigger``.

    An automation is an external-origin fire with a backing rule id; everything
    else is a generic trigger. Matches the predicate used by the activity and
    operations read models.
    """
    origin = _as_dict(doc.get("origin_snapshot"))
    source_event = _as_dict(doc.get("source_event"))
    if origin.get("kind") == "external" and source_event.get("rule_id"):
        return "automation"
    return "trigger"


def trigger_run_source(doc: dict[str, Any]) -> str | None:
    """Derive a human-readable source label from a trigger instance doc.

    Shared fallback chain so the activity feed and operations panel cannot
    drift on source labeling.
    """
    source_event = _as_dict(doc.get("source_event"))
    action = _as_dict(doc.get("action_snapshot"))
    origin = _as_dict(doc.get("origin_snapshot"))
    source = (
        source_event.get("rule_name")
        or source_event.get("protocol_name")
        or source_event.get("trigger_source")
        or action.get("protocol_name")
        or origin.get("source")
        or origin.get("kind")
    )
    return str(source) if source else None
