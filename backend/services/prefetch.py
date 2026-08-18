"""
PrefetchService — protocol pre-renderer.

Polls upcoming protocol-linked trigger instances and anticipated
automations a few minutes ahead of their fire time, runs a headless silent turn
to pre-compute the briefing, and caches the rendered text in MongoDB. The
orchestrator consumes the cache at fire time so announce-mode briefings deliver
sub-second.

Design properties:
- Announce-only: silent has nothing to pre-render; evaluate needs live
  evaluation. Non-announce candidates never enter the pipeline.
- Single-flight via `find_one_and_update`: one atomic round trip claims the
  slot for the first writer; concurrent ticks short-circuit on the unique
  `(source, trigger_id, protocol_name)` index.
- Self-healing: stale `running` rows (older than 2 polls) are reclaimable;
  TTL on `expires_at` cleans up everything else.
- Fire-time validation lives at consume time in the orchestrator — stale
  entries are rejected in favor of live execution.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Optional

from pymongo.errors import DuplicateKeyError

from core.config import settings
from core.prompts.protocol_context import build_protocol_context, is_protocol_prefetch_safe
from core.prompts.system_turn_context import SystemTurnContext, build_system_turn_message
from core.triggers.vocabulary import DECISION_TELL
from core.turns.delivery import contains_no_reply
from services.automation import automation_service, render_template
from services.database.mongodb import mongodb

if TYPE_CHECKING:
    from core.turns.orchestrator import AssistantOrchestrator

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PrefetchCandidate:
    """One upcoming protocol-linked trigger eligible for prefetch."""
    source: str            # "trigger" | "automation"
    trigger_id: str        # instance_id or f"{rule_id}:{item_id}"
    protocol_name: str
    owner_id: str
    fire_time: datetime
    system_context: str    # rendered prompt fed to _run_headless_turn

    @property
    def key(self) -> dict[str, str]:
        return {
            "source": self.source,
            "trigger_id": self.trigger_id,
            "protocol_name": self.protocol_name,
        }


class PrefetchService:
    """Polls upcoming triggers and pre-renders their protocol output."""

    def __init__(
        self,
        *,
        poll_interval_s: Optional[int] = None,
        window_min: Optional[int] = None,
    ):
        self.poll_interval_s = poll_interval_s or settings.PREFETCH_POLL_INTERVAL_S
        self.window = timedelta(minutes=window_min or settings.PREFETCH_WINDOW_MIN)
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._inflight: set[asyncio.Task] = set()
        self._orchestrator: Optional["AssistantOrchestrator"] = None

    # --- Lifecycle ---

    async def start(self, orchestrator: "AssistantOrchestrator") -> None:
        """Start the poll loop. `orchestrator` is required — cache rows are
        useless without it, and injecting here keeps prefetch decoupled from
        websocket-handler module state."""
        if self.running:
            return
        self._orchestrator = orchestrator
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "PrefetchService started (interval=%ds, window=%s)",
            self.poll_interval_s, self.window,
        )

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        # Drain in-flight prefetch runs so we don't leave headless turns mid-LLM.
        if self._inflight:
            for t in self._inflight:
                t.cancel()
            await asyncio.gather(*self._inflight, return_exceptions=True)
        logger.info("PrefetchService stopped")

    async def _poll_loop(self) -> None:
        # Sleep first so crash-restart loops don't spam prefetch runs.
        while self.running:
            try:
                await asyncio.sleep(self.poll_interval_s)
            except asyncio.CancelledError:
                break
            if not self.running:
                break
            try:
                await self._tick()
            except Exception:
                logger.exception("PrefetchService tick failed")

    # --- Tick ---

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        window_end = now + self.window

        triggers = await self._scan_triggers(now, window_end)
        automations = (
            [] if automation_service.is_paused(now)
            else await self._scan_automations(now, window_end)
        )

        for candidate in (*triggers, *automations):
            if await self._claim_slot(candidate, now):
                self._spawn_run(candidate)

    def _spawn_run(self, candidate: PrefetchCandidate) -> None:
        """Schedule a prefetch run and track it for graceful shutdown."""
        task = asyncio.create_task(self._run_prefetch(candidate))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)

    # --- Scanners ---

    async def _scan_triggers(
        self, now: datetime, window_end: datetime
    ) -> list[PrefetchCandidate]:
        """Upcoming tell-protocol trigger instances with voice routing."""
        cursor = mongodb.db.trigger_instances.find({
            "status": "pending",
            "action_snapshot.decision": DECISION_TELL,
            "action_snapshot.protocol_name": {"$exists": True, "$ne": None},
            "delivery_snapshot.channel": "voice",
            "due_at": {
                "$gte": now,
                "$lte": window_end,
            },
        })
        candidates: list[PrefetchCandidate] = []
        async for doc in cursor:
            action = doc.get("action_snapshot", {})
            protocol = action.get("protocol_name")
            owner_id = doc.get("owner_id")
            instance_id = doc.get("id")
            if not (owner_id and protocol and instance_id):
                continue
            if not await is_protocol_prefetch_safe(protocol, owner_id):
                continue
            due_raw = doc.get("due_at")
            if isinstance(due_raw, str):
                try:
                    fire_time = datetime.fromisoformat(due_raw)
                except ValueError:
                    continue
            elif isinstance(due_raw, datetime):
                fire_time = due_raw
            else:
                continue
            candidates.append(PrefetchCandidate(
                source="trigger",
                trigger_id=instance_id,
                protocol_name=protocol,
                owner_id=owner_id,
                fire_time=fire_time,
                system_context=await self._render_context(
                    owner_id=owner_id,
                    protocol=protocol,
                    message=action.get("message", ""),
                    item_context=doc.get("source_event"),
                    rule_id=doc.get("rule_id"),
                    rule_name=action.get("message"),
                    instructions=action.get("instructions"),
                ),
            ))
        return candidates

    async def _scan_automations(
        self, now: datetime, window_end: datetime
    ) -> list[PrefetchCandidate]:
        """Upcoming anticipated automation fires with a linked protocol + speakable decision."""
        candidates: list[PrefetchCandidate] = []
        for fire in automation_service.iter_upcoming_protocol_fires(now, window_end):
            action = fire["rule"].get("action", {})
            if action.get("decision") != DECISION_TELL:
                continue
            owner_id = fire["owner_id"]
            protocol = fire["protocol_name"]
            if not (owner_id and protocol):
                continue
            if not await is_protocol_prefetch_safe(protocol, owner_id):
                continue
            candidates.append(PrefetchCandidate(
                source="automation",
                trigger_id=f"{fire['rule_id']}:{fire['item_id']}",
                protocol_name=protocol,
                owner_id=owner_id,
                fire_time=fire["fire_time"],
                system_context=await self._render_context(
                    owner_id=owner_id,
                    protocol=protocol,
                    message=render_template(
                        action.get("message", "Automation triggered."), fire["item"],
                    ),
                    item_context=fire["item"],
                    rule_id=fire["rule_id"],
                    rule_name=fire["rule"].get("name"),
                    instructions=action.get("instructions"),
                ),
            ))
        return candidates

    async def _render_context(
        self,
        *,
        owner_id: str,
        protocol: str,
        message: str,
        item_context: Any,
        rule_id: Optional[str],
        rule_name: Optional[str],
        instructions: Optional[str],
    ) -> str:
        """Build the system_context using the shared context builder.

        Reusing the same builder as the live path guarantees prefetched and
        live runs see identical prompts — without this guarantee the cache is
        a footgun.
        """
        protocol_ctx = await build_protocol_context(
            protocol, owner_id, settings.PREFETCH_FALLBACK_TIMEZONE,
        )
        return build_system_turn_message(SystemTurnContext(
            message=message,
            decision=DECISION_TELL,
            item_context=item_context,
            rule_id=rule_id,
            rule_name=rule_name,
            instructions=instructions,
            protocol_context=protocol_ctx,
            content_type="protocol",
        ))

    # --- Claim + run ---

    async def _claim_slot(
        self, candidate: PrefetchCandidate, now: datetime
    ) -> bool:
        """Atomically reserve a `running` slot. Returns True iff we own it.

        Single-flight via `find_one_and_update` plus a fallback `insert_one`:
        - The update matches stale-running or failed rows and replaces them.
        - When no claimable row exists, the insert tries to create one; the
          unique compound index guarantees at most one writer wins, so a
          racing tick (or a fresh ready/running row) collapses to False.
        """
        stale_cutoff = now - timedelta(seconds=self.poll_interval_s * 2)
        running_doc = {
            **candidate.key,
            "owner_id": candidate.owner_id,
            "fire_time": candidate.fire_time,
            "expires_at": candidate.fire_time + self.window,
            "status": "running",
            "created_at": now,
        }

        replaced = await mongodb.db.prefetched_results.find_one_and_update(
            {
                **candidate.key,
                "$or": [
                    {"status": "failed"},
                    {"status": "running", "created_at": {"$lt": stale_cutoff}},
                ],
            },
            {"$set": running_doc},
        )
        if replaced is not None:
            return True

        try:
            await mongodb.db.prefetched_results.insert_one(running_doc)
            return True
        except DuplicateKeyError:
            # A fresh ready/running row already owns the slot.
            return False

    async def _run_prefetch(self, candidate: PrefetchCandidate) -> None:
        """Execute the headless turn and persist the rendered text."""
        assert self._orchestrator is not None  # _spawn_run only fires after start()
        started_at = datetime.now(timezone.utc)
        try:
            result = await self._orchestrator._run_headless_turn(
                session_context={
                    "owner_id": candidate.owner_id,
                    "connection_id": candidate.owner_id,
                    "timezone": settings.PREFETCH_FALLBACK_TIMEZONE,
                },
                system_context=candidate.system_context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("Prefetch run failed for %s", candidate.trigger_id)
            await self._mark_failed(candidate, str(e)[:500])
            return

        text = (result.full_response or "").strip()
        if not text or contains_no_reply(text):
            # NO_REPLY is the evaluate sentinel; we should never speak it
            # verbatim, and an empty response wouldn't be useful to cache.
            logger.debug(
                "Prefetch produced no usable text for %s/%s",
                candidate.source, candidate.trigger_id,
            )
            await self._mark_failed(candidate, "empty or NO_REPLY response")
            return

        await mongodb.db.prefetched_results.update_one(
            candidate.key,
            {"$set": {
                "status": "ready",
                "text": text,
                "completed_at": datetime.now(timezone.utc),
            }},
        )
        elapsed_ms = int((datetime.now(timezone.utc) - started_at).total_seconds() * 1000)
        logger.info(
            "Prefetched %s/%s protocol=%s (%dms)",
            candidate.source, candidate.trigger_id, candidate.protocol_name, elapsed_ms,
        )

    async def _mark_failed(self, candidate: PrefetchCandidate, error: str) -> None:
        try:
            await mongodb.db.prefetched_results.update_one(
                candidate.key,
                {"$set": {
                    "status": "failed",
                    "error": error,
                    "completed_at": datetime.now(timezone.utc),
                }},
            )
        except Exception:
            logger.warning("Could not persist prefetch failure for %s", candidate.trigger_id)


prefetch_service = PrefetchService()
