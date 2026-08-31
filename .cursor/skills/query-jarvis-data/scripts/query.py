#!/usr/bin/env python3
"""Query JARV1S MongoDB. Run from backend/: uv run python ../.cursor/skills/query-jarvis-data/scripts/query.py …"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from core.config import settings
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from sources import DataSource, format_source, pick_source

DB_NAME = "jarvis"
KEY_COLS = (
    "conversations",
    "turn_runs",
    "trigger_rules",
    "trigger_instances",
    "background_tasks",
    "protocols",
)


def _preview(content: Any, limit: int = 120) -> str:
    text = str(content or "").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _compact_json(value: Any, limit: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return _preview(value, limit)
    try:
        text = json.dumps(value, default=str, separators=(",", ":"))
    except TypeError:
        text = str(value)
    return _preview(text, limit)


def _meta_first(rows: list[dict[str, Any]], key: str) -> Any:
    for row in rows:
        value = (row.get("metadata") or {}).get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _called_summary(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        meta = row.get("metadata") or {}
        if meta.get("turn_type") != "tool_call":
            continue
        capability = meta.get("capability")
        if capability:
            counts[str(capability)] = counts.get(str(capability), 0) + 1
    parts = [
        cap if n == 1 else f"{cap}×{n}"
        for cap, n in counts.items()
    ]
    return ", ".join(parts)


def _routed_summary(routed: Any) -> str:
    names = [str(name) for name in (routed if isinstance(routed, list) else [routed])]
    if len(names) <= 6:
        return ", ".join(names)
    grouped: dict[str, list[str]] = {}
    for name in names:
        plugin = name.split(".", 1)[0] if "." in name else name
        grouped.setdefault(plugin, []).append(name)
    return ", ".join(
        f"{plugin} ({len(group)})" if len(group) > 1 else group[0]
        for plugin, group in grouped.items()
    )


def format_turn_dump(
    rows: list[dict[str, Any]],
    perf: dict[str, Any] | None = None,
) -> str:
    """One-screen ledger from stored conversation + turn_runs fields."""
    lines: list[str] = []
    source = next((row.get("source") for row in rows if row.get("source")), None)
    decision = _meta_first(rows, "decision")
    rule_id = _meta_first(rows, "rule_id")
    instance_id = _meta_first(rows, "instance_id")
    routed = _meta_first(rows, "routed_tools")
    header_bits = []
    if source:
        header_bits.append(f"source={source}")
    if decision:
        header_bits.append(f"decision={decision}")
    if rule_id:
        header_bits.append(f"rule_id={rule_id}")
    if instance_id:
        header_bits.append(f"instance_id={instance_id}")
    lines.append("[header]")
    lines.append(f"  {' '.join(header_bits) if header_bits else '(no origin fields)'}")
    called = _called_summary(rows)
    if called:
        lines.append(f"  called: {called}")
    if routed:
        lines.append(f"  routed_tools: {_routed_summary(routed)}")
    routing = (perf or {}).get("tool_routing") or {}
    if routing:
        routing_bits = []
        plugins = routing.get("matched_plugins")
        if plugins:
            routing_bits.append(
                f"matched_plugins={','.join(str(p) for p in plugins)}"
                if isinstance(plugins, list)
                else f"matched_plugins={plugins}"
            )
        if routing.get("routed_tool_count") is not None:
            routing_bits.append(f"routed_tool_count={routing['routed_tool_count']}")
        if routing_bits:
            lines.append(f"  tool_routing: {' '.join(routing_bits)}")
    if perf:
        lines.append("[turn_runs]")
        lines.append(
            f"  {perf.get('modality')} {perf.get('status')} "
            f"response={perf.get('response_ms')}ms total={perf.get('total_ms')}ms "
            f"node={perf.get('node_id')}"
        )

    if rows:
        lines.append("[ledger]")
        for index, row in enumerate(rows, start=1):
            meta = row.get("metadata") or {}
            tt = meta.get("turn_type") or row.get("role") or "?"
            content = row.get("content")
            prefix = f"  {index}."
            if tt == "tool_call":
                capability = meta.get("capability") or "?"
                args = _compact_json(meta.get("arguments") or {}, 280)
                spoken = meta.get("spoken") or ""
                call = f"{capability}({args})"
                lines.append(f"{prefix} tool_call {call}")
                if spoken:
                    lines.append(f"      spoken={_preview(spoken, 160)!r}")
            elif tt == "tool_result":
                invocations = meta.get("invocations") if isinstance(meta.get("invocations"), list) else []
                status = meta.get("status")
                capability = meta.get("capability")
                if invocations and isinstance(invocations[0], dict):
                    inv = invocations[0]
                    capability = inv.get("capability") or capability
                    status = inv.get("status") or status
                extra = f" {capability}" if capability else ""
                status_bit = f" status={status}" if status else ""
                lines.append(f"{prefix} tool_result{status_bit}{extra}")
                preview = _preview(meta.get("output") if meta.get("output") is not None else content, 200)
                if preview:
                    lines.append(f"      {preview!r}")
            else:
                lines.append(f"{prefix} {tt} {_preview(content, 200)!r}")
    return "\n".join(lines)


@asynccontextmanager
async def open_db(source: DataSource) -> AsyncIterator[AsyncIOMotorDatabase]:
    client = AsyncIOMotorClient(source.mongodb_url, serverSelectionTimeoutMS=5000)
    try:
        await client.admin.command("ping")
    except Exception as exc:
        client.close()
        hint = ""
        if source.name == "app":
            hint = " Start the JARV1S desktop app."
        raise SystemExit(f"Cannot reach {source.name} MongoDB ({source.mongodb_url}): {exc}.{hint}") from exc
    try:
        yield client[DB_NAME]
    finally:
        client.close()


async def cmd_status(source: DataSource, owner_id: str) -> None:
    print(format_source(source))
    async with open_db(source) as db:
        print(f"\ndatabase: {DB_NAME}")
        for name in KEY_COLS:
            n = await db[name].estimated_document_count()
            print(f"  {name}: {n}")
        owners = await db.conversations.distinct("owner_id")
        print(f"owner_ids: {owners or ['(none)']}")
        if owners and owner_id not in owners:
            print(f"warning: --owner {owner_id!r} not in {owners}")


async def cmd_turn(source: DataSource, owner_id: str, turn_id: str) -> None:
    print(f"source: {source.name}")
    async with open_db(source) as db:
        perf = await db.turn_runs.find_one(
            {"owner_id": owner_id, "turn_id": turn_id},
            {"_id": 0},
        )
        rows = await db.conversations.find(
            {"owner_id": owner_id, "metadata.turn_id": turn_id},
            {"_id": 0, "role": 1, "content": 1, "timestamp": 1, "metadata": 1, "source": 1},
        ).sort("timestamp", 1).to_list(200)

        print(f"turn_id: {turn_id}")
        if not perf and not rows:
            print("not found")
            return
        print(format_turn_dump(rows, perf=perf))


async def cmd_recent(
    source: DataSource,
    owner_id: str,
    *,
    hours: int,
    limit: int,
    min_response_ms: int | None,
    node_id: str | None,
) -> None:
    print(f"source: {source.name}")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "source": "user",
        "completed_at": {"$gte": since},
    }
    if min_response_ms is not None:
        query["response_ms"] = {"$gte": min_response_ms}
    if node_id:
        query["node_id"] = node_id

    sort_key = "response_ms" if min_response_ms else "completed_at"
    async with open_db(source) as db:
        cursor = (
            db.turn_runs.find(
                query,
                {
                    "_id": 0,
                    "turn_id": 1,
                    "modality": 1,
                    "response_ms": 1,
                    "completed_at": 1,
                    "node_id": 1,
                },
            )
            .sort(sort_key, -1)
            .limit(limit)
        )
        label = f"recent ({hours}h)" if not min_response_ms else f"slow (>={min_response_ms}ms, {hours}h)"
        print(label)
        async for doc in cursor:
            print(
                f"  {doc.get('turn_id')} {doc.get('modality')} "
                f"response={doc.get('response_ms')}ms node={doc.get('node_id')}"
            )


async def cmd_rules(
    source: DataSource,
    owner_id: str,
    *,
    query: str | None,
    enabled_only: bool,
) -> None:
    print(f"source: {source.name}")
    mongo_query: dict[str, Any] = {"owner_id": owner_id}
    if enabled_only:
        mongo_query["enabled"] = True
    if query:
        mongo_query["$or"] = [
            {"name": {"$regex": query, "$options": "i"}},
            {"action.instructions": {"$regex": query, "$options": "i"}},
            {"action.message": {"$regex": query, "$options": "i"}},
        ]

    async with open_db(source) as db:
        rules = await db.trigger_rules.find(mongo_query, {"_id": 0}).sort(
            "updated_at", -1
        ).to_list(100)
        if not rules:
            print("no rules matched")
            return
        for rule in rules:
            origin = rule.get("origin") or {}
            action = rule.get("action") or {}
            print(
                f"{rule.get('id')} | {rule.get('name')} | "
                f"enabled={rule.get('enabled')} | "
                f"local={origin.get('original_local_time')} | "
                f"recurrence={origin.get('recurrence')} | "
                f"decision={action.get('decision')}"
            )
            text = action.get("instructions") or action.get("message") or ""
            if text:
                print(f"  {_preview(text, 140)}")


async def cmd_rule(source: DataSource, owner_id: str, rule_id: str) -> None:
    print(f"source: {source.name}")
    async with open_db(source) as db:
        rule = await db.trigger_rules.find_one(
            {"owner_id": owner_id, "id": rule_id},
            {"_id": 0},
        )
        if not rule:
            print("rule not found")
            return
        print(json.dumps(rule, indent=2, default=str))

        instances = await db.trigger_instances.find(
            {"owner_id": owner_id, "rule_id": rule_id},
            {"_id": 0, "id": 1, "status": 1, "due_at": 1, "created_at": 1, "completed_at": 1},
        ).sort("due_at", -1).limit(8).to_list(8)
        if instances:
            print("\n[recent instances]")
            for inst in instances:
                print(json.dumps(inst, default=str))


async def cmd_search(
    source: DataSource,
    owner_id: str,
    pattern: str,
    *,
    limit: int,
    role: str | None,
) -> None:
    print(f"source: {source.name}")
    mongo_query: dict[str, Any] = {
        "owner_id": owner_id,
        "content": {"$regex": pattern, "$options": "i"},
    }
    if role:
        mongo_query["role"] = role

    async with open_db(source) as db:
        rows = await db.conversations.find(
            mongo_query,
            {"_id": 0, "role": 1, "content": 1, "timestamp": 1, "metadata": 1},
        ).sort("timestamp", -1).limit(limit).to_list(limit)
        if not rows:
            print("no matches")
            return
        for row in rows:
            meta = row.get("metadata") or {}
            print(
                f"\n[{row.get('timestamp')}] role={row.get('role')} "
                f"type={meta.get('turn_type')} turn={meta.get('turn_id')}"
            )
            print(_preview(row.get("content"), 500))


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * p
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _fmt_ms(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def _td(row: dict[str, Any]) -> dict[str, Any]:
    return row.get("turn_detection") or {}


def _eou_ms(row: dict[str, Any]) -> float | None:
    value = _td(row).get("end_of_turn_delay_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _tx_ms(row: dict[str, Any]) -> float | None:
    value = _td(row).get("transcription_delay_ms")
    return float(value) if isinstance(value, (int, float)) else None


def _is_short(row: dict[str, Any], short_chars: int) -> bool:
    return int(_td(row).get("text_chars") or 0) <= short_chars


def _bottleneck(row: dict[str, Any]) -> str:
    """Classify speech-end→commit delay into actionable buckets."""
    eou = _eou_ms(row)
    tx = _tx_ms(row) or 0.0
    if eou is None:
        return "unknown"
    if tx >= 150:
        return "stt_lag"
    if eou <= 220:
        return "min_floor"
    if "max_delay" in str(_td(row).get("reason") or ""):
        return "force_wait"
    return "eou_wait"


async def cmd_eou(
    source: DataSource,
    owner_id: str,
    *,
    hours: int,
    short_chars: int,
) -> None:
    """Scorecard for speech-end→commit latency vs early-cut proxy (fast recovery)."""
    print(f"source: {source.name}")
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    query: dict[str, Any] = {
        "owner_id": owner_id,
        "source": "user",
        "modality": "voice",
        "completed_at": {"$gte": since},
        "turn_detection.end_of_turn_delay_ms": {"$exists": True},
    }

    async with open_db(source) as db:
        docs = await db.turn_runs.find(
            query,
            {
                "_id": 0,
                "turn_id": 1,
                "turn_detection": 1,
                "voice": 1,
            },
        ).sort("completed_at", -1).to_list(500)

    if not docs:
        print(f"eou ({hours}h): no instrumented voice turns")
        return

    groups: dict[str, list[dict[str, Any]]] = {}
    for doc in docs:
        td = doc.get("turn_detection") or {}
        profile = td.get("endpointing_profile") or "(untagged)"
        groups.setdefault(profile, []).append(doc)

    print(f"eou ({hours}h, n={len(docs)})  short=text_chars≤{short_chars}")
    print(
        f"{'profile':<56} {'n':>3} {'eou50':>5} {'eou90':>5} {'short50':>7} {'short90':>7} "
        f"{'tx50':>5} {'astt%':>5} {'rec*%':>5}"
    )
    for profile, rows in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        eous = [v for row in rows if (v := _eou_ms(row)) is not None]
        txs = [v for row in rows if (v := _tx_ms(row)) is not None]
        short_rows = [row for row in rows if _is_short(row, short_chars)]
        short_eous = [v for row in short_rows if (v := _eou_ms(row)) is not None]
        awaiting = sum(1 for row in rows if int(_td(row).get("awaiting_stt_count") or 0) > 0)
        recovered = sum(1 for row in rows if (row.get("voice") or {}).get("recovered"))
        label = profile if len(profile) <= 56 else profile[:53] + "..."
        print(
            f"{label:<56} {len(rows):3d} {_fmt_ms(_percentile(eous, 0.5)):>5} "
            f"{_fmt_ms(_percentile(eous, 0.9)):>5} {_fmt_ms(_percentile(short_eous, 0.5)):>7} "
            f"{_fmt_ms(_percentile(short_eous, 0.9)):>7} {_fmt_ms(_percentile(txs, 0.5)):>5} "
            f"{100.0 * awaiting / len(rows):4.0f}% {100.0 * recovered / len(rows):3.0f}%"
        )

        # Bottleneck mix on short turns — the lever that actually moves perceived submit lag.
        if short_rows:
            counts = {"min_floor": 0, "eou_wait": 0, "stt_lag": 0, "force_wait": 0, "unknown": 0}
            for row in short_rows:
                counts[_bottleneck(row)] += 1
            total = len(short_rows)
            print(
                "  short bottlenecks: "
                f"min_floor={100.0 * counts['min_floor'] / total:.0f}% "
                f"eou_wait={100.0 * counts['eou_wait'] / total:.0f}% "
                f"stt_lag={100.0 * counts['stt_lag'] / total:.0f}% "
                f"force_wait={100.0 * counts['force_wait'] / total:.0f}%"
            )
    print("* rec% is fast recovery within its active window, not a complete early-cut rate.")
    print(
        "* bottlenecks: min_floor≈min_delay, eou_wait≈detector patience, "
        "stt_lag≈transcript after speech-end, force_wait≈max_delay commit"
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Query JARV1S runtime data")
    parser.add_argument("--owner", default=settings.DEFAULT_USER_ID)
    parser.add_argument(
        "--source",
        choices=["app", "dev"],
        default="app",
        help="app = installed desktop host (default); dev = contributor Docker on localhost:27018",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Source + key collection counts")

    turn = sub.add_parser("turn", help="Trace + perf for one turn_id")
    turn.add_argument("turn_id")

    recent = sub.add_parser("recent", help="Recent user turns")
    recent.add_argument("--hours", type=int, default=24)
    recent.add_argument("--limit", type=int, default=20)
    recent.add_argument("--min-response-ms", type=int)
    recent.add_argument("--node-id")

    eou = sub.add_parser("eou", help="EOU submit-latency scorecard by endpointing_profile")
    eou.add_argument("--hours", type=int, default=24)
    eou.add_argument(
        "--short-chars",
        type=int,
        default=35,
        help="Maximum transcript length included in short90 (default: 35)",
    )

    rules = sub.add_parser("rules", help="List trigger rules")
    rules.add_argument("-q", "--query", help="Regex on name/instructions/message")
    rules.add_argument("--enabled-only", action="store_true")

    rule = sub.add_parser("rule", help="One rule + recent instances")
    rule.add_argument("rule_id")

    search = sub.add_parser("search", help="Search conversations (regex)")
    search.add_argument("pattern")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument("--role", choices=["user", "assistant", "system"])

    args = parser.parse_args()
    source = await pick_source(args.source)

    if args.command == "status":
        await cmd_status(source, args.owner)
    elif args.command == "turn":
        await cmd_turn(source, args.owner, args.turn_id)
    elif args.command == "recent":
        await cmd_recent(
            source,
            args.owner,
            hours=args.hours,
            limit=args.limit,
            min_response_ms=args.min_response_ms,
            node_id=args.node_id,
        )
    elif args.command == "eou":
        await cmd_eou(
            source,
            args.owner,
            hours=args.hours,
            short_chars=args.short_chars,
        )
    elif args.command == "rules":
        await cmd_rules(
            source, args.owner, query=args.query, enabled_only=args.enabled_only
        )
    elif args.command == "rule":
        await cmd_rule(source, args.owner, args.rule_id)
    elif args.command == "search":
        await cmd_search(
            source, args.owner, args.pattern, limit=args.limit, role=args.role
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
