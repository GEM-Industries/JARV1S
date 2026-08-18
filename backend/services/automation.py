"""
AutomationService — ECA (Event-Condition-Action) background engine.

Polls registered Watchers, evaluates automation rules, and creates
TriggerInstance documents via trigger_service. TRIGGER_DUE
events drive delivery through the orchestrator.

Design decisions:
- Automations are timely *observations*, not buffered commitments. If the user
  is offline when an automation fires, the instance is marked awaiting_delivery.
- `_pending` tracks scheduled call_later timers keyed by (rule_id, item_id). Each
  tick reconciles pending timers against fresh watcher data: cancels timers for moved
  or deleted events, reschedules if the fire time changed, and adds timers for newly
  discovered events. PendingFire may keep a schedule-time rule snapshot for prefetch,
  but `_fire` reloads the live rule from Mongo so `update_rule` action/instruction
  edits apply even when fire_time was unchanged.
- `_fired` is dispatch-only dedup: written when an automation actually fires, not
  when a timer is scheduled. Persisted to MongoDB so dedup survives restarts.
- MAX_LATENESS is a semantic staleness guard. Guards against: (a) new rules created
  for events already in progress, (b) DB unavailable at startup leaving _fired empty.
"""

import asyncio
import importlib
import inspect
import logging
import pkgutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pymongo.errors import DuplicateKeyError  # type: ignore[import-not-found]
from pydantic import BaseModel

from core.context import ensure_aware
from core.triggers.conditions import evaluate_conditions as evaluate_trigger_conditions
from core.triggers.models import TriggerAction, TriggerOrigin, TriggerRule
from services.database.mongodb import mongodb
from services.events import event_bus, Event, EventType
from services.health import IntegrationHealth
from services.watchers import Watcher

logger = logging.getLogger(__name__)

MAX_LATENESS = timedelta(minutes=10)
POLL_INTERVAL = 60  # seconds
FIRED_TTL = timedelta(hours=24)
PRUNE_INTERVAL = timedelta(hours=1)


@dataclass
class PendingFire:
    """A call_later timer waiting to fire for a specific (rule, item) pair.

    Carries a schedule-time rule/item snapshot for prefetch and timer bookkeeping.
    Dispatch must not trust this snapshot for action/instructions — `_fire`
    reloads the live rule from Mongo.
    """
    fire_time: datetime
    handle: asyncio.TimerHandle
    rule_id: str
    item_id: str
    rule: TriggerRule
    item: dict


class TriggerEvent(BaseModel):
    """Canonical event shape for the push path.

    Inbound Composio webhook payloads and reactive push adapters normalize into
    this before calling on_push_event(). The poll path operates on raw item dicts
    directly for efficiency.

    raw_event_type preserves the original provider slug before normalization —
    essential for debugging when a rule doesn't fire as expected.
    """
    source: str           # "calendar", "slack", "github"
    event_type: str       # "event_starting", "new_message", "commit"
    event_id: str         # stable ID for dedup (provider event ID or hash)
    occurred_at: datetime # when the event happened (or will happen)
    provider: str         # "google", "composio", "polling"
    payload: dict         # full event data — conditions evaluate against this
    raw_event_type: Optional[str] = None  # original provider slug before normalization


def evaluate_conditions(conditions: list, item: dict) -> bool:
    """
    Evaluate a list of AND conditions against an item dict.
    Short-circuits on first failure. Case-insensitive string ops.
    Numeric ops (greater_than, less_than) parse both sides as floats.
    """
    return evaluate_trigger_conditions(conditions, item)


def render_template(template: str, item: dict) -> str:
    """Resolve {field} placeholders from item. Missing fields → empty string."""
    return template.format_map(defaultdict(str, item))


def render_automation_message(rule: TriggerRule, item: dict) -> str:
    """Render the user-facing automation message with minimal event context."""
    template = rule.action.message or "Automation triggered."
    message = render_template(template, item)

    # Calendar watchers can legitimately fire multiple events at the same time.
    # If the user configured a generic message, append the event title so the
    # spoken notification is distinguishable without changing template syntax.
    title = item.get("title")
    if rule.origin.source == "calendar" and title and "{title}" not in template:
        message = f"{message}: {title}"
    return message


def compute_fire_time(rule: TriggerRule, item: dict) -> Optional[datetime]:
    """
    Compute the UTC datetime when this rule should fire for this item.
    Returns None if the fire time cannot be determined (e.g. all-day events).
    """
    if item.get("is_all_day"):
        # "5 minutes before" an all-day event is meaningless.
        # Phase 1: all-day events are skipped entirely.
        return None

    start_str = item.get("start", "")
    if not start_str:
        return None

    try:
        # Calendar providers emit tz-aware ISO strings; naive fallback to UTC.
        # DST is handled by the provider's offset — no manual calculation needed.
        start_dt = ensure_aware(start_str, timezone.utc)
        offset_minutes = rule.origin.offset_minutes
        return (start_dt + timedelta(minutes=offset_minutes)).astimezone(timezone.utc)
    except (ValueError, TypeError) as e:
        logger.warning("Could not parse event start '%s': %s", start_str, e)
        return None


def item_semantic_key(source: str, item: dict) -> tuple:
    """Best-effort identity for duplicate source items with different provider IDs."""
    if source == "calendar":
        return (
            source,
            str(item.get("title") or "").strip().lower(),
            item.get("start") or "",
            item.get("end") or "",
            item.get("account") or "",
        )
    return (source, item.get("id") or "")


class AutomationService:
    """
    Background service that evaluates all automation rules across all registered
    watchers and creates TriggerInstance documents when fire times arrive.
    """

    def __init__(self, poll_interval: int = POLL_INTERVAL):
        self.poll_interval = poll_interval
        self.running = False
        self._task: Optional[asyncio.Task] = None

        self._watchers: dict[str, Watcher] = {}
        # Scheduled timers: (rule_id, item_id) → PendingFire.
        # In-memory only — rebuilt from fresh watcher data on each tick and on restart.
        self._pending: dict[tuple[str, str], PendingFire] = {}
        # Dispatch dedup: (rule_id, item_id) → timestamp when automation fired.
        # Written only at actual dispatch time. Persisted to MongoDB so dedup
        # survives restarts. MongoDB TTL index auto-expires entries after 24h.
        self._fired: dict[tuple[str, str], datetime] = {}
        # Strong references to fire-and-forget tasks — prevents GC and enables
        # exception logging via done callbacks.
        self._background_tasks: set[asyncio.Task] = set()
        self._paused_until: Optional[datetime] = None
        self._health = IntegrationHealth(owner="automation")
        self._last_prune: datetime = datetime.min.replace(tzinfo=timezone.utc)

    # --- Lifecycle ---

    async def start(self) -> None:
        if self.running:
            return
        self._discover_watchers()
        await self._load_fired_from_db()
        await self._load_pause_state()
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("AutomationService started with watchers: %s", list(self._watchers))

    async def _load_fired_from_db(self) -> None:
        """Restore _fired from the persisted dedup log on startup."""
        try:
            async for doc in mongodb.db.automation_fired.find({}):
                key = (doc["rule_id"], doc["item_id"])
                # BSON datetimes are decoded as UTC-aware (CodecOptions tz_aware=True).
                self._fired[key] = doc["fired_at"]
            logger.info("Loaded %d fired entries from DB", len(self._fired))
        except Exception as e:
            logger.warning("Could not load fired entries from DB: %s", e)

    async def _load_pause_state(self) -> None:
        """Restore _paused_until from MongoDB so global pauses survive restarts."""
        try:
            doc = await mongodb.db.automation_config.find_one({"_id": "global"})
            if doc and doc.get("paused_until"):
                paused = doc["paused_until"]
                if isinstance(paused, datetime):
                    if paused > datetime.now(timezone.utc):
                        self._paused_until = paused
                        logger.info("Restored global pause until %s", paused.isoformat())
        except Exception as e:
            logger.warning("Could not load pause state: %s", e)

    async def _persist_pause_state(self) -> None:
        """Persist _paused_until to MongoDB so global pauses survive restarts."""
        try:
            await mongodb.db.automation_config.update_one(
                {"_id": "global"},
                {"$set": {"paused_until": self._paused_until}},
                upsert=True,
            )
        except Exception as e:
            logger.warning("Could not persist pause state: %s", e)

    def _discover_watchers(self) -> None:
        """
        Auto-discover all Watcher implementations in services/watchers/.
        Mirrors the plugin registry pattern: scan the package, find Protocol
        implementors, instantiate and register each one.
        Adding a new watcher requires only dropping a new file in services/watchers/ —
        no changes to main.py or this service.
        """
        watchers_pkg = Path(__file__).parent / "watchers"
        for _, module_name, _ in pkgutil.iter_modules([str(watchers_pkg)]):
            try:
                module = importlib.import_module(f"services.watchers.{module_name}")
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if obj is Watcher:
                        continue
                    # Only register classes defined in this module — not imports.
                    if obj.__module__ != f"services.watchers.{module_name}":
                        continue
                    # Check structural compatibility: has `source` attr and `poll` method.
                    # Avoids instantiating unknown classes just to do an isinstance check.
                    if hasattr(obj, "source") and hasattr(obj, "poll") and callable(getattr(obj, "poll", None)):
                        instance = obj()
                        self._watchers[instance.source] = instance
                        logger.info("Registered watcher: %s", instance.source)
            except Exception as e:
                logger.error("Failed to load watcher module '%s': %s", module_name, e)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Cancel all pending timers and in-flight fire tasks
        for pf in self._pending.values():
            pf.handle.cancel()
        self._pending.clear()
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()
        logger.info("AutomationService stopped")

    # --- Public methods (used by the automations plugin) ---

    def watcher_trigger_info(self, source: str) -> list[dict]:
        """Return trigger_events declared by the watcher for source, or [].

        Used by list_available_triggers to surface built-in watcher capabilities
        alongside (or instead of) Composio triggers.
        """
        watcher = self._watchers.get(source)
        if watcher is None:
            return []
        return list(getattr(watcher, "trigger_events", []))

    def watcher_condition_fields(self, source: str) -> list[dict]:
        """Return condition_fields declared by the watcher for source, or []."""
        watcher = self._watchers.get(source)
        if watcher is None:
            return []
        return list(getattr(watcher, "condition_fields", []))

    def watcher_sources(self) -> set[str]:
        """Return registered external watcher source names."""
        return set(self._watchers)

    async def pause(self, until: Optional[datetime] = None) -> None:
        """
        Pause all automations. Cancels all pending timers immediately.
        The next tick after resume() will re-discover and re-schedule them.
        """
        self._paused_until = until or datetime.max.replace(tzinfo=timezone.utc)
        for pf in self._pending.values():
            pf.handle.cancel()
        self._pending.clear()
        await self._persist_pause_state()

    async def resume(self) -> None:
        """Resume all automations."""
        self._paused_until = None
        await self._persist_pause_state()

    def is_paused(self, now: datetime) -> bool:
        """True if the global automation pause is active at `now`."""
        return self._paused_until is not None and now < self._paused_until

    def iter_upcoming_protocol_fires(
        self, now: datetime, window_end: datetime
    ) -> list[dict]:
        """Snapshot pending fires with linked protocols scheduled within [now, window_end].

        Consumed by PrefetchService to pre-render protocol briefings. Returns
        plain dicts (not PendingFire) so callers never touch TimerHandle state.
        Snapshot iteration avoids mutation-during-iteration if a timer resolves
        while we scan.
        """
        candidates: list[dict] = []
        for _, pf in list(self._pending.items()):
            if pf.fire_time < now or pf.fire_time > window_end:
                continue
            protocol = pf.rule.action.protocol_name
            if not protocol:
                continue
            candidates.append({
                "source": "automation",
                "rule_id": pf.rule_id,
                "item_id": pf.item_id,
                "owner_id": pf.rule.owner_id,
                "protocol_name": protocol,
                "fire_time": pf.fire_time,
                "rule": pf.rule.model_dump(mode="python"),
                "item": pf.item,
            })
        return candidates

    async def test_rule(self, rule_id: str) -> list[dict] | None:
        """
        Dry-run a rule against current watcher state.
        Returns None if the source has no watcher (push-backed — cannot dry-run via polling).
        Returns [] if polled but no items matched.
        """
        rule_doc = await mongodb.db.trigger_rules.find_one({
            "id": rule_id,
            "origin.kind": "external",
        })
        if not rule_doc:
            return []
        rule = TriggerRule.model_validate(rule_doc)

        source = rule.origin.source
        watcher = self._watchers.get(source)
        if not watcher:
            return None

        items = await watcher.poll()
        now = datetime.now(timezone.utc)
        is_reactive = getattr(watcher, "trigger_mode", "anticipated") == "reactive"
        would_fire = []

        for item in items:
            item_id = item.get("id", "")
            key = (rule_id, item_id)
            if item_id in rule.suppressed_event_ids:
                continue
            if not evaluate_conditions(rule.conditions, item):
                continue

            if is_reactive:
                would_fire.append({
                    "item_id": item_id,
                    "title": item.get("title") or item.get("subject"),
                    "fire_time": now.isoformat(),
                    "seconds_until_fire": 0,
                    "already_fired": key in self._fired,
                })
                continue

            fire_time = compute_fire_time(rule, item)
            if fire_time is None:
                continue
            would_fire.append({
                "item_id": item_id,
                "title": item.get("title"),
                "fire_time": fire_time.isoformat(),
                "seconds_until_fire": max(0, (fire_time - now).total_seconds()),
                "already_fired": key in self._fired,
            })

        return would_fire

    async def on_push_event(self, event: TriggerEvent) -> None:
        """Process a push-delivered trigger event immediately.

        Unlike the poll path, push events are happening now — no fire-time
        computation or call_later scheduling. Reuses evaluate_conditions,
        _mark_fired, and _fire so dedup and dispatch are identical.
        """
        now = datetime.now(timezone.utc)

        if self._paused_until and now < self._paused_until:
            return

        async for rule_doc in mongodb.db.trigger_rules.find({
            "enabled": True,
            "origin.kind": "external",
            "origin.source": event.source,
        }):
            rule = TriggerRule.model_validate(rule_doc)
            rule_id = rule.id
            if not rule_id:
                continue

            # Match event type if the rule specifies one (empty = match any)
            trigger_event_type = rule.origin.event or ""
            if trigger_event_type and trigger_event_type != event.event_type:
                continue

            # Per-rule pause check
            paused_until_raw = rule.paused_until
            if paused_until_raw:
                try:
                    if now < ensure_aware(paused_until_raw, timezone.utc):
                        continue
                except (ValueError, TypeError):
                    logger.warning(
                        "Invalid paused_until for rule %s: %r", rule_id, paused_until_raw
                    )

            key = (rule_id, event.event_id)

            if event.event_id in rule.suppressed_event_ids:
                continue
            if key in self._fired:
                continue
            if not evaluate_conditions(rule.conditions, event.payload):
                continue

            # Immediate fire — claim before any user-visible side effect.
            if not await self._claim_fire(key, now):
                continue

            self._create_fire_task(key, rule, event.payload)
            logger.debug(
                "Push event dispatched: rule=%s source=%s event_type=%s event_id=%s",
                rule_id, event.source, event.event_type, event.event_id,
            )

    # --- Internal loop ---

    async def _poll_loop(self) -> None:
        while self.running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("Error in AutomationService tick: %s", e)
            await asyncio.sleep(self.poll_interval)

    async def kick_source(self, source: str) -> None:
        """Out-of-cycle evaluation for a single source, triggered by a push notification.

        Loads enabled rules for the source, polls the watcher, and evaluates via
        _evaluate_source. Runs scoped orphan cleanup for this source only so it
        doesn't interfere with pending timers from other sources.
        Called by PushRegistry when an anticipated adapter (e.g. Calendar Watch)
        signals that the source state has changed.
        """
        if self._paused_until and datetime.now(timezone.utc) < self._paused_until:
            return

        watcher = self._watchers.get(source)
        if not watcher:
            return

        rules: list[TriggerRule] = []
        async for rule_doc in mongodb.db.trigger_rules.find({
            "enabled": True,
            "origin.kind": "external",
            "origin.source": source,
        }):
            rules.append(TriggerRule.model_validate(rule_doc))
        if not rules:
            return

        now = datetime.now(timezone.utc)
        try:
            items = await watcher.poll()
            self._health.record_success(source)
        except Exception as e:
            await self._health.record_failure(source, e)
            return

        active_keys, evaluated_rule_ids, active_item_ids = await self._evaluate_source(
            source, watcher, items, rules, now
        )

        # Scoped orphan cleanup — only for rules belonging to this source
        for key in list(self._pending):
            if key[0] in evaluated_rule_ids and key not in active_keys:
                self._pending[key].handle.cancel()
                del self._pending[key]
                logger.debug("kick_source: cancelled orphaned timer for rule=%s item=%s", key[0], key[1])

        logger.debug("kick_source('%s'): evaluated %d rules against %d items", source, len(rules), len(items))

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)

        # Prune fired entries older than 24h
        expired = [k for k, ts in self._fired.items() if now - ts > FIRED_TTL]
        for k in expired:
            del self._fired[k]

        # Global pause check
        if self._paused_until and now < self._paused_until:
            return

        # Load all enabled rules grouped by source
        rules_by_source: dict[str, list[TriggerRule]] = defaultdict(list)
        async for rule_doc in mongodb.db.trigger_rules.find({
            "enabled": True,
            "origin.kind": "external",
        }):
            rule = TriggerRule.model_validate(rule_doc)
            source = rule.origin.source
            if source:
                rules_by_source[source].append(rule)

        all_active_keys: set[tuple[str, str]] = set()
        all_evaluated_rule_ids: set[str] = set()
        active_items_by_source: dict[str, set[str]] = {}

        # Poll all watchers concurrently
        pollable = {
            source: (self._watchers[source], rules)
            for source, rules in rules_by_source.items()
            if source in self._watchers
        }
        if not pollable:
            return

        async def _poll_one(source: str, watcher: Watcher) -> tuple[str, list[dict] | None]:
            try:
                items = await watcher.poll()
                self._health.record_success(source)
                return source, items
            except Exception as e:
                await self._health.record_failure(source, e)
                return source, None

        poll_results = await asyncio.gather(
            *[_poll_one(src, w) for src, (w, _) in pollable.items()]
        )

        for source, items in poll_results:
            if items is None:
                continue
            _, rules = pollable[source]

            active_keys, evaluated_rule_ids, active_item_ids = await self._evaluate_source(
                source, self._watchers[source], items, rules, now
            )
            all_active_keys |= active_keys
            all_evaluated_rule_ids |= evaluated_rule_ids
            active_items_by_source[source] = active_item_ids

        # Orphan cleanup: cancel timers for items that are no longer valid candidates.
        # Only runs for rules whose source successfully polled this tick.
        for key in list(self._pending):
            if key[0] in all_evaluated_rule_ids and key not in all_active_keys:
                self._pending[key].handle.cancel()
                del self._pending[key]
                logger.debug("Cancelled orphaned timer for rule=%s item=%s", key[0], key[1])

        # Periodic suppression pruning — remove IDs for events no longer in the poll window
        if now - self._last_prune > PRUNE_INTERVAL:
            await self._prune_suppressed(rules_by_source, active_items_by_source)
            self._last_prune = now

    async def _evaluate_source(
        self,
        source: str,
        watcher: Watcher,
        items: list[dict],
        rules: list[TriggerRule],
        now: datetime,
    ) -> tuple[set[tuple[str, str]], set[str], set[str]]:
        """Evaluate polled items against rules for a single source.

        Shared by _tick() (60s cadence) and kick_source() (push-triggered out-of-cycle).
        Returns (active_keys, evaluated_rule_ids, active_item_ids) so callers can
        perform cross-source orphan cleanup.

        Handles both trigger modes:
          - "reactive": fire immediately on first detection; dedup via _fired prevents
            re-fire on subsequent polls (same semantics as on_push_event).
          - "anticipated": compute_fire_time → call_later timer pipeline.
        """
        is_reactive = getattr(watcher, "trigger_mode", "anticipated") == "reactive"

        active_item_ids = {item.get("id", "") for item in items}

        active_keys: set[tuple[str, str]] = set()
        evaluated_rule_ids: set[str] = set()

        for rule in rules:
            rule_id = rule.id
            if not rule_id:
                logger.warning("Skipping rule with missing 'id' field")
                continue

            # Per-rule pause check — cancel any in-flight timers for paused rules
            # so they don't fire during the pause window.
            paused_until_raw = rule.paused_until
            if paused_until_raw:
                try:
                    if now < ensure_aware(paused_until_raw, timezone.utc):
                        for k in [k for k in self._pending if k[0] == rule_id]:
                            self._pending[k].handle.cancel()
                            del self._pending[k]
                        continue
                except (ValueError, TypeError):
                    logger.warning("Invalid paused_until for rule %s: %r", rule_id, paused_until_raw)

            # For reactive sources, skip rules targeting a specific event name
            # that doesn't match what the poll path delivers ("polled").
            # Mirrors the identical guard in on_push_event. Not applied to
            # anticipated sources — their event names are semantic labels (e.g. "starting").
            if is_reactive:
                rule_event = rule.origin.event or ""
                if rule_event and rule_event != "polled":
                    continue

            evaluated_rule_ids.add(rule_id)
            seen_items: set[tuple] = set()

            for item in items:
                item_id = item.get("id", "")
                key = (rule_id, item_id)

                if item_id in rule.suppressed_event_ids:
                    continue
                if not evaluate_conditions(rule.conditions, item):
                    continue

                semantic_key = item_semantic_key(source, item)
                if semantic_key in seen_items:
                    logger.debug(
                        "Skipping duplicate automation item for rule=%s item=%s semantic_key=%s",
                        rule_id, item_id, semantic_key,
                    )
                    continue
                seen_items.add(semantic_key)

                if key in self._fired:
                    continue

                if is_reactive:
                    if await self._claim_fire(key, now):
                        self._create_fire_task(key, rule, item)
                    continue

                fire_time = compute_fire_time(rule, item)
                if fire_time is None:
                    continue

                delay = (fire_time - now).total_seconds()

                if delay <= 0:
                    if now - fire_time > MAX_LATENESS:
                        logger.debug(
                            "Skipping stale fire for rule=%s item=%s (late by %s)",
                            rule_id, item_id, now - fire_time,
                        )
                        await self._claim_fire(key, now, status="skipped_stale")
                        continue
                    pending = self._pending.pop(key, None)
                    if pending:
                        pending.handle.cancel()
                    if await self._claim_fire(key, now):
                        self._create_fire_task(key, rule, item)
                else:
                    active_keys.add(key)
                    pending = self._pending.get(key)
                    if pending is None:
                        handle = self._schedule_fire(key, rule, item, delay)
                        self._pending[key] = PendingFire(
                            fire_time=fire_time, handle=handle,
                            rule_id=rule_id, item_id=item_id, rule=rule, item=item,
                        )
                    elif pending.fire_time != fire_time:
                        pending.handle.cancel()
                        handle = self._schedule_fire(key, rule, item, delay)
                        self._pending[key] = PendingFire(
                            fire_time=fire_time, handle=handle,
                            rule_id=rule_id, item_id=item_id, rule=rule, item=item,
                        )
                        logger.debug(
                            "Rescheduled timer for rule=%s item=%s new_fire=%s",
                            rule_id, item_id, fire_time.isoformat(),
                        )
                    # else: same fire_time — timer already armed, nothing to do.

        return active_keys, evaluated_rule_ids, active_item_ids

    # --- Dedup persistence ---

    async def _claim_fire(
        self,
        key: tuple[str, str],
        now: datetime,
        *,
        status: str = "fired",
    ) -> bool:
        """
        Atomically claim a rule/item fire before dispatch.

        The unique (rule_id, item_id) index turns insert_one into an inter-process
        claim. DuplicateKeyError means another worker already handled it.
        """
        try:
            await mongodb.db.automation_fired.insert_one({
                "rule_id": key[0],
                "item_id": key[1],
                "fired_at": now,
                "status": status,
            })
        except DuplicateKeyError:
            self._fired[key] = now
            return False
        except Exception as e:
            logger.warning("Could not claim fired entry %s: %s", key, e)
            return False

        self._fired[key] = now
        return True

    async def _prune_suppressed(
        self,
        rules_by_source: dict[str, list[TriggerRule]],
        active_items: dict[str, set[str]],
    ) -> None:
        """Remove suppressed event IDs that are no longer in the watcher's poll window."""
        for source, rules in rules_by_source.items():
            current_ids = active_items.get(source)
            if current_ids is None:
                continue
            for rule in rules:
                suppressed = rule.suppressed_event_ids
                if not suppressed:
                    continue
                stale = [eid for eid in suppressed if eid not in current_ids]
                if stale:
                    await mongodb.db.trigger_rules.update_one(
                        {"id": rule.id, "origin.kind": "external"},
                        {"$pullAll": {"suppressed_event_ids": stale}},
                    )
                    logger.debug(
                        "Pruned %d stale suppressed events from rule %s",
                        len(stale), rule.id,
                    )

    # --- Firing ---

    def _schedule_fire(self, key: tuple[str, str], rule: TriggerRule, item: dict, delay: float) -> asyncio.TimerHandle:
        """Schedule a point-in-time fire via call_later. Returns the handle for storage in _pending."""
        loop = asyncio.get_running_loop()
        return loop.call_later(delay, self._create_claimed_fire_task, key, rule, item)

    def _create_claimed_fire_task(self, key: tuple[str, str], rule: TriggerRule, item: dict) -> None:
        """Create a tracked task that claims the fire before dispatching."""
        task = asyncio.create_task(self._claim_and_fire(key, rule, item))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_log_task_exception)

    def _create_fire_task(self, key: tuple[str, str], rule: TriggerRule, item: dict) -> None:
        """Create a tracked fire task with exception logging."""
        task = asyncio.create_task(self._fire(key, rule, item))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_log_task_exception)

    async def _claim_and_fire(self, key: tuple[str, str], rule: TriggerRule, item: dict) -> None:
        """Timer entrypoint: claim in Mongo, then fire only if this worker won."""
        now = datetime.now(timezone.utc)
        if await self._claim_fire(key, now):
            await self._fire(key, rule, item)
        else:
            self._pending.pop(key, None)

    async def _resolve_live_rule(self, rule: TriggerRule) -> TriggerRule | None:
        """Load current rule intent for dispatch; skip if missing, disabled, or paused."""
        doc = await mongodb.db.trigger_rules.find_one(
            {"id": rule.id, "owner_id": rule.owner_id},
        )
        if not doc:
            return None
        live = TriggerRule.model_validate(doc)
        if not live.enabled:
            return None
        paused_until = live.paused_until
        if paused_until:
            try:
                if datetime.now(timezone.utc) < ensure_aware(paused_until, timezone.utc):
                    return None
            except (ValueError, TypeError):
                logger.warning(
                    "Invalid paused_until for rule %s at fire: %r",
                    live.id,
                    paused_until,
                )
        return live

    async def _fire(self, key: tuple[str, str], rule: TriggerRule, item: dict) -> None:
        """Create a TriggerInstance for a matched rule+item pair.

        On dispatch failure, marks the automation_fired entry as status="failed"
        so SystemPulse can surface recent automation errors. Scope: catches
        failures of _fire itself (template render, agent dispatch, event publish).
        Does NOT catch downstream orchestrator/delivery errors — those run on
        separate tasks and log via _log_headless_exception.
        """
        # Remove from pending — the Mongo claim already happened before dispatch.
        self._pending.pop(key, None)

        try:
            live = await self._resolve_live_rule(rule)
            if live is None:
                logger.info(
                    "Skipping automation fire for rule=%s item=%s: rule missing, disabled, or paused",
                    key[0],
                    key[1],
                )
                return
            rule = live

            message = render_automation_message(rule, item)
            owner_id = rule.owner_id
            rule_id = rule.id
            item_id = item.get("id")

            from core.triggers.service import trigger_service
            fire_time = datetime.now(timezone.utc)
            offset_minutes = rule.origin.offset_minutes or 0
            due_at = fire_time + timedelta(minutes=offset_minutes) if offset_minutes > 0 else None
            # automation_fired owns rule/item suppression with a 24h TTL. Trigger
            # dedup is a stable insertion race guard for the same rule/item fire.
            dedup_key = f"{rule_id}:{item_id}" if rule_id and item_id else None
            action = TriggerAction(
                decision=rule.action.decision,
                message=message,
                protocol_name=rule.action.protocol_name,
                instructions=rule.action.instructions,
                content_type=rule.action.content_type,
                reply_grounding=rule.action.reply_grounding,
            )
            origin = TriggerOrigin(
                kind="external",
                source=rule.origin.source,
                event=rule.origin.event or "fire",
                offset_minutes=rule.origin.offset_minutes,
            )
            instance = await trigger_service.create_instance(
                owner_id=owner_id,
                origin=origin,
                action=action,
                attention=rule.attention,
                delivery=rule.delivery,
                freshness=rule.freshness,
                rule_id=rule_id,
                due_at=due_at,
                source_event={
                    "rule_id": rule_id,
                    "rule_name": rule.name,
                    "item_id": item_id,
                    "item": item,
                    "fire_time": fire_time.isoformat(),
                },
                dedup_key=dedup_key,
                management=rule.management,
            )
            if offset_minutes > 0:
                logger.info(
                    "Automation deferred instance %s for %s minutes (rule=%s item=%s)",
                    instance.id,
                    offset_minutes,
                    rule_id,
                    item_id,
                )
            else:
                await event_bus.publish(
                    Event(
                        type=EventType.TRIGGER_DUE,
                        source="automation",
                        data={"instance_id": instance.id, "owner_id": owner_id},
                    )
                )
            logger.info(
                "Automation fired: rule=%s item=%s instance=%s",
                rule_id, item_id, instance.id,
            )
        except Exception as e:
            try:
                await mongodb.db.automation_fired.update_one(
                    {"rule_id": key[0], "item_id": key[1]},
                    {"$set": {
                        "status": "failed",
                        "error": str(e)[:500],
                        "failed_at": datetime.now(timezone.utc),
                    }},
                )
            except Exception as persist_err:
                logger.warning("Could not persist failure for %s: %s", key, persist_err)
            raise


def _log_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("Automation fire task failed", exc_info=task.exception())


# Global instance
automation_service = AutomationService()
