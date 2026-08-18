"""Attention service.

The effective mode is *derived* on every read from two inputs: the persisted
`ManualOverride` and the owner's enabled `QuietWindow`s. The service never
reconciles competing writers into a single stored mode — it stores the override
and computes the rest. The `attention_state` doc only caches `published_mode`
so the reconcile loop can emit edge-triggered `ATTENTION_CHANGED` events.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.attention.models import (
    MANUAL_SOURCES,
    AttentionMode,
    AttentionSource,
    AttentionState,
    ManualOverride,
    QuietWindow,
)
from core.attention.resolver import resolve_effective_attention, resolve_scheduled_attention
from core.id import generate_id
from services.database.mongodb import mongodb
from services.events import Event, EventType, event_bus

logger = logging.getLogger(__name__)

_STATE_COLLECTION = "attention_state"
_SCHEDULE_COLLECTION = "attention_schedules"


class AttentionService:
    """Mongo-backed manual override + quiet-window schedules; derives effective state."""

    async def get_state(self, owner_id: str) -> AttentionState:
        now = datetime.now(timezone.utc)
        doc = await mongodb.db[_STATE_COLLECTION].find_one({"owner_id": owner_id})
        override = _parse_override(doc)
        windows = await self.list_quiet_windows(owner_id, enabled_only=True)
        return self._build_state(owner_id, now, override, windows)

    async def get_mode(self, owner_id: str) -> AttentionMode:
        return (await self.get_state(owner_id)).mode

    async def set_mode(
        self,
        owner_id: str,
        mode: AttentionMode,
        duration_minutes: int | None = None,
        source: str = "tool",
    ) -> AttentionState:
        """Record an explicit attention choice and return the resulting state."""
        now = datetime.now(timezone.utc)
        normalized = self._normalize_source(source)
        windows = await self.list_quiet_windows(owner_id, enabled_only=True)
        override = self._build_override(now, mode, normalized, duration_minutes, windows)

        state = self._build_state(owner_id, now, override, windows)
        await self._persist(owner_id, override, state, now)
        logger.info(
            "attention mode=%s owner=%s source=%s expires=%s",
            state.mode, owner_id, state.source, state.expires_at,
        )
        await self._publish_changed(state)
        return state

    @staticmethod
    def _build_override(
        now: datetime,
        mode: AttentionMode,
        source: AttentionSource,
        duration_minutes: int | None,
        windows: list[QuietWindow],
    ) -> ManualOverride | None:
        if mode == "active":
            # Active only needs an override to temporarily suppress an *active*
            # quiet window; otherwise it is just the default and future windows
            # must still apply, so we clear the override.
            scheduled = resolve_scheduled_attention(now, windows)
            if scheduled.mode == "quiet" and scheduled.effective_until:
                return ManualOverride(
                    mode="active", source=source, set_at=now, expires_at=scheduled.effective_until
                )
            return None

        expires_at = (
            now + timedelta(minutes=duration_minutes)
            if duration_minutes and duration_minutes > 0
            else None
        )
        return ManualOverride(mode=mode, source=source, set_at=now, expires_at=expires_at)

    # ------------------------------------------------------------------
    # Quiet windows
    # ------------------------------------------------------------------

    async def list_quiet_windows(
        self,
        owner_id: str,
        *,
        enabled_only: bool = False,
    ) -> list[QuietWindow]:
        query: dict[str, Any] = {"owner_id": owner_id}
        if enabled_only:
            query["enabled"] = True
        cursor = mongodb.db[_SCHEDULE_COLLECTION].find(query).sort("start_time", 1)
        docs = await cursor.to_list(None)
        return [QuietWindow(**{k: v for k, v in doc.items() if k != "_id"}) for doc in docs]

    async def get_quiet_window(self, owner_id: str, window_id: str) -> QuietWindow | None:
        doc = await mongodb.db[_SCHEDULE_COLLECTION].find_one(
            {"owner_id": owner_id, "id": window_id}
        )
        if not doc:
            return None
        return QuietWindow(**{k: v for k, v in doc.items() if k != "_id"})

    async def upsert_quiet_window(self, window: QuietWindow) -> QuietWindow:
        now = datetime.now(timezone.utc)
        window = window.model_copy(
            update={"updated_at": now, "created_at": window.created_at or now}
        )
        await mongodb.db[_SCHEDULE_COLLECTION].update_one(
            {"owner_id": window.owner_id, "id": window.id},
            {"$set": window.model_dump(mode="json")},
            upsert=True,
        )
        await self.reconcile_owner(window.owner_id)
        return window

    async def delete_quiet_window(self, owner_id: str, window_id: str) -> bool:
        result = await mongodb.db[_SCHEDULE_COLLECTION].delete_one(
            {"owner_id": owner_id, "id": window_id}
        )
        if result.deleted_count:
            await self.reconcile_owner(owner_id)
            return True
        return False

    # ------------------------------------------------------------------
    # Reconciliation (edge-triggered event emission only)
    # ------------------------------------------------------------------

    async def reconcile_owner(self, owner_id: str) -> AttentionState:
        """Recompute effective state; publish + cache only when the mode changes."""
        now = datetime.now(timezone.utc)
        doc = await mongodb.db[_STATE_COLLECTION].find_one({"owner_id": owner_id})
        override = _parse_override(doc)
        windows = await self.list_quiet_windows(owner_id, enabled_only=True)
        state = self._build_state(owner_id, now, override, windows)

        # Drop a spent override so it stops shadowing future windows.
        if override is not None and override.expires_at and override.expires_at <= now:
            override = None

        if doc is None or doc.get("published_mode") != state.mode:
            await self._persist(owner_id, override, state, now)
            await self._publish_changed(state)
        return state

    async def reconcile_all_owners(self) -> None:
        owner_ids = set(await mongodb.db[_SCHEDULE_COLLECTION].distinct("owner_id"))
        owner_ids.update(await mongodb.db[_STATE_COLLECTION].distinct("owner_id"))
        for owner_id in owner_ids:
            await self.reconcile_owner(owner_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_state(
        owner_id: str,
        now: datetime,
        override: ManualOverride | None,
        windows: list[QuietWindow],
    ) -> AttentionState:
        effective = resolve_effective_attention(now, override, windows)
        return AttentionState(
            owner_id=owner_id,
            mode=effective.mode,
            expires_at=effective.expires_at,
            updated_at=now,
            source=effective.source,
            active_window_ids=effective.active_window_ids,
        )

    async def _persist(
        self,
        owner_id: str,
        override: ManualOverride | None,
        state: AttentionState,
        now: datetime,
    ) -> None:
        await mongodb.db[_STATE_COLLECTION].update_one(
            {"owner_id": owner_id},
            {
                "$set": {
                    "owner_id": owner_id,
                    "override": override.model_dump(mode="json") if override else None,
                    "published_mode": state.mode,
                    "updated_at": now.isoformat(),
                }
            },
            upsert=True,
        )

    async def _publish_changed(self, state: AttentionState) -> None:
        await event_bus.publish(
            Event(
                type=EventType.ATTENTION_CHANGED,
                source=state.source,
                data={"owner_id": state.owner_id, "state": state.model_dump(mode="json")},
            )
        )

    @staticmethod
    def _normalize_source(source: str) -> AttentionSource:
        if source in MANUAL_SOURCES:
            return source  # type: ignore[return-value]
        if source.startswith("local_command"):
            return "local_command"
        return "tool"


def _parse_override(doc: dict[str, Any] | None) -> ManualOverride | None:
    if not doc:
        return None
    raw = doc.get("override")
    if not raw:
        return None
    return ManualOverride(**raw)


attention_service = AttentionService()


def new_window_id() -> str:
    return generate_id("attsched-")
