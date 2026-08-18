"""Operations read-side joins over lifecycle, trace, and perf stores."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from core.activity.models import ActivityItem, ActivityOutcome
from core.operations.models import (
    OperationPerfStage,
    OperationPerfSummary,
    OperationProtocolRun,
    OperationRunDetail,
    OperationRunKind,
    OperationTraceLine,
    OperationTurnAttempt,
)
from core.plugins.capabilities import capability_call_preview
from core.time import local_datetime_fields
from core.triggers.projection import trigger_run_kind, trigger_run_source
from services.database.mongodb import mongodb

TRACE_CONTENT_CHARS = 2000
USER_TURN_SUMMARY_CHARS = 180


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _run_kind(doc: dict[str, Any]) -> OperationRunKind:
    return trigger_run_kind(doc)


def _run_source(doc: dict[str, Any]) -> str | None:
    return trigger_run_source(doc)


def _trace_line(doc: dict[str, Any]) -> OperationTraceLine:
    meta = _as_dict(doc.get("metadata"))
    content = doc.get("content", "")
    invocations = meta.get("invocations") if isinstance(meta.get("invocations"), list) else []
    focus_tools = meta.get("focus_tools") if isinstance(meta.get("focus_tools"), list) else []
    capability = meta.get("capability")
    arguments = meta.get("arguments") if isinstance(meta.get("arguments"), dict) else None
    preview = (
        capability_call_preview(str(capability), arguments or {})
        if capability
        else meta.get("code")
    )
    return OperationTraceLine(
        timestamp=doc["timestamp"],
        role=str(doc.get("role", "")),
        content=str(content)[:TRACE_CONTENT_CHARS],
        turn_type=meta.get("turn_type"),
        tool_call_id=meta.get("tool_call_id"),
        code=preview,
        output=meta.get("output") if meta.get("turn_type") == "tool_result" else None,
        response_id=meta.get("response_id"),
        model=meta.get("model"),
        reasoning_effort=meta.get("reasoning_effort"),
        focus_tools=[str(item) for item in focus_tools],
        invocations=[item for item in invocations if isinstance(item, dict)],
    )


def _perf_summary(doc: dict[str, Any]) -> OperationPerfSummary:
    stages = [
        OperationPerfStage(
            key=str(stage.get("key", "")),
            label=stage.get("label"),
            detail=stage.get("detail"),
            ms=stage.get("ms"),
            group=stage.get("group"),
            status=stage.get("status"),
        )
        for stage in doc.get("stages", [])
        if isinstance(stage, dict)
    ]
    return OperationPerfSummary(
        status=doc.get("status"),
        started_at=doc.get("started_at"),
        completed_at=doc.get("completed_at"),
        response_ms=doc.get("response_ms"),
        total_ms=doc.get("total_ms"),
        model=doc.get("model"),
        reasoning_effort=doc.get("reasoning_effort"),
        reasoning_chars=doc.get("reasoning_chars"),
        stages=stages,
        stt=_as_dict(doc.get("stt")) or None,
        turn_detection=_as_dict(doc.get("turn_detection")) or None,
        voice=_as_dict(doc.get("voice")) or None,
        tool_routing=_as_dict(doc.get("tool_routing")) or None,
    )


def _protocol_run(doc: dict[str, Any]) -> OperationProtocolRun:
    return OperationProtocolRun(
        protocol_name=str(doc.get("protocol_name", "")),
        triggered_by=doc.get("triggered_by"),
        started_at=doc.get("started_at"),
        completed_at=doc.get("completed_at"),
        status=str(doc.get("status", "unknown")),
    )


async def _load_turn_attempts(
    owner_id: str,
    turn_ids: list[str],
    *,
    perf_docs_by_turn: dict[str, dict[str, Any]] | None = None,
) -> dict[str, OperationTurnAttempt]:
    """Join conversation trace, perf, and protocol rows for each turn_id."""
    if not turn_ids:
        return {}

    traces_by_turn: dict[str, list[OperationTraceLine]] = defaultdict(list)
    perf_by_turn: dict[str, OperationPerfSummary] = {
        turn_id: _perf_summary(doc)
        for turn_id, doc in (perf_docs_by_turn or {}).items()
    }
    protocols_by_turn: dict[str, list[OperationProtocolRun]] = defaultdict(list)

    async for row in mongodb.db.conversations.find(
        {"owner_id": owner_id, "metadata.turn_id": {"$in": turn_ids}},
        {
            "_id": 0,
            "timestamp": 1,
            "role": 1,
            "content": 1,
            "metadata.turn_id": 1,
            "metadata.turn_type": 1,
            "metadata.tool_call_id": 1,
            "metadata.code": 1,
            "metadata.output": 1,
            "metadata.response_id": 1,
            "metadata.model": 1,
            "metadata.reasoning_effort": 1,
            "metadata.focus_tools": 1,
            "metadata.invocations": 1,
        },
    ).sort("timestamp", 1):
        meta = _as_dict(row.get("metadata"))
        turn_id = meta.get("turn_id")
        if turn_id:
            traces_by_turn[str(turn_id)].append(_trace_line(row))

    missing_perf_turn_ids = [turn_id for turn_id in turn_ids if turn_id not in perf_by_turn]
    if missing_perf_turn_ids:
        async for perf in mongodb.db.turn_runs.find(
            {"owner_id": owner_id, "turn_id": {"$in": missing_perf_turn_ids}},
            {"_id": 0},
        ):
            turn_id = perf.get("turn_id")
            if turn_id:
                perf_by_turn[str(turn_id)] = _perf_summary(perf)

    async for protocol in mongodb.db.protocol_runs.find(
        {"owner_id": owner_id, "turn_id": {"$in": turn_ids}},
        {"_id": 0},
    ).sort("started_at", 1):
        turn_id = protocol.get("turn_id")
        if turn_id:
            protocols_by_turn[str(turn_id)].append(_protocol_run(protocol))

    return {
        turn_id: OperationTurnAttempt(
            turn_id=turn_id,
            trace=traces_by_turn.get(turn_id, []),
            perf=perf_by_turn.get(turn_id),
            protocols=protocols_by_turn.get(turn_id, []),
        )
        for turn_id in turn_ids
    }


async def get_trigger_run_detail(owner_id: str, instance_id: str) -> OperationRunDetail | None:
    instance = await mongodb.db.trigger_instances.find_one(
        {"owner_id": owner_id, "id": instance_id},
        {"_id": 0},
    )
    if not instance:
        return None

    turn_ids = [str(tid) for tid in instance.get("turn_ids", []) if tid]
    attempts_by_turn = await _load_turn_attempts(owner_id, turn_ids)
    attempts = [attempts_by_turn[turn_id] for turn_id in turn_ids if turn_id in attempts_by_turn]

    return OperationRunDetail(
        id=str(instance["id"]),
        kind=_run_kind(instance),
        owner_id=str(instance["owner_id"]),
        status=str(instance.get("status", "")),
        rule_id=instance.get("rule_id"),
        source=_run_source(instance),
        due_at=instance["due_at"],
        created_at=instance["created_at"],
        updated_at=instance.get("updated_at"),
        completed_at=instance.get("completed_at"),
        result_text=instance.get("result_text"),
        failure_reason=instance.get("failure_reason"),
        origin_snapshot=_as_dict(instance.get("origin_snapshot")),
        action_snapshot=_as_dict(instance.get("action_snapshot")),
        source_event=_as_dict(instance.get("source_event")),
        turn_ids=turn_ids,
        attempts=attempts,
    )


def _user_turn_outcome(status: str | None) -> ActivityOutcome:
    if status in {"cancelled", "failed"}:
        return "failed"
    if status in {"running", "handoff"}:
        return "running"
    return "completed"


def _user_turn_summary(trace: list[OperationTraceLine], perf: OperationPerfSummary | None) -> str:
    for line in trace:
        if line.role == "user" and line.content.strip():
            return line.content.strip()[:USER_TURN_SUMMARY_CHARS]
    for line in trace:
        if line.turn_type == "tool_call" and (line.code or line.content):
            return f"Tool run: {(line.code or line.content)[:120]}"
        if line.role == "assistant" and line.content.strip() and line.content.strip() != "NO_REPLY":
            return line.content.strip()[:USER_TURN_SUMMARY_CHARS]
    if perf and perf.status == "cancelled":
        return "Turn cancelled"
    if perf and perf.status == "handoff":
        return "Turn interrupted (handoff)"
    return "User turn"


def _time_fields_from_dt(value: datetime | None) -> tuple[str, str]:
    if value is None:
        value = datetime.now(timezone.utc)
    fields = local_datetime_fields(value)
    return fields["time"], fields["utc_time"]


async def _load_visible_user_turn_rows(
    owner_id: str,
    *,
    limit: int,
    node_id: str | None = None,
) -> list[dict[str, Any]]:
    """Load content-backed user turns for the Operations User Turns facet."""
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "role": "user",
        "source": "user",
        "metadata.turn_id": {"$exists": True},
        "metadata.turn_type": {"$exists": False},
    }
    if node_id:
        query["metadata.node_id"] = node_id

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    async for row in mongodb.db.conversations.find(
        query,
        {
            "_id": 0,
            "role": 1,
            "source": 1,
            "content": 1,
            "timestamp": 1,
            "metadata.turn_id": 1,
            "metadata.node_id": 1,
            "metadata.node_label": 1,
        },
    ).sort("timestamp", -1).limit(limit):
        meta = _as_dict(row.get("metadata"))
        turn_id = str(meta.get("turn_id") or "")
        content = str(row.get("content", "")).strip()
        if row.get("role") != "user" or row.get("source") != "user" or meta.get("turn_type"):
            continue
        if not turn_id or turn_id in seen or not content:
            continue
        if node_id and meta.get("node_id") != node_id:
            continue
        seen.add(turn_id)
        rows.append(row)
    return rows


async def _has_turn_content(owner_id: str, turn_id: str) -> bool:
    cursor = mongodb.db.conversations.find(
        {
            "owner_id": owner_id,
            "metadata.turn_id": turn_id,
        },
        {"_id": 1},
    ).limit(1)
    docs = await cursor.to_list(length=1)
    return bool(docs)


async def get_user_turn_detail(owner_id: str, turn_id: str) -> OperationRunDetail | None:
    """Load a bare user or system turn by turn_id (no trigger envelope)."""
    if not await _has_turn_content(owner_id, turn_id):
        return None

    perf_doc = await mongodb.db.turn_runs.find_one(
        {"owner_id": owner_id, "turn_id": turn_id},
        {"_id": 0},
    )
    perf_doc = perf_doc or {}

    attempts_by_turn = await _load_turn_attempts(
        owner_id,
        [turn_id],
        perf_docs_by_turn={turn_id: perf_doc},
    )
    attempt = attempts_by_turn.get(turn_id)
    if attempt is None:
        attempt = OperationTurnAttempt(turn_id=turn_id)

    node_label = perf_doc.get("node_label")
    modality = perf_doc.get("modality")
    source = str(perf_doc.get("source") or "user")
    source_label = node_label or perf_doc.get("node_id") or modality or source

    return OperationRunDetail(
        id=turn_id,
        kind="system" if source == "system" else "user",
        owner_id=owner_id,
        status=str(perf_doc.get("status") or "unknown"),
        source=str(source_label),
        started_at=perf_doc.get("started_at"),
        completed_at=perf_doc.get("completed_at"),
        node_id=perf_doc.get("node_id"),
        node_label=node_label,
        modality=modality,
        turn_ids=[turn_id],
        attempts=[attempt],
    )


async def list_user_turns(
    owner_id: str,
    *,
    limit: int = 50,
    node_id: str | None = None,
) -> list[ActivityItem]:
    """Opt-in list of user-initiated turns for Operations facets (not default activity)."""
    visible_rows = await _load_visible_user_turn_rows(owner_id, limit=limit, node_id=node_id)
    if not visible_rows:
        return []

    turn_ids = [
        str(_as_dict(row.get("metadata")).get("turn_id"))
        for row in visible_rows
        if _as_dict(row.get("metadata")).get("turn_id")
    ]
    perf_docs: dict[str, dict[str, Any]] = {}
    async for perf in mongodb.db.turn_runs.find(
        {"owner_id": owner_id, "source": "user", "turn_id": {"$in": turn_ids}},
        {"_id": 0},
    ):
        turn_id = perf.get("turn_id")
        if turn_id:
            perf_docs[str(turn_id)] = perf

    items: list[ActivityItem] = []
    for row in visible_rows:
        meta = _as_dict(row.get("metadata"))
        turn_id = str(meta.get("turn_id") or "")
        if not turn_id:
            continue
        doc = perf_docs.get(turn_id, {})
        status = str(doc.get("status") or "completed")
        when, sort_at = _time_fields_from_dt(
            doc.get("started_at") or doc.get("completed_at") or row.get("timestamp")
        )
        node_label = doc.get("node_label") or meta.get("node_label")
        node_id_val = doc.get("node_id") or meta.get("node_id")
        source = node_label or node_id_val or doc.get("modality")
        items.append(
            ActivityItem(
                kind="user",
                id=turn_id,
                summary=str(row.get("content", "")).strip()[:USER_TURN_SUMMARY_CHARS],
                when=when,
                sort_at=sort_at,
                outcome=_user_turn_outcome(status),
                delivery=None,
                source=str(source) if source else None,
            )
        )
    return items
