from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from api.deps.device_auth import require_owner_id
from core.activity.headless import project_headless_row
from core.plugins.capabilities import capability_call_preview
from core.triggers.vocabulary import TRACE_SUPPRESSED
from core.turns.delivery import HIDDEN_DELIVERIES
from services.database.mongodb import mongodb

router = APIRouter(prefix="/history", tags=["history"])


class HistoryItem(BaseModel):
    role: str
    content: str
    timestamp: str
    type: str = "text"
    code: str | None = None
    code_result: str | None = None
    tool_call_id: str | None = None
    response_id: str | None = None
    turn_id: str | None = None
    node_id: str | None = None
    node_label: str | None = None


class HeadlessTurnItem(BaseModel):
    """Audit-feed entry for a turn that ran without user-facing output.

    One entry per persisted trace row — a single fire produces multiple items
    (system context, tool_call rows, tool_result rows, final agent text), all
    sharing the same `delivery` + origin metadata. Same shape as the main
    transcript; the read filter is the only thing that differs.

    Distinct from `BackgroundTask` (the `jarvis.agents.dispatch` subagent
    system surfaced via `BackgroundTaskWidget`) — those are user-initiated
    heavy work. These are system-initiated invisible fires: silent
    automations / alarms / protocols, or evaluate turns that
    decided NO_REPLY.
    """
    timestamp: str
    turn_id: str | None = None          # groups all rows from the same turn execution
    delivery: str               # "silent" | "suppressed"
    role: str                   # "system" | "assistant" | "user" (tool result)
    content: str
    turn_type: str | None = None        # "tool_call" | "tool_result" | "text_only" | ...
    tool_call_id: str | None = None
    code: str | None = None
    trigger_source: str | None = None   # "alarm" | "automation" | "system_pulse" | ...
    rule_id: str | None = None
    rule_name: str | None = None
    protocol_name: str | None = None
    directive: str | None = None
    model: str | None = None


def _presence_fields(meta: dict) -> dict[str, str | None]:
    return {
        "turn_id": meta.get("turn_id"),
        "node_id": meta.get("node_id"),
        "node_label": meta.get("node_label"),
    }


def _with_presence(item: dict, meta: dict) -> dict:
    item.update({k: v for k, v in _presence_fields(meta).items() if v is not None})
    return item


def _to_history_items(msg: dict, code_items_by_tool_call_id: dict[str, dict] | None = None) -> list[dict]:
    """Map a DB message to one or more HistoryItem dicts.

    Tool calls and outputs are persisted as structured capability metadata.
    This projects them back into the frontend transcript shape.

    Hidden delivery rows are dropped except user-origin suppressed turns, which
    are projected as the heard input plus a small "no reply" notice for reloads.
    """
    role = msg["role"]
    content = msg["content"]
    ts = msg["timestamp"]
    meta = msg.get("metadata") or {}
    turn_type = meta.get("turn_type")

    delivery = meta.get("delivery")
    if delivery in HIDDEN_DELIVERIES:
        if role == "user" and delivery == TRACE_SUPPRESSED and not turn_type:
            return [
                _with_presence({"role": "user", "content": content, "timestamp": ts, "type": "text"}, meta),
                {
                    "role": "system",
                    "content": "Jarvis didn't reply.",
                    "timestamp": ts,
                    "type": "notice",
                },
            ]
        return []

    # Raw execution-prompt rows are audit data, not user-facing conversation.
    # The assistant-delivered text (role="assistant") is kept as-is.
    if role == "system":
        return []

    if turn_type == "tool_call":
        items: list[dict] = []
        spoken = meta.get("spoken", "")
        if spoken:
            items.append(_with_presence({"role": role, "content": spoken, "timestamp": ts, "type": "text"}, meta))
        code_item = _with_presence({
            "role": role,
            "content": "",
            "timestamp": ts,
            "type": "code",
            "code": (
                capability_call_preview(meta["capability"], meta.get("arguments") or {})
                if meta.get("capability")
                else meta.get("code", "")
            ),
            "code_result": None,
            "tool_call_id": meta.get("tool_call_id"),
        }, meta)
        items.append(code_item)
        tool_call_id = meta.get("tool_call_id")
        if code_items_by_tool_call_id is not None and tool_call_id:
            code_items_by_tool_call_id[tool_call_id] = code_item
        return items

    if turn_type == "tool_result":
        tool_call_id = meta.get("tool_call_id")
        if code_items_by_tool_call_id is not None and tool_call_id:
            code_item = code_items_by_tool_call_id.get(tool_call_id)
            if code_item is not None:
                output = meta.get("output")
                if isinstance(output, str):
                    code_item["code_result"] = output
        return []

    if turn_type == "reasoning":
        return [_with_presence({
            "role": role,
            "content": content,
            "timestamp": ts,
            "type": "reasoning",
            "response_id": meta.get("response_id"),
        }, meta)]

    return [_with_presence({"role": role, "content": content, "timestamp": ts, "type": "text"}, meta)]


@router.get("/", response_model=list[HistoryItem])
async def get_history(
    limit: int = Query(default=50, le=200),
    node_id: str | None = Query(default=None, description="Filter to turns from this node"),
    owner_id: str = Depends(require_owner_id),
):
    """Return recent conversation turns (user + system notifications).

    System-source assistant rows (what JARV1S spoke for notifications /
    protocols) are included so missed alerts remain visible after reload.
    Raw role="system" execution-prompt rows are omitted — they are audit data
    and not user-facing conversation. Silent/headless suppressed traces stay in
    the audit feed only; user-origin suppressed turns are restored as the heard
    input plus a small no-reply notice.

    Tool call entries are expanded into text + code items using metadata
    tagged at write time — no content parsing required.
    """
    raw_limit = min(limit * 3, 600)
    raw = await mongodb.get_history(
        owner_id,
        limit=raw_limit,
        source_filter=["user", "system"],
        include_timestamps=True,
        skip_tool_results=False,
        include_metadata=True,
        node_id=node_id,
    )
    items: list[dict] = []
    code_items_by_tool_call_id: dict[str, dict] = {}
    for msg in raw:
        items.extend(_to_history_items(msg, code_items_by_tool_call_id))
    return items[-limit:]


@router.get("/headless", response_model=list[HeadlessTurnItem])
async def get_headless_turns(
    limit: int = Query(default=50, le=200),
    owner_id: str = Depends(require_owner_id),
):
    """Audit feed of silent / suppressed turns.

    Surfaces every row tagged `metadata.delivery in {silent, suppressed}`,
    regardless of what fired it (alarm, automation, protocol, system_pulse).
    Returns the agent's full trace (text + tool calls + final decision) per
    fire — same shape as the main transcript, just hidden from chat.

    Not to be confused with `GET /tasks/` — that endpoint covers
    user-initiated background *agents* (`jarvis.agents.dispatch`).
    """
    raw = await mongodb.get_history(
        owner_id,
        limit=limit,
        source_filter=["system"],
        include_timestamps=True,
        include_metadata=True,
        skip_tool_results=True,
        include_deliveries=list(HIDDEN_DELIVERIES),
    )
    return [project_headless_row(msg) for msg in raw]
