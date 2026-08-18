"""TriggerScheduler — polls for due trigger instances and dispatches them.

Runs as a background asyncio task; poll interval defaults to 5 seconds.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional

from services.database.mongodb import mongodb
from services.events import Event, EventType, event_bus

from core.triggers.lifecycle import rule_doc_allows_dispatch

logger = logging.getLogger(__name__)


class TriggerScheduler:
    """Polls trigger_instances for due pending items and publishes TRIGGER_DUE."""

    def __init__(self, poll_interval: int = 5) -> None:
        self.poll_interval = poll_interval
        self.running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.running:
            return
        await self._recover_orphans()
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("TriggerScheduler started")

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("TriggerScheduler stopped")

    async def _poll_loop(self) -> None:
        while self.running:
            try:
                await self._process_due()
            except Exception:
                logger.exception("Error in TriggerScheduler poll loop")
            await asyncio.sleep(self.poll_interval)

    async def _process_due(self) -> None:
        """Atomically claim due pending instances and dispatch each one."""
        now = datetime.now(timezone.utc)
        retry_owner_ids = await mongodb.db.trigger_instances.distinct(
            "owner_id",
            {
                "status": "awaiting_delivery",
                "next_retry_at": {"$lte": now},
            },
        )
        for owner_id in retry_owner_ids:
            await event_bus.publish(
                Event(
                    type=EventType.TRIGGER_RETRY_AWAITING,
                    source="trigger_scheduler.retry_due",
                    data={"owner_id": owner_id, "retry_due_only": True},
                )
            )

        while True:
            doc = await mongodb.db.trigger_instances.find_one(
                {
                    "status": "pending",
                    "due_at": {"$lte": now},
                },
                sort=[("due_at", 1)],
            )
            if not doc:
                break

            instance_id = doc["id"]
            owner_id = doc["owner_id"]
            rule_id = doc.get("rule_id")
            if rule_id:
                rule_doc = await mongodb.db.trigger_rules.find_one({"id": rule_id})
                if rule_doc is None or not rule_doc_allows_dispatch(rule_doc, now=now):
                    reason = (
                        "parent_rule_missing"
                        if rule_doc is None
                        else "parent_rule_paused_or_disabled"
                    )
                    await mongodb.db.trigger_instances.update_one(
                        {"id": instance_id, "status": "pending"},
                        {
                            "$set": {
                                "status": "cancelled",
                                "completed_at": now,
                                "updated_at": now,
                                "failure_reason": reason,
                            }
                        },
                    )
                    continue

            claimed = await mongodb.db.trigger_instances.find_one_and_update(
                {"id": instance_id, "status": "pending"},
                {"$set": {"status": "claimed", "claimed_at": now, "updated_at": now}},
                return_document=True,
            )
            if not claimed:
                continue

            claimed.pop("_id", None)
            logger.info("TriggerScheduler claiming instance %s for %s", instance_id, owner_id)

            await event_bus.publish(
                Event(
                    type=EventType.TRIGGER_DUE,
                    source="trigger_scheduler",
                    data={"instance_id": instance_id, "owner_id": owner_id},
                )
            )

            if rule_id:
                await self._maybe_schedule_next(rule_id, claimed, now)

    async def _maybe_schedule_next(
        self, rule_id: str, instance_doc: dict, now: datetime
    ) -> None:
        """If the rule is recurring and enabled, create the next pending instance."""
        rule_doc = await mongodb.db.trigger_rules.find_one(
            {"id": rule_id, "enabled": True}
        )
        if not rule_doc:
            return
        if not rule_doc_allows_dispatch(rule_doc, now=now):
            return

        trigger = rule_doc.get("origin", {})
        recurrence = trigger.get("recurrence")
        if not recurrence:
            return

        from core.scheduling import next_occurrence, recurrence_rule_from_origin

        next_time = next_occurrence(
            recurrence_rule_from_origin(
                trigger,
                rule_doc=rule_doc,
                owner_id=instance_doc["owner_id"],
                rule_id=rule_id,
            ),
            now,
        )
        if not next_time:
            return

        from core.triggers.service import trigger_service
        from core.triggers.models import (
            AttentionPolicy,
            DeliveryPlan,
            FreshnessPolicy,
            ManagementOwnership,
            TriggerAction,
            TriggerOrigin,
        )

        created = await trigger_service.materialize_recurring_occurrence(
            owner_id=instance_doc["owner_id"],
            rule_id=rule_id,
            origin=TriggerOrigin.model_validate(rule_doc["origin"]),
            action=TriggerAction.model_validate(rule_doc["action"]),
            attention=AttentionPolicy.model_validate(rule_doc["attention"]),
            delivery=DeliveryPlan.model_validate(rule_doc["delivery"]),
            freshness=FreshnessPolicy.model_validate(rule_doc["freshness"]),
            due_at=next_time,
            management=ManagementOwnership.model_validate(rule_doc["management"]),
        )
        if created:
            logger.info(
                "Scheduled next occurrence for rule %s at %s",
                rule_id,
                next_time.isoformat(),
            )

    async def _recover_orphans(self) -> None:
        """On startup, reset claimed/executing instances to awaiting_delivery."""
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_many(
            {"status": {"$in": ["claimed", "executing"]}},
            {
                "$set": {
                    "status": "awaiting_delivery",
                    "failure_reason": "scheduler_recovery",
                    "updated_at": now,
                }
            },
        )
        if result.modified_count:
            logger.info(
                "Recovered %d orphaned trigger instances to awaiting_delivery",
                result.modified_count,
            )


trigger_scheduler = TriggerScheduler()
