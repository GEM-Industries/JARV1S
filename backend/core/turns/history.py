"""Conversation history projection for prompt assembly.

Loads node-scoped short-term history and projects delivered proactive
assistant rows into safe conversational context. Raw system execution
prompts and hidden deliveries stay out of the LLM request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from core.config import settings
from core.plugins.registry import encode_provider_name
from core.prompts.system_turn_context import render_reply_context
from core.triggers.vocabulary import VISIBLE_DELIVERY_TAGS
from core.turns.delivery import HIDDEN_DELIVERIES, is_no_reply
from services.database.mongodb import mongodb

HistoryPolicy = Literal["interactive_user", "proactive_bounded", "headless_minimal"]

_SYSTEM_TAIL_DELIVERIES: frozenset[str] = frozenset(VISIBLE_DELIVERY_TAGS)


@dataclass(frozen=True)
class LoadedTurnHistory:
    messages: list[dict[str, Any]]
    reply_tools: frozenset[str] = frozenset()


def history_timestamp(row: dict) -> datetime:
    ts = row.get("timestamp")
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, str):
        try:
            parsed = datetime.fromisoformat(ts)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.min.replace(tzinfo=timezone.utc)


def strip_context_metadata(rows: list[dict]) -> list[dict]:
    return reconstruct_adapter_history(rows)


def reconstruct_adapter_history(rows: list[dict]) -> list[dict]:
    """Rebuild assistant tool-call / tool-result messages from structured traces.

    Incomplete pairs are dropped so follow-up turns keep valid adapter semantics.
    """
    pending: dict[str, dict[str, Any]] | None = None
    messages: list[dict] = []

    def flush_pending(*, complete: bool) -> None:
        nonlocal pending
        if pending is None:
            return
        if complete and pending.get("results"):
            messages.append(pending["assistant"])
            messages.extend(pending["results"])
        elif pending.get("spoken"):
            messages.append({"role": "assistant", "content": pending["spoken"]})
        pending = None

    for row in rows:
        meta = row.get("metadata") or {}
        turn_type = meta.get("turn_type")
        if turn_type == "tool_call":
            flush_pending(complete=False)
            call_id = meta.get("tool_call_id") or ""
            capability = meta.get("capability") or ""
            arguments = meta.get("arguments") if isinstance(meta.get("arguments"), dict) else {}
            spoken = (meta.get("spoken") or "").strip()
            provider_name = meta.get("provider_name") or (
                encode_provider_name(capability) if capability else "unknown"
            )
            pending = {
                "spoken": spoken,
                "assistant": {
                    "role": "assistant",
                    "content": spoken or None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": provider_name,
                                "arguments": json.dumps(arguments, default=str),
                            },
                        }
                    ],
                },
                "results": [],
                "call_id": call_id,
            }
            continue
        if turn_type == "tool_result":
            call_id = meta.get("tool_call_id")
            output = meta.get("output")
            if output is None:
                output = row.get("content") or ""
            if pending is not None and pending.get("call_id") == call_id:
                pending["results"].append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(output),
                })
                flush_pending(complete=True)
            continue
        flush_pending(complete=False)
        role = row.get("role", "user")
        content = row.get("content", "")
        if role in {"assistant", "user", "system"}:
            messages.append({"role": role, "content": content})
    flush_pending(complete=False)
    return messages


def project_system_tail(
    raw_rows: list[dict],
    *,
    grounding_by_instance: dict[str, dict[str, Any]] | None = None,
) -> list[dict]:
    """Project delivered system-trigger output into safe user-turn context.

    Raw system rows contain one-shot instructions and stay out of user-turn
    context. Delivered assistant text is safe to reuse as conversational history.
    """
    projected: list[dict] = []
    for row in raw_rows:
        role = row.get("role")
        meta = row.get("metadata") or {}
        delivery = meta.get("delivery")
        turn_type = meta.get("turn_type")

        if role == "system":
            continue
        if turn_type in ("tool_call", "tool_result"):
            continue
        if role != "assistant" or delivery not in _SYSTEM_TAIL_DELIVERIES:
            continue

        content = (row.get("content") or "").strip()
        if not content:
            continue
        instance_id = str(meta.get("instance_id") or "")
        grounding = (grounding_by_instance or {}).get(instance_id)
        if rendered_grounding := render_reply_context(grounding):
            content = f"{content}\n\n{rendered_grounding}"

        projected.append({
            "role": "assistant",
            "content": content,
            "timestamp": row.get("timestamp"),
        })
    return projected


def latest_reply_context_system_row(
    user_history: list[dict],
    raw_system_tail: list[dict],
) -> dict | None:
    """Return the newest delivered prompt through one unrelated user interjection."""
    candidates = [
        row
        for row in raw_system_tail
        if row.get("role") == "assistant"
        and (row.get("metadata") or {}).get("delivery") in _SYSTEM_TAIL_DELIVERIES
        and (row.get("metadata") or {}).get("turn_type") not in ("tool_call", "tool_result")
        and (row.get("content") or "").strip()
    ]
    if not candidates:
        return None
    candidate = max(candidates, key=history_timestamp)
    candidate_at = history_timestamp(candidate)
    later_user_turns = sum(
        1
        for row in user_history
        if row.get("role") == "user" and history_timestamp(row) > candidate_at
    )
    if later_user_turns > 1:
        return None
    return candidate


def merge_user_history_with_system_tail(
    user_history: list[dict],
    system_tail: list[dict],
) -> list[dict]:
    merged = sorted([*user_history, *system_tail], key=history_timestamp)
    return strip_context_metadata(merged)


async def resolve_prompt_window_start(
    owner_id: str,
    node_id: str | None,
    *,
    exclude_turn_id: str | None = None,
) -> datetime | None:
    """Resolve the node-local prompt window used by turns and the live transcript."""
    return await mongodb.resolve_conversation_window_start(
        owner_id,
        str(node_id) if node_id else None,
        gap=timedelta(minutes=settings.CONVERSATION_SESSION_INACTIVITY_MINUTES),
        exclude_turn_id=exclude_turn_id,
        visible_deliveries=list(VISIBLE_DELIVERY_TAGS),
    )


async def load_turn_history(
    *,
    owner_id: str,
    session_context: dict[str, Any],
    current_turn_id: str | None,
    policy: HistoryPolicy,
) -> LoadedTurnHistory:
    """Load the transcript projection appropriate for this turn purpose."""
    if policy == "headless_minimal":
        return LoadedTurnHistory(messages=[])

    hidden = list(HIDDEN_DELIVERIES)
    node_id = session_context.get("node_id")
    history_since = await resolve_prompt_window_start(
        owner_id,
        str(node_id) if node_id else None,
        exclude_turn_id=current_turn_id,
    )
    # Suppressed user turns stay in context: input that completed silently
    # (e.g. "cancel the alarm, I'm awake" -> tool call -> NO_REPLY) is still
    # state later turns need. Only the NO_REPLY sentinel rows are dropped so
    # past silence does not bias the next response.
    user_history = [
        row
        for row in await mongodb.get_history(
            owner_id,
            limit=200 if policy == "interactive_user" else 100,
            source_filter=["user"],
            include_timestamps=True,
            include_metadata=True,
            exclude_turn_id=current_turn_id,
            exclude_turn_types=["reasoning"],
            node_id=node_id,
            since=history_since,
        )
        if not (row.get("role") == "assistant" and is_no_reply(str(row.get("content") or "")))
    ]
    raw_system_tail = await mongodb.get_history(
        owner_id,
        limit=5,
        source_filter=["system"],
        exclude_deliveries=hidden,
        include_timestamps=True,
        include_metadata=True,
        exclude_turn_id=current_turn_id,
        exclude_turn_types=["reasoning"],
        node_id=node_id,
        since=history_since,
    )
    grounding_by_instance: dict[str, dict[str, Any]] = {}
    reply_tools: frozenset[str] = frozenset()
    if policy == "interactive_user":
        candidate = latest_reply_context_system_row(user_history, raw_system_tail)
        if candidate:
            metadata = candidate.get("metadata") or {}
            if instance_id := str(metadata.get("instance_id") or ""):
                from core.triggers.service import trigger_service

                grounding_by_instance = await trigger_service.get_delivered_reply_grounding(
                    owner_id=owner_id,
                    instance_ids=[instance_id],
                )
                if instance_id in grounding_by_instance:
                    reply_tools = frozenset(
                        tool
                        for tool in metadata.get("routed_tools") or []
                        if isinstance(tool, str) and "." in tool
                    )

    return LoadedTurnHistory(
        messages=merge_user_history_with_system_tail(
            user_history,
            project_system_tail(
                raw_system_tail,
                grounding_by_instance=grounding_by_instance,
            ),
        ),
        reply_tools=reply_tools,
    )
