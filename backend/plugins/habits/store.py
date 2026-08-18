"""Persistence helpers for the V0 habits plugin."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from pymongo.errors import DuplicateKeyError  # type: ignore[import-not-found]

from services.database.mongodb import mongodb

from .models import (
    Habit,
    HabitCheckinPlan,
    HabitCheckinSummary,
    HabitLog,
    HabitLogDetails,
    HabitLogSource,
    HabitLogStatus,
    HabitLogSummary,
    HabitSetup,
    HabitStatus,
)

HABITS_COLLECTION = "habits"
HABIT_LOGS_COLLECTION = "habit_logs"
HABIT_CHECKIN_PLANS_COLLECTION = "habit_checkin_plans"
MAX_STATUS_DAYS = 30
RECENT_LOG_LIMIT = 5


async def ensure_indexes() -> None:
    """Create lightweight indexes used by status and lookup queries."""
    await mongodb.db[HABITS_COLLECTION].create_index(
        [("owner_id", 1), ("id", 1)],
        unique=True,
    )
    await mongodb.db[HABITS_COLLECTION].create_index(
        [("owner_id", 1), ("name_key", 1)],
        unique=True,
    )
    await mongodb.db[HABITS_COLLECTION].create_index([("owner_id", 1), ("active", 1)])
    await mongodb.db[HABIT_LOGS_COLLECTION].create_index(
        [("owner_id", 1), ("habit_id", 1), ("logged_at", -1)]
    )
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].create_index(
        [("owner_id", 1), ("id", 1)],
        unique=True,
    )
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].create_index(
        [("owner_id", 1), ("habit_id", 1), ("active", 1)]
    )
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].create_index(
        [("owner_id", 1), ("rule_id", 1)]
    )


async def create_habit(
    *,
    owner_id: str,
    name: str,
    behavior: str,
    cue: str | None = None,
    minimum_version: str | None = None,
    desired_frequency: str | None = None,
) -> Habit:
    name = name.strip()
    behavior = behavior.strip()
    if await get_habit_by_name(owner_id, name):
        raise ValueError("Habit already exists")

    habit = Habit(
        owner_id=owner_id,
        name=name,
        name_key=normalize_name(name),
        behavior=behavior,
        cue=_clean_optional(cue),
        minimum_version=_clean_optional(minimum_version),
        desired_frequency=_clean_optional(desired_frequency),
    )
    try:
        await mongodb.db[HABITS_COLLECTION].insert_one(habit.model_dump(mode="python"))
    except DuplicateKeyError as exc:
        raise ValueError("Habit already exists") from exc
    return habit


async def get_habit(owner_id: str, habit_id: str) -> Habit | None:
    doc = await mongodb.db[HABITS_COLLECTION].find_one(
        {"owner_id": owner_id, "id": habit_id, "active": True}
    )
    if not doc:
        return None
    doc.pop("_id", None)
    if "name_key" not in doc:
        doc["name_key"] = normalize_name(doc["name"])
    return Habit.model_validate(doc)


async def get_habit_by_name(owner_id: str, name: str) -> Habit | None:
    doc = await mongodb.db[HABITS_COLLECTION].find_one(
        {"owner_id": owner_id, "name_key": normalize_name(name), "active": True}
    )
    if not doc:
        doc = await mongodb.db[HABITS_COLLECTION].find_one(
            {"owner_id": owner_id, "name": name.strip(), "active": True}
        )
    if not doc:
        return None
    doc.pop("_id", None)
    if "name_key" not in doc:
        doc["name_key"] = normalize_name(doc["name"])
    return Habit.model_validate(doc)


async def resolve_habit(owner_id: str, habit_id_or_name: str) -> Habit | None:
    return await get_habit(owner_id, habit_id_or_name) or await get_habit_by_name(
        owner_id,
        habit_id_or_name,
    )


async def list_active_habits(owner_id: str) -> list[Habit]:
    cursor = mongodb.db[HABITS_COLLECTION].find({"owner_id": owner_id, "active": True})
    docs = await cursor.to_list(None)
    return [_habit_from_doc(doc) for doc in docs]


async def log_habit(
    *,
    owner_id: str,
    habit_id: str,
    status: HabitLogStatus,
    note: str | None = None,
    details: HabitLogDetails | None = None,
    source: HabitLogSource = "voice",
    logged_at: datetime | None = None,
) -> tuple[Habit, HabitLog]:
    habit = await get_habit(owner_id, habit_id)
    if habit is None:
        raise ValueError("Habit not found")

    log = HabitLog(
        owner_id=owner_id,
        habit_id=habit_id,
        status=status,
        note=_clean_optional(note),
        details=details,
        source=source,
        logged_at=logged_at or datetime.now(timezone.utc),
    )
    await mongodb.db[HABIT_LOGS_COLLECTION].insert_one(log.model_dump(mode="python"))
    return habit, log


async def log_habit_by_name(
    *,
    owner_id: str,
    name: str,
    status: HabitLogStatus,
    note: str | None = None,
    details: HabitLogDetails | None = None,
    source: HabitLogSource = "voice",
    logged_at: datetime | None = None,
) -> tuple[Habit, HabitLog]:
    habit = await get_habit_by_name(owner_id, name)
    if habit is None:
        raise ValueError("Habit not found")
    return await log_habit(
        owner_id=owner_id,
        habit_id=habit.id,
        status=status,
        note=note,
        details=details,
        source=source,
        logged_at=logged_at,
    )


async def get_habit_statuses(
    *,
    owner_id: str,
    habit_id: str | None = None,
    days: int = 7,
) -> list[HabitStatus]:
    days = max(1, min(days, MAX_STATUS_DAYS))
    habits = [await resolve_habit(owner_id, habit_id)] if habit_id else await list_active_habits(owner_id)
    habits = [habit for habit in habits if habit is not None]
    if not habits:
        return []

    habit_ids = [habit.id for habit in habits]
    since = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = mongodb.db[HABIT_LOGS_COLLECTION].find(
        {
            "owner_id": owner_id,
            "habit_id": {"$in": habit_ids},
            "logged_at": {"$gte": since},
        }
    ).sort("logged_at", -1)
    logs = [_log_from_doc(doc) for doc in await cursor.to_list(None)]

    logs_by_habit: dict[str, list[HabitLog]] = defaultdict(list)
    for log in logs:
        logs_by_habit[log.habit_id].append(log)

    return [_status_for_habit(habit, logs_by_habit.get(habit.id, []), days) for habit in habits]


async def get_habit_setup(
    *,
    owner_id: str,
    habit_id: str,
    days: int = 7,
) -> HabitSetup | None:
    habit = await resolve_habit(owner_id, habit_id)
    if habit is None:
        return None
    statuses = await get_habit_statuses(owner_id=owner_id, habit_id=habit.id, days=days)
    recent_status = statuses[0] if statuses else _status_for_habit(habit, [], max(1, min(days, MAX_STATUS_DAYS)))
    return HabitSetup(
        habit=habit,
        recent_status=recent_status,
        checkins=await list_habit_checkins(owner_id=owner_id, habit_id=habit.id),
    )


async def list_owner_checkin_plans(owner_id: str) -> list[HabitCheckinPlan]:
    cursor = mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].find({"owner_id": owner_id})
    return [_plan_from_doc(doc) for doc in await cursor.to_list(None)]


async def get_checkin_plan(owner_id: str, plan_id: str) -> HabitCheckinPlan | None:
    doc = await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].find_one(
        {"owner_id": owner_id, "id": plan_id},
    )
    if not doc:
        return None
    return _plan_from_doc(doc)


async def replace_checkin_plan(
    plan: HabitCheckinPlan,
    *,
    habit: Habit,
) -> HabitCheckinPlan:
    """Persist a validated plan edit and update its linked trigger artifacts."""
    from core.scheduling import coerce_timezone, parse_schedule_time
    from core.triggers.models import TriggerOrigin
    from core.triggers.presets import reminder_preset
    from .triggers import checkin_reply_grounding

    existing = await get_checkin_plan(plan.owner_id, plan.id)
    if existing is None:
        raise ValueError("Check-in plan not found")
    if bool(existing.recurrence) != bool(plan.recurrence):
        raise ValueError("Changing between one-shot and recurring check-ins is not supported")

    trigger_time = parse_schedule_time(plan.when, timezone_name=plan.timezone)
    kwargs = reminder_preset(
        owner_id=plan.owner_id,
        message=plan.message,
        fire_at=trigger_time,
        recurrence=plan.recurrence,
        timezone_name=plan.timezone,
        importance="normal",
        instructions=plan.instructions,
        decision=plan.decision,
        reply_grounding=checkin_reply_grounding(
            habit,
            checkin_kind=plan.checkin_kind,
        ),
    )
    origin = TriggerOrigin(
        kind="time",
        fire_at=trigger_time,
        recurrence=plan.recurrence,
        timezone=plan.timezone,
        original_local_time=trigger_time.astimezone(
            coerce_timezone(plan.timezone)
        ).strftime("%H:%M") if plan.recurrence else None,
    )
    now = datetime.now(timezone.utc)
    plan.updated_at = now

    if plan.rule_id:
        rule = await mongodb.db.trigger_rules.find_one(
            {"owner_id": plan.owner_id, "id": plan.rule_id}
        )
        if not rule:
            raise ValueError("Linked recurring trigger rule not found")
        await mongodb.db.trigger_rules.update_one(
            {"owner_id": plan.owner_id, "id": plan.rule_id},
            {"$set": {
                "origin": origin.model_dump(mode="python", exclude_none=True),
                "action": kwargs["action"].model_dump(mode="python", exclude_none=True),
                "updated_at": now,
            }},
        )
        await mongodb.db.trigger_instances.update_many(
            {
                "owner_id": plan.owner_id,
                "rule_id": plan.rule_id,
                "status": {"$in": ["pending", "awaiting_delivery"]},
            },
            {"$set": {
                "due_at": trigger_time,
                "origin_snapshot": origin.model_dump(mode="python", exclude_none=True),
                "action_snapshot": kwargs["action"].model_dump(mode="python", exclude_none=True),
                "updated_at": now,
            }},
        )
    elif plan.initial_instance_id:
        instance = await mongodb.db.trigger_instances.find_one(
            {"owner_id": plan.owner_id, "id": plan.initial_instance_id}
        )
        if not instance:
            raise ValueError("Linked trigger instance not found")
        if instance.get("status") not in ("pending", "awaiting_delivery"):
            raise ValueError("Habit check-in occurrence is no longer pending")
        await mongodb.db.trigger_instances.update_one(
            {"owner_id": plan.owner_id, "id": plan.initial_instance_id},
            {"$set": {
                "due_at": trigger_time,
                "origin_snapshot": origin.model_dump(mode="python", exclude_none=True),
                "action_snapshot": kwargs["action"].model_dump(mode="python", exclude_none=True),
                "updated_at": now,
            }},
        )
    else:
        raise ValueError("Check-in plan has no linked trigger")

    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].update_one(
        {"owner_id": plan.owner_id, "id": plan.id},
        {"$set": plan.model_dump(mode="python", exclude={"id", "owner_id", "created_at"})},
    )
    return plan


async def pause_checkin_plan(
    owner_id: str,
    plan_id: str,
    *,
    until: datetime | None = None,
) -> HabitCheckinPlan:
    from core.triggers.lifecycle import cancel_open_instances_for_rule, materialize_after_pause
    from core.triggers.models import TriggerRule

    plan = await get_checkin_plan(owner_id, plan_id)
    if plan is None:
        raise ValueError("Check-in plan not found")
    now = datetime.now(timezone.utc)
    if plan.rule_id:
        update: dict[str, object] = {
            "enabled": until is not None,
            "paused_until": until,
            "updated_at": now,
        }
        await mongodb.db.trigger_rules.update_one(
            {"owner_id": owner_id, "id": plan.rule_id},
            {"$set": update},
        )
        await cancel_open_instances_for_rule(
            owner_id,
            plan.rule_id,
            reason="parent_rule_paused_or_disabled",
        )
        if until is not None:
            rule_doc = await mongodb.db.trigger_rules.find_one(
                {"owner_id": owner_id, "id": plan.rule_id},
                {"_id": 0},
            )
            if rule_doc:
                await materialize_after_pause(
                    TriggerRule.model_validate(rule_doc),
                    until,
                )
    plan.active = until is not None
    plan.paused_until = until
    plan.updated_at = now
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].update_one(
        {"owner_id": owner_id, "id": plan.id},
        {"$set": {
            "active": plan.active,
            "paused_until": until,
            "updated_at": now,
        }},
    )
    return plan


async def resume_checkin_plan(owner_id: str, plan_id: str) -> HabitCheckinPlan:
    from core.triggers.service import trigger_service

    plan = await get_checkin_plan(owner_id, plan_id)
    if plan is None:
        raise ValueError("Check-in plan not found")
    now = datetime.now(timezone.utc)
    if plan.rule_id:
        await mongodb.db.trigger_rules.update_one(
            {"owner_id": owner_id, "id": plan.rule_id},
            {"$set": {"enabled": True, "paused_until": None, "updated_at": now}},
        )
    plan.active = True
    plan.paused_until = None
    plan.updated_at = now
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].update_one(
        {"owner_id": owner_id, "id": plan.id},
        {"$set": {"active": True, "paused_until": None, "updated_at": now}},
    )
    if plan.rule_id and plan.recurrence:
        rule_doc = await mongodb.db.trigger_rules.find_one({"id": plan.rule_id})
        if rule_doc:
            from core.scheduling import next_occurrence, recurrence_rule_from_origin, parse_schedule_time
            from core.triggers.models import (
                AttentionPolicy,
                DeliveryPlan,
                FreshnessPolicy,
                ManagementOwnership,
                TriggerAction,
                TriggerOrigin,
            )

            trigger_time = parse_schedule_time(plan.when, timezone_name=plan.timezone)
            next_time = next_occurrence(
                recurrence_rule_from_origin(
                    rule_doc.get("origin", {}),
                    rule_doc=rule_doc,
                    owner_id=owner_id,
                    rule_id=plan.rule_id,
                ),
                now,
            ) or trigger_time
            await trigger_service.materialize_recurring_occurrence(
                owner_id=owner_id,
                rule_id=plan.rule_id,
                origin=TriggerOrigin.model_validate(rule_doc["origin"]),
                action=TriggerAction.model_validate(rule_doc["action"]),
                attention=AttentionPolicy.model_validate(rule_doc["attention"]),
                delivery=DeliveryPlan.model_validate(rule_doc["delivery"]),
                freshness=FreshnessPolicy.model_validate(rule_doc["freshness"]),
                due_at=next_time,
                management=ManagementOwnership.model_validate(rule_doc["management"]),
            )
    return plan


async def delete_checkin_plan(owner_id: str, plan_id: str) -> None:
    from core.triggers.lifecycle import cancel_open_instances_for_rule

    plan = await get_checkin_plan(owner_id, plan_id)
    if plan is None:
        raise ValueError("Check-in plan not found")
    if plan.rule_id:
        await cancel_open_instances_for_rule(owner_id, plan.rule_id, reason="user_deleted")
        await mongodb.db.trigger_rules.delete_one({"owner_id": owner_id, "id": plan.rule_id})
    if plan.initial_instance_id:
        await mongodb.db.trigger_instances.update_one(
            {"owner_id": owner_id, "id": plan.initial_instance_id},
            {
                "$set": {
                    "status": "cancelled",
                    "completed_at": datetime.now(timezone.utc),
                    "updated_at": datetime.now(timezone.utc),
                }
            },
        )
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].delete_one(
        {"owner_id": owner_id, "id": plan.id},
    )


async def save_habit_checkin_plan(plan: HabitCheckinPlan) -> HabitCheckinPlan:
    await mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].insert_one(
        plan.model_dump(mode="python")
    )
    return plan


async def list_habit_checkins(*, owner_id: str, habit_id: str) -> list[HabitCheckinSummary]:
    plans_cursor = mongodb.db[HABIT_CHECKIN_PLANS_COLLECTION].find(
        {"owner_id": owner_id, "habit_id": habit_id, "active": True}
    ).sort("updated_at", -1)
    plans = [_plan_from_doc(doc) for doc in await plans_cursor.to_list(None)]
    if not plans:
        return []

    rule_ids = [plan.rule_id for plan in plans if plan.rule_id]
    rules_by_id: dict[str, dict] = {}
    if rule_ids:
        rules_cursor = mongodb.db.trigger_rules.find(
            {"owner_id": owner_id, "id": {"$in": rule_ids}}
        )
        rules_by_id = {
            str(rule["id"]): rule for rule in await rules_cursor.to_list(None)
        }

    plan_ids = {plan.id for plan in plans}
    initial_instance_ids = {
        plan.initial_instance_id for plan in plans if plan.initial_instance_id
    }
    instances_cursor = mongodb.db.trigger_instances.find({"owner_id": owner_id}).sort(
        "due_at", -1
    )
    instances = [
        instance
        for instance in await instances_cursor.to_list(None)
        if instance.get("rule_id") in rule_ids
        or instance.get("id") in initial_instance_ids
        or _managed_plan_id(instance) in plan_ids
    ]

    instances_by_rule: dict[str, list[dict]] = defaultdict(list)
    instances_by_plan: dict[str, list[dict]] = defaultdict(list)
    for instance in instances:
        if instance.get("rule_id"):
            instances_by_rule[str(instance["rule_id"])].append(instance)
        plan_id = _managed_plan_id(instance)
        if plan_id:
            instances_by_plan[plan_id].append(instance)

    return [
        _checkin_from_plan(
            plan,
            rules_by_id.get(plan.rule_id or ""),
            instances_by_rule.get(plan.rule_id or "", [])
            + instances_by_plan.get(plan.id, []),
        )
        for plan in plans
    ]


def _plan_from_doc(doc: dict) -> HabitCheckinPlan:
    doc = dict(doc)
    doc.pop("_id", None)
    return HabitCheckinPlan.model_validate(doc)


def _habit_from_doc(doc: dict) -> Habit:
    doc = dict(doc)
    doc.pop("_id", None)
    if "name_key" not in doc:
        doc["name_key"] = normalize_name(doc["name"])
    return Habit.model_validate(doc)


def _log_from_doc(doc: dict) -> HabitLog:
    doc = dict(doc)
    doc.pop("_id", None)
    return HabitLog.model_validate(doc)


def _checkin_from_plan(
    plan: HabitCheckinPlan,
    rule: dict | None,
    instances: list[dict],
) -> HabitCheckinSummary:
    origin = rule.get("origin", {}) if rule and isinstance(rule.get("origin"), dict) else {}
    next_instance = _next_pending_instance(instances)
    latest_instance = instances[0] if instances else None
    is_enabled = not rule or rule.get("enabled", True)
    return HabitCheckinSummary(
        id=plan.id,
        scope="plan",
        checkin_kind=plan.checkin_kind,
        message=plan.message,
        status=str(
            next_instance.get("status")
            if next_instance
            else ("active" if plan.active and is_enabled else "disabled")
        ),
        plan_id=plan.id,
        rule_id=plan.rule_id,
        instance_id=(next_instance or latest_instance or {}).get("id"),
        recurrence=plan.recurrence or origin.get("recurrence"),
        decision=plan.decision,
        next_due_at=next_instance.get("due_at") if next_instance else None,
        last_due_at=latest_instance.get("due_at") if latest_instance else None,
    )


def _managed_plan_id(instance: dict) -> str | None:
    management = instance.get("management")
    if not isinstance(management, dict) or management.get("provider") != "habits":
        return None
    value = management.get("resource_id")
    return str(value) if value else None


def _next_pending_instance(instances: list[dict]) -> dict | None:
    pending = [
        instance
        for instance in instances
        if instance.get("status") in {"pending", "awaiting_delivery"}
    ]
    if not pending:
        return None
    return min(pending, key=lambda instance: instance.get("due_at") or datetime.max.replace(tzinfo=timezone.utc))


def _status_for_habit(habit: Habit, logs: list[HabitLog], days: int) -> HabitStatus:
    counts = Counter(log.status for log in logs)
    recent = logs[:RECENT_LOG_LIMIT]
    last = recent[0] if recent else None
    return HabitStatus(
        habit_id=habit.id,
        name=habit.name,
        behavior=habit.behavior,
        cue=habit.cue,
        minimum_version=habit.minimum_version,
        desired_frequency=habit.desired_frequency,
        days=days,
        done=counts["done"],
        missed=counts["missed"],
        skipped=counts["skipped"],
        total=len(logs),
        last_status=last.status if last else None,
        last_logged_at=last.logged_at if last else None,
        recent_logs=[
            HabitLogSummary(
                status=log.status,
                note=log.note,
                details=log.details,
                logged_at=log.logged_at,
            )
            for log in recent
        ],
        suggested_adjustment=_suggest_adjustment(habit, logs),
    )


def _suggest_adjustment(habit: Habit, logs: list[HabitLog]) -> str | None:
    if len(logs) < 3:
        return None
    recent = logs[:7]
    missed = sum(1 for log in recent if log.status == "missed")
    done = sum(1 for log in recent if log.status == "done")
    if missed >= 3 and missed > done:
        if habit.minimum_version:
            return "Misses are clustering. Consider shrinking the minimum version or moving the cue."
        return "Misses are clustering. Consider adding a minimum version or moving the cue."
    return None


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def normalize_name(value: str) -> str:
    return " ".join(value.strip().casefold().split())
