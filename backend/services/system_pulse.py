"""
SystemPulse — periodic mechanical evaluator.

Ticks on a configurable interval (default 30m) and queries trigger_instances for
actionable conditions (overdue, stuck executing, failed, awaiting delivery). If
any are found AND they are not already suppressed by a recent escalation, creates
an evaluative TriggerInstance so the agent can decide whether to speak. Attention
state gates presentation later in trigger delivery, not this mechanical check.

Design properties:
- Zero LLM cost on null ticks. Only mechanical Mongo reads when nothing matches.
- Findings-level dedup (6h window) prevents nagging on the same finding.
- Uses trigger_instances instead of the old alerts collection.
- Single-owner by design (settings.DEFAULT_USER_ID); owner_id filters are forward-compat.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config import settings
from core.triggers.vocabulary import DECISION_OFFER
from services.database.mongodb import mongodb
from services.events import event_bus, Event, EventType

logger = logging.getLogger(__name__)

OVERDUE_GRACE = timedelta(minutes=5)
FAILURE_WINDOW = timedelta(hours=1)
DEDUP_WINDOW = timedelta(hours=6)
PER_BUCKET_LIMIT = 5


def _minutes_late(trigger_time: Optional[datetime], now: datetime) -> int:
    if not isinstance(trigger_time, datetime):
        return 0
    return max(0, int((now - trigger_time).total_seconds() // 60))


def _instance_message(instance: dict) -> str:
    action = instance.get("action_snapshot") or {}
    return action.get("message", "") if isinstance(action, dict) else ""


def _instance_due_at(instance: dict) -> Optional[datetime]:
    raw = instance.get("due_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return None


def _build_findings(
    overdue: list[dict],
    stuck_executing: list[dict],
    failures: list[dict],
    awaiting_delivery: list[dict],
    now: datetime,
) -> dict[str, list[dict]]:
    """Shape raw query results into capped, key-stable finding buckets."""
    return {
        "overdue_triggers": [
            {
                "key": a.get("id", str(a.get("_id", ""))),
                "message": _instance_message(a),
                "minutes_late": _minutes_late(_instance_due_at(a), now),
            }
            for a in overdue[:PER_BUCKET_LIMIT]
        ],
        "stuck_executing": [
            {
                "key": a.get("id", str(a.get("_id", ""))),
                "message": _instance_message(a),
                "minutes_late": _minutes_late(_instance_due_at(a), now),
            }
            for a in stuck_executing[:PER_BUCKET_LIMIT]
        ],
        "failed_automations": [
            {
                "key": f"{d.get('rule_id', '')}:{d.get('item_id', '')}",
                "error": d.get("error", ""),
            }
            for d in failures[:PER_BUCKET_LIMIT]
        ],
        "awaiting_delivery": [
            {
                "key": a.get("id", str(a.get("_id", ""))),
                "message": _instance_message(a),
            }
            for a in awaiting_delivery[:PER_BUCKET_LIMIT]
        ],
    }


def _flatten_keys(findings: dict[str, list[dict]]) -> set[str]:
    """Extract {bucket:key} pairs so dedup compares across bucket and item."""
    return {f"{bucket}:{item['key']}" for bucket, items in findings.items() for item in items}


_BUCKET_LABELS = {
    "overdue_triggers": "Overdue triggers",
    "stuck_executing": "Triggers stuck executing",
    "failed_automations": "Recent automation failures",
    "awaiting_delivery": "Triggers awaiting delivery",
}


def _format_findings_message(findings: dict[str, list[dict]]) -> str:
    """Render findings as human-readable multiline bullets for the pulse message."""
    lines: list[str] = ["System pulse surfaced actionable state:"]
    for bucket, items in findings.items():
        if not items:
            continue
        lines.append(f"- {_BUCKET_LABELS.get(bucket, bucket)} ({len(items)}):")
        for item in items:
            if bucket in ("overdue_triggers", "stuck_executing"):
                lines.append(f"    * {item['message']!r} ({item['minutes_late']}m late)")
            elif bucket == "failed_automations":
                lines.append(f"    * {item['key']} — {item['error']}")
            else:
                lines.append(f"    * {item['message']!r}")
    return "\n".join(lines)


class SystemPulse:
    """Periodic evaluator. Mirrors TriggerScheduler / AutomationService lifecycle."""

    def __init__(
        self,
        *,
        interval_min: Optional[int] = None,
        owner_id: Optional[str] = None,
    ):
        interval = interval_min if interval_min is not None else settings.SYSTEM_PULSE_INTERVAL_MIN
        self.interval_s = interval * 60
        self.owner_id = owner_id or settings.DEFAULT_USER_ID
        self.running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "SystemPulse started (interval=%ds)",
            self.interval_s,
        )

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SystemPulse stopped")

    async def _poll_loop(self) -> None:
        # Sleep first so crash-restart loops don't spam pulse checks.
        while self.running:
            await asyncio.sleep(self.interval_s)
            if not self.running:
                break
            try:
                await self._tick()
            except Exception as e:
                logger.error("SystemPulse tick failed: %s", e, exc_info=True)

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        overdue_cutoff = now - OVERDUE_GRACE
        failure_cutoff = now - FAILURE_WINDOW

        # Four indexed point queries — fire in parallel, walltime == slowest.
        overdue, stuck_executing, failures, awaiting_delivery = await asyncio.gather(
            mongodb.db.trigger_instances.find({
                "status": "pending",
                "due_at": {"$lt": overdue_cutoff},
                "owner_id": self.owner_id,
            }).to_list(PER_BUCKET_LIMIT),
            mongodb.db.trigger_instances.find({
                "status": {"$in": ["claimed", "executing"]},
                "due_at": {"$lt": overdue_cutoff},
                "owner_id": self.owner_id,
            }).to_list(PER_BUCKET_LIMIT),
            mongodb.db.automation_fired.find({
                "status": "failed",
                "failed_at": {"$gt": failure_cutoff},
            }).to_list(PER_BUCKET_LIMIT),
            mongodb.db.trigger_instances.find({
                "status": "awaiting_delivery",
                "owner_id": self.owner_id,
                "$or": [
                    {"next_retry_at": {"$exists": False}},
                    {"next_retry_at": None},
                ],
            }).to_list(PER_BUCKET_LIMIT),
        )

        findings = _build_findings(overdue, stuck_executing, failures, awaiting_delivery, now)
        current_keys = _flatten_keys(findings)

        if not current_keys:
            await self._log_run(now, escalated=False, reason="empty")
            return

        last = await mongodb.db.pulse_runs.find_one(
            {"escalated": True, "tick_at": {"$gt": now - DEDUP_WINDOW}},
            sort=[("tick_at", -1)],
        )
        last_keys: set[str] = set(last.get("findings_keys", [])) if last else set()
        new_keys = current_keys - last_keys

        if not new_keys:
            await self._log_run(
                now, escalated=False, reason="suppressed", findings_keys=current_keys,
            )
            logger.debug("SystemPulse suppressed: %d keys all previously escalated", len(current_keys))
            return

        from core.triggers.service import trigger_service
        from core.triggers.models import (
            AttentionPolicy,
            DeliveryPlan,
            FreshnessPolicy,
            TriggerAction,
            TriggerOrigin,
        )
        message = _format_findings_message(findings)
        instance = await trigger_service.create_instance(
            owner_id=self.owner_id,
            origin=TriggerOrigin(kind="system"),
            action=TriggerAction(decision=DECISION_OFFER, message=message),
            attention=AttentionPolicy(level="normal", sound="none"),
            delivery=DeliveryPlan(),
            freshness=FreshnessPolicy(),
            source_event={"findings_keys": sorted(current_keys)},
        )
        await event_bus.publish(
            Event(
                type=EventType.TRIGGER_DUE,
                source="system_pulse",
                data={"instance_id": instance.id, "owner_id": self.owner_id},
            )
        )
        # TODO: per-turn LLM token cost lands in Phase 8 cost-tracking.
        await self._log_run(
            now,
            escalated=True,
            reason="escalated",
            findings_keys=current_keys,
            new_keys=new_keys,
        )
        logger.info(
            "SystemPulse escalated: %d findings (%d new) via evaluate",
            len(current_keys), len(new_keys),
        )

    async def _log_run(
        self,
        tick_at: datetime,
        *,
        escalated: bool,
        reason: str,
        findings_keys: Optional[set[str]] = None,
        new_keys: Optional[set[str]] = None,
    ) -> None:
        doc: dict[str, Any] = {"tick_at": tick_at, "escalated": escalated, "reason": reason}
        if findings_keys is not None:
            doc["findings_keys"] = sorted(findings_keys)
        if new_keys is not None:
            doc["new_keys"] = sorted(new_keys)
        try:
            await mongodb.db.pulse_runs.insert_one(doc)
        except Exception as e:
            logger.warning("Could not persist pulse_runs entry: %s", e)


system_pulse = SystemPulse()
