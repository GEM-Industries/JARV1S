"""TriggerService — create, claim, and lifecycle-manage trigger instances."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from pymongo.errors import DuplicateKeyError  # type: ignore[import-not-found]

from core.activity_events import publish_activity_changed
from core.id import generate_id
from core.triggers.models import (
    AttentionPolicy,
    DeliveryPlan,
    FreshnessPolicy,
    ManagementOwnership,
    TriggerAction,
    TriggerCondition,
    TriggerInstance,
    TriggerRule,
    TriggerOrigin,
)
from services.database.mongodb import mongodb

logger = logging.getLogger(__name__)


def schedule_dedup_key(rule_id: str, due_at: datetime) -> str:
    return f"schedule:{rule_id}:{due_at.isoformat()}"


def _validate_rule_parts(
    *,
    origin: TriggerOrigin,
    action: TriggerAction,
    delivery: DeliveryPlan,
) -> None:
    if origin.kind in {"time", "interval"} and origin.source:
        raise ValueError("time/interval trigger rules must not set origin.source")
    if origin.kind == "external" and not origin.source:
        raise ValueError("external trigger rules require origin.source")
    if action.protocol_name and not action.protocol_name.strip():
        raise ValueError("protocol_name trigger actions require a non-empty protocol_name")


class TriggerService:
    """Thin persistence layer for trigger rules and instances.

    Provides create, claim, complete, fail, snooze, cancel, and
    awaiting_delivery transitions.
    """

    async def _publish_activity_changed_for_instance(self, instance_id: str) -> None:
        find_one = getattr(mongodb.db.trigger_instances, "find_one", None)
        if find_one is None:
            return
        doc = await find_one(
            {"id": instance_id},
            {"_id": 0, "owner_id": 1},
        )
        owner_id = doc.get("owner_id") if doc else None
        if owner_id:
            await publish_activity_changed(str(owner_id))

    # ------------------------------------------------------------------
    # Rules
    # ------------------------------------------------------------------

    async def create_rule(
        self,
        *,
        owner_id: str,
        name: str,
        description: str | None = None,
        surface: bool = True,
        origin: TriggerOrigin,
        action: TriggerAction,
        attention: AttentionPolicy,
        delivery: DeliveryPlan,
        freshness: FreshnessPolicy,
        management: ManagementOwnership,
        conditions: list[TriggerCondition | dict[str, Any]] | None = None,
    ) -> TriggerRule:
        _validate_rule_parts(origin=origin, action=action, delivery=delivery)
        now = datetime.now(timezone.utc)
        rule_id = generate_id("rule-")
        if management.resource_id is None:
            management = management.model_copy(update={"resource_id": rule_id})
        rule = TriggerRule(
            id=rule_id,
            owner_id=owner_id,
            name=name,
            description=description,
            enabled=True,
            surface=surface,
            created_at=now,
            updated_at=now,
            origin=origin,
            conditions=conditions or [],
            action=action,
            attention=attention,
            delivery=delivery,
            freshness=freshness,
            management=management,
        )
        doc = rule.model_dump(mode="python")
        await mongodb.db.trigger_rules.insert_one(doc)
        logger.info("Created trigger rule %s (%s)", rule.id, name)
        return rule

    async def get_rule(self, rule_id: str) -> TriggerRule | None:
        doc = await mongodb.db.trigger_rules.find_one({"id": rule_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return TriggerRule.model_validate(doc)

    async def disable_rule(self, rule_id: str) -> None:
        await mongodb.db.trigger_rules.update_one(
            {"id": rule_id},
            {"$set": {"enabled": False, "updated_at": datetime.now(timezone.utc)}},
        )

    # ------------------------------------------------------------------
    # Instances
    # ------------------------------------------------------------------

    async def create_instance(
        self,
        *,
        owner_id: str,
        origin: TriggerOrigin,
        action: TriggerAction,
        attention: AttentionPolicy,
        delivery: DeliveryPlan,
        rule_id: str | None = None,
        due_at: datetime | None = None,
        source_event: dict[str, Any] | None = None,
        dedup_key: str | None = None,
        freshness: FreshnessPolicy,
        management: ManagementOwnership | None = None,
    ) -> TriggerInstance:
        now = datetime.now(timezone.utc)
        effective_due = due_at or origin.fire_at or now
        instance_id = generate_id("trg-")
        if rule_id is not None and management is None:
            raise ValueError("rule-linked trigger instances require management ownership")
        if management is None:
            provider = {
                "external": "automations",
                "system": "system",
                "time": "scheduler",
                "interval": "scheduler",
            }.get(origin.kind, "scheduler")
            management = ManagementOwnership(
                provider=provider,
                resource_id=instance_id,
            )
        instance = TriggerInstance(
            id=instance_id,
            rule_id=rule_id,
            owner_id=owner_id,
            status="pending",
            due_at=effective_due,
            created_at=now,
            origin_snapshot=origin,
            action_snapshot=action,
            attention_snapshot=attention,
            delivery_snapshot=delivery,
            freshness_snapshot=freshness,
            source_event=source_event or {},
            dedup_key=dedup_key,
            management=management,
        )
        doc = instance.model_dump(mode="python", exclude_none=True)
        try:
            await mongodb.db.trigger_instances.insert_one(doc)
        except DuplicateKeyError:
            if not dedup_key:
                raise
            logger.info("Trigger dedup: skipping duplicate key %s", dedup_key)
            existing = await mongodb.db.trigger_instances.find_one(
                {"dedup_key": dedup_key}
            )
            if existing:
                existing.pop("_id", None)
                return TriggerInstance.model_validate(existing)
            raise
        logger.info(
            "Created trigger instance %s (rule=%s, due=%s, action=%s)",
            instance.id,
            rule_id or "none",
            effective_due.isoformat(),
            action.decision,
        )
        return instance

    async def has_pending_for_rule(self, rule_id: str) -> bool:
        doc = await mongodb.db.trigger_instances.find_one(
            {"rule_id": rule_id, "status": "pending"},
            projection={"_id": 1},
        )
        return doc is not None

    async def materialize_recurring_occurrence(
        self,
        *,
        owner_id: str,
        rule_id: str,
        origin: TriggerOrigin,
        action: TriggerAction,
        attention: AttentionPolicy,
        delivery: DeliveryPlan,
        freshness: FreshnessPolicy,
        due_at: datetime,
        management: ManagementOwnership,
    ) -> TriggerInstance | None:
        """Create the next pending occurrence for a recurring rule, idempotently."""
        if await self.has_pending_for_rule(rule_id):
            logger.info(
                "Skipping recurring materialization for rule %s: pending instance exists",
                rule_id,
            )
            return None
        return await self.create_instance(
            owner_id=owner_id,
            rule_id=rule_id,
            origin=origin,
            action=action,
            attention=attention,
            delivery=delivery,
            freshness=freshness,
            due_at=due_at,
            dedup_key=schedule_dedup_key(rule_id, due_at),
            management=management,
        )

    async def supersede_awaiting_for_rule(
        self,
        rule_id: str,
        *,
        keep_instance_id: str,
        reason: str = "superseded_by_newer_occurrence",
    ) -> int:
        """Expire older awaiting_delivery siblings for the same recurring series."""
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_many(
            {
                "rule_id": rule_id,
                "status": "awaiting_delivery",
                "id": {"$ne": keep_instance_id},
            },
            {
                "$set": {
                    "status": "expired",
                    "completed_at": now,
                    "updated_at": now,
                    "failure_reason": reason,
                }
            },
        )
        return result.modified_count

    async def supersede_siblings_after_settlement(self, instance_id: str) -> None:
        instance = await self.get_instance(instance_id)
        if not instance or not instance.rule_id:
            return
        count = await self.supersede_awaiting_for_rule(
            instance.rule_id,
            keep_instance_id=instance_id,
            reason="superseded_by_settled_occurrence",
        )
        if count:
            logger.info(
                "Expired %d awaiting_delivery sibling(s) for rule %s after %s settled",
                count,
                instance.rule_id,
                instance_id,
            )

    async def claim_instance(self, instance_id: str) -> TriggerInstance | None:
        """Atomically move a pending instance to claimed. Returns None if already taken."""
        now = datetime.now(timezone.utc)
        doc = await mongodb.db.trigger_instances.find_one_and_update(
            {"id": instance_id, "status": "pending"},
            {"$set": {"status": "claimed", "claimed_at": now, "updated_at": now}},
            return_document=True,
        )
        if not doc:
            return None
        await publish_activity_changed(str(doc["owner_id"]))
        doc.pop("_id", None)
        return TriggerInstance.model_validate(doc)

    async def mark_executing(self, instance_id: str) -> bool:
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "status": "claimed"},
            {"$set": {"status": "executing", "updated_at": now}},
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def record_turn_id(self, instance_id: str, turn_id: str) -> None:
        now = datetime.now(timezone.utc)
        await mongodb.db.trigger_instances.update_one(
            {"id": instance_id},
            {
                "$addToSet": {"turn_ids": turn_id},
                "$set": {"updated_at": now},
            },
        )

    async def complete_instance(
        self,
        instance_id: str,
        *,
        result_text: str | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id},
            {
                "$set": {
                    "status": "completed",
                    "completed_at": now,
                    "updated_at": now,
                    "result_text": result_text,
                }
            },
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def mark_delivered(
        self,
        instance_id: str,
        *,
        result_text: str | None = None,
    ) -> bool:
        """Mark a trigger instance whose user-facing delivery succeeded."""
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "status": {"$in": ["claimed", "executing"]}},
            {
                "$set": {
                    "status": "delivered",
                    "delivered_at": now,
                    "updated_at": now,
                    "result_text": result_text,
                },
                "$unset": {"failure_reason": "", "next_retry_at": ""},
            },
        )
        if result.modified_count:
            await self.supersede_siblings_after_settlement(instance_id)
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def suppress_instance(self, instance_id: str, *, reason: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        update: dict[str, Any] = {
            "status": "suppressed",
            "completed_at": now,
            "updated_at": now,
        }
        if reason:
            update["failure_reason"] = reason
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id},
            {"$set": update},
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)

    async def expire_instance(self, instance_id: str, *, reason: str) -> bool:
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "status": {"$in": ["pending", "claimed", "executing", "awaiting_delivery"]}},
            {
                "$set": {
                    "status": "expired",
                    "completed_at": now,
                    "updated_at": now,
                    "failure_reason": reason,
                }
            },
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def fail_instance(self, instance_id: str, *, reason: str) -> None:
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id},
            {
                "$set": {
                    "status": "failed",
                    "completed_at": now,
                    "updated_at": now,
                    "failure_reason": reason,
                }
            },
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)

    async def mark_awaiting_delivery(
        self,
        instance_id: str,
        *,
        reason: str = "no_target",
        next_retry_at: datetime | None = None,
    ) -> bool:
        now = datetime.now(timezone.utc)
        update: dict[str, Any] = {
            "$set": {
                "status": "awaiting_delivery",
                "failure_reason": reason,
                "updated_at": now,
            }
        }
        if next_retry_at:
            update["$set"]["next_retry_at"] = next_retry_at
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "status": {"$in": ["claimed", "executing"]}},
            update,
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def cancel_instance(self, instance_id: str, *, reason: str | None = None) -> None:
        now = datetime.now(timezone.utc)
        update: dict[str, object] = {
            "status": "cancelled",
            "completed_at": now,
            "updated_at": now,
        }
        if reason:
            update["failure_reason"] = reason
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id},
            {"$set": update},
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)

    async def acknowledge_instance(self, instance_id: str) -> bool:
        now = datetime.now(timezone.utc)
        doc = await mongodb.db.trigger_instances.find_one_and_update(
            {
                "id": instance_id,
                "status": {"$in": ["claimed", "executing", "delivered", "awaiting_delivery"]},
                "$or": [
                    {"attention_snapshot.requires_ack": True},
                    {"attention_snapshot.sound": {"$in": ["alarm", "timer"]}},
                ],
            },
            {
                "$set": {
                    "status": "acknowledged",
                    "acknowledged_at": now,
                    "completed_at": now,
                    "updated_at": now,
                }
            },
            return_document=True,
        )
        if doc is not None:
            await self.supersede_siblings_after_settlement(instance_id)
            await publish_activity_changed(str(doc["owner_id"]))
        return doc is not None

    async def snooze_instance(
        self,
        instance_id: str,
        *,
        snooze_until: datetime,
    ) -> TriggerInstance | None:
        """Mark current instance snoozed; create a new pending instance due at snooze_until."""
        now = datetime.now(timezone.utc)
        original = await mongodb.db.trigger_instances.find_one_and_update(
            {
                "id": instance_id,
                "status": {"$in": ["claimed", "executing", "delivered", "awaiting_delivery"]},
                "$or": [
                    {"attention_snapshot.requires_ack": True},
                    {"attention_snapshot.sound": {"$in": ["alarm", "timer"]}},
                ],
            },
            {"$set": {"status": "snoozed", "completed_at": now, "updated_at": now}},
            return_document=True,
        )
        if not original:
            return None
        await publish_activity_changed(str(original["owner_id"]))
        original.pop("_id", None)
        parent = TriggerInstance.model_validate(original)
        child = await self.create_instance(
            owner_id=parent.owner_id,
            origin=parent.origin_snapshot,
            action=parent.action_snapshot,
            attention=parent.attention_snapshot,
            delivery=parent.delivery_snapshot,
            rule_id=None,
            due_at=snooze_until,
            source_event={**parent.source_event, "snoozed_from": instance_id},
            freshness=parent.freshness_snapshot,
        )
        return child

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    async def get_awaiting_delivery(
        self,
        owner_id: str,
        *,
        retry_due_at: datetime | None = None,
        include_unscheduled: bool = True,
    ) -> list[TriggerInstance]:
        query: dict[str, Any] = {"owner_id": owner_id, "status": "awaiting_delivery"}
        if retry_due_at:
            retry_predicates: list[dict[str, Any]] = [
                {"next_retry_at": {"$lte": retry_due_at}},
            ]
            if include_unscheduled:
                retry_predicates.extend([
                    {"next_retry_at": {"$exists": False}},
                    {"next_retry_at": None},
                ])
            query["$or"] = retry_predicates
        cursor = mongodb.db.trigger_instances.find(query)
        docs = await cursor.to_list(None)
        return [TriggerInstance.model_validate({**d, "_id": None} if "_id" in d else d) for d in docs]

    async def dedupe_awaiting_for_retry(
        self,
        instances: list[TriggerInstance],
    ) -> list[TriggerInstance]:
        """Keep one-shot instances and only the latest awaiting row per recurring rule."""
        one_shots: list[TriggerInstance] = []
        by_rule: dict[str, list[TriggerInstance]] = {}
        for instance in instances:
            if not instance.rule_id:
                one_shots.append(instance)
                continue
            by_rule.setdefault(instance.rule_id, []).append(instance)

        selected = list(one_shots)
        for group in by_rule.values():
            latest = max(group, key=lambda inst: inst.due_at)
            selected.append(latest)
            for stale in group:
                if stale.id != latest.id:
                    await self.expire_instance(
                        stale.id,
                        reason="superseded_by_newer_occurrence",
                    )
        return selected

    async def claim_awaiting_instance(self, instance_id: str) -> bool:
        now = datetime.now(timezone.utc)
        result = await mongodb.db.trigger_instances.update_one(
            {"id": instance_id, "status": "awaiting_delivery"},
            {
                "$set": {
                    "status": "claimed",
                    "claimed_at": now,
                    "updated_at": now,
                },
                "$unset": {"next_retry_at": "", "failure_reason": ""},
            },
        )
        if result.modified_count:
            await self._publish_activity_changed_for_instance(instance_id)
        return bool(result.modified_count)

    async def get_instance(self, instance_id: str) -> TriggerInstance | None:
        doc = await mongodb.db.trigger_instances.find_one({"id": instance_id})
        if not doc:
            return None
        doc.pop("_id", None)
        return TriggerInstance.model_validate(doc)

    async def get_delivered_reply_grounding(
        self,
        *,
        owner_id: str,
        instance_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        """Return grounding only for instances authoritatively settled as delivered."""
        if not instance_ids:
            return {}
        cursor = mongodb.db.trigger_instances.find(
            {
                "owner_id": owner_id,
                "id": {"$in": list(dict.fromkeys(instance_ids))},
                "delivered_at": {"$exists": True, "$ne": None},
            },
            {
                "_id": 0,
                "id": 1,
                "action_snapshot.reply_grounding": 1,
            },
        )
        docs = await cursor.to_list(len(instance_ids))
        return {
            str(doc["id"]): grounding
            for doc in docs
            if isinstance(
                grounding := (doc.get("action_snapshot") or {}).get("reply_grounding"),
                dict,
            )
            and grounding
        }

    async def get_pending_due(self, now: datetime) -> list[TriggerInstance]:
        """Return pending instances whose due_at <= now."""
        cursor = mongodb.db.trigger_instances.find(
            {"status": "pending", "due_at": {"$lte": now}}
        )
        docs = await cursor.to_list(None)
        return [TriggerInstance.model_validate({**d, "_id": None} if "_id" in d else d) for d in docs]

    async def count_active(self, owner_id: str) -> int:
        return await mongodb.db.trigger_instances.count_documents(
            {"owner_id": owner_id, "status": {"$in": ["pending", "claimed", "executing", "awaiting_delivery"]}}
        )

    async def get_ackable_for_owner(self, owner_id: str) -> dict | None:
        """Return the most recent alarm/timer-like trigger instance that can be stopped."""
        doc = await mongodb.db.trigger_instances.find_one(
            {
                "owner_id": owner_id,
                "status": {"$in": ["claimed", "executing", "delivered", "awaiting_delivery"]},
                "$or": [
                    {"attention_snapshot.requires_ack": True},
                    {"attention_snapshot.sound": {"$in": ["alarm", "timer"]}},
                ],
            },
            sort=[("updated_at", -1), ("delivered_at", -1), ("claimed_at", -1), ("due_at", -1)],
        )
        if not doc:
            return None
        doc.pop("_id", None)
        return doc

    async def acknowledge_latest_for_owner(self, owner_id: str) -> dict | None:
        doc = await self.get_ackable_for_owner(owner_id)
        if not doc:
            return None
        return doc if await self.acknowledge_instance(doc["id"]) else None

    async def snooze_latest_for_owner(
        self, owner_id: str, *, duration: timedelta
    ) -> TriggerInstance | None:
        doc = await self.get_ackable_for_owner(owner_id)
        if not doc:
            return None
        return await self.snooze_instance(
            doc["id"],
            snooze_until=datetime.now(timezone.utc) + duration,
        )


trigger_service = TriggerService()
