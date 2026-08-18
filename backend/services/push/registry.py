"""
PushRegistry — lifecycle manager for push adapters.

Owns renewal timers, MongoDB persistence, restart recovery, and dispatch routing.
Adapters are auto-discovered from services/push/ (same pattern as watcher discovery).
Adding a new push provider requires only dropping a new file in services/push/.

Design decisions:
- Registry-owned lifecycle: adapters return PushChannel state; the registry manages
  timers, persistence, and retry. Adapters are stateless and testable in isolation.
- Graceful degradation: NeedsReauth / KeyError during any adapter call logs a warning
  and marks the channel inactive. The watcher poll fallback (60s) continues unaffected.
- Renewal buffer: channels are renewed RENEWAL_BUFFER_SECONDS before their expiration
  to prevent gaps in push coverage.
- Restart recovery: push_channels MongoDB collection persists channel state. On startup,
  channels with future expiry have their renewal timers restored; expired/missing channels
  are re-registered with the provider.
"""

import asyncio
import importlib
import inspect
import logging
import pkgutil
from datetime import datetime, timezone
from pathlib import Path

from services.database.mongodb import mongodb
from services.health import IntegrationHealth
from services.push import PushAdapter, PushChannel

logger = logging.getLogger(__name__)

# Renew this many seconds before expiration to avoid gaps
RENEWAL_BUFFER_SECONDS = 3600  # 1 hour


def _to_doc(channel: PushChannel) -> dict:
    return {
        "source": channel.source,
        "resource_id": channel.resource_id,
        "channel_id": channel.channel_id,
        "provider_resource_id": channel.provider_resource_id,
        "expiration": channel.expiration,
        "sync_cursor": channel.sync_cursor,
        "cursor_updated_at": channel.cursor_updated_at,
        "last_renewed_at": channel.last_renewed_at,
        "extra": channel.extra,
    }


def _from_doc(doc: dict) -> PushChannel:
    return PushChannel(
        source=doc["source"],
        resource_id=doc["resource_id"],
        channel_id=doc["channel_id"],
        provider_resource_id=doc.get("provider_resource_id", ""),
        expiration=doc["expiration"],
        sync_cursor=doc.get("sync_cursor"),
        cursor_updated_at=doc.get("cursor_updated_at"),
        last_renewed_at=doc.get("last_renewed_at"),
        extra=doc.get("extra", {}),
    )


class PushRegistry:
    """
    Lifecycle manager for push adapters.

    Startup flow:
      1. Discover all PushAdapter implementations in services/push/.
      2. For each adapter: load existing PushChannels from MongoDB.
         - Channels with future expiry: schedule renewal timers.
         - Expired or missing channels: register with the provider.
      3. On push notification (via webhook route): verify, process, dispatch.

    Shutdown flow:
      - Cancel all pending renewal timers.
      - Call adapter.teardown() for each active channel.
    """

    def __init__(self) -> None:
        self._adapters: dict[str, PushAdapter] = {}
        # (source, resource_id) -> PushChannel
        self._channels: dict[tuple[str, str], PushChannel] = {}
        # (source, resource_id) -> renewal timer handle
        self._renewal_handles: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._health = IntegrationHealth(owner="push")
        self._running = False
        self._background_tasks: set[asyncio.Task] = set()

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._discover_adapters()
        await self._restore_channels()
        logger.info("PushRegistry started with adapters: %s", list(self._adapters))

    async def stop(self) -> None:
        self._running = False

        # Cancel all pending renewal timers
        for handle in self._renewal_handles.values():
            handle.cancel()
        self._renewal_handles.clear()

        # Cancel in-flight background tasks
        for task in list(self._background_tasks):
            task.cancel()
        self._background_tasks.clear()

        # Teardown all active channels
        for (source, resource_id), channel in list(self._channels.items()):
            adapter = self._adapters.get(source)
            if not adapter:
                continue
            try:
                client = await self._get_client(source)
                if client:
                    await adapter.teardown(client, channel)
            except Exception as e:
                logger.warning("Teardown failed for %s/%s: %s", source, resource_id, e)

        self._channels.clear()
        logger.info("PushRegistry stopped")

    def _discover_adapters(self) -> None:
        """Auto-discover PushAdapter implementations in services/push/.

        Mirrors the watcher discovery pattern in AutomationService._discover_watchers().
        Only classes defined in the module are registered (not imports).
        """
        push_pkg = Path(__file__).parent
        for _, module_name, _ in pkgutil.iter_modules([str(push_pkg)]):
            if module_name == "registry":
                continue
            try:
                module = importlib.import_module(f"services.push.{module_name}")
                for _, obj in inspect.getmembers(module, inspect.isclass):
                    if obj is PushAdapter:
                        continue
                    if obj.__module__ != f"services.push.{module_name}":
                        continue
                    if (
                        hasattr(obj, "source")
                        and hasattr(obj, "trigger_mode")
                        and hasattr(obj, "register")
                        and callable(getattr(obj, "register", None))
                    ):
                        instance = obj()
                        self._adapters[instance.source] = instance
                        logger.info("Registered push adapter: %s", instance.source)
            except Exception as e:
                logger.error("Failed to load push module '%s': %s", module_name, e)

    async def _restore_channels(self) -> None:
        """Load persisted PushChannels and restore renewal timers or re-register."""
        now = datetime.now(timezone.utc)

        # Load all persisted channels grouped by source
        persisted: dict[str, list[PushChannel]] = {}
        try:
            async for doc in mongodb.db.push_channels.find({}):
                try:
                    channel = _from_doc(doc)
                    persisted.setdefault(channel.source, []).append(channel)
                except Exception as e:
                    logger.warning("Failed to deserialize push channel doc: %s", e)
        except Exception as e:
            logger.warning("Could not load push channels from DB: %s", e)

        for source, adapter in self._adapters.items():
            client = await self._get_client(source)
            if client is None:
                logger.debug("Push adapter '%s': integration not configured, skipping", source)
                continue

            existing = persisted.get(source, [])
            if not existing:
                await self._register_adapter(adapter, client)
            elif any(ch.expiration <= now for ch in existing):
                # At least one channel expired — re-register all resources for
                # this adapter (register() returns a fresh set per source).
                logger.info("Push adapter '%s' has expired channel(s), re-registering", source)
                await self._register_adapter(adapter, client)
            else:
                # All channels still valid — restore in memory and schedule renewals
                for channel in existing:
                    key = (source, channel.resource_id)
                    self._channels[key] = channel
                    self._schedule_renewal(adapter, channel)
                    logger.info(
                        "Restored push channel %s/%s (expires %s)",
                        source, channel.resource_id, channel.expiration.isoformat(),
                    )

    async def _register_adapter(
        self, adapter: PushAdapter, client: object
    ) -> None:
        """Call adapter.register() and persist all returned channels."""
        try:
            channels = await adapter.register(client)  # type: ignore[arg-type]
            self._health.record_success(adapter.source)
        except Exception as e:
            await self._health.record_failure(adapter.source, e)
            logger.warning("Push adapter '%s' registration failed: %s", adapter.source, e)
            return

        now = datetime.now(timezone.utc)
        for channel in channels:
            channel.last_renewed_at = now
            key = (channel.source, channel.resource_id)
            self._channels[key] = channel
            await self._persist_channel(channel)
            self._schedule_renewal(adapter, channel)
            logger.info(
                "Registered push channel %s/%s (expires %s)",
                channel.source, channel.resource_id, channel.expiration.isoformat(),
            )

    def _schedule_renewal(self, adapter: PushAdapter, channel: PushChannel) -> None:
        """Schedule a renewal timer for a channel, RENEWAL_BUFFER_SECONDS before expiry."""
        now = datetime.now(timezone.utc)
        key = (channel.source, channel.resource_id)

        # Cancel any existing timer for this channel
        existing = self._renewal_handles.pop(key, None)
        if existing:
            existing.cancel()

        delay = max(0.0, (channel.expiration - now).total_seconds() - RENEWAL_BUFFER_SECONDS)
        loop = asyncio.get_running_loop()
        handle = loop.call_later(delay, self._create_renewal_task, adapter, channel)
        self._renewal_handles[key] = handle

    def _create_renewal_task(self, adapter: PushAdapter, channel: PushChannel) -> None:
        task = asyncio.create_task(self._renew_channel(adapter, channel))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(_log_task_exception)

    async def _renew_channel(self, adapter: PushAdapter, channel: PushChannel) -> None:
        """Renew a single channel. On success, persist and reschedule. On failure, retry once."""
        key = (channel.source, channel.resource_id)
        client = await self._get_client(channel.source)
        if client is None:
            logger.warning("Cannot renew push channel %s/%s: integration not available", *key)
            return

        for attempt in (1, 2):
            try:
                new_channel = await adapter.renew(client, channel)  # type: ignore[arg-type]
                new_channel.last_renewed_at = datetime.now(timezone.utc)
                self._channels[key] = new_channel
                await self._persist_channel(new_channel)
                self._schedule_renewal(adapter, new_channel)
                self._health.record_success(channel.source)
                logger.info(
                    "Renewed push channel %s/%s (expires %s)",
                    channel.source, channel.resource_id, new_channel.expiration.isoformat(),
                )
                return
            except Exception as e:
                await self._health.record_failure(channel.source, e)
                if attempt == 1:
                    logger.warning(
                        "Push channel renewal failed for %s/%s (attempt 1), retrying: %s",
                        channel.source, channel.resource_id, e,
                    )
                    await asyncio.sleep(30)
                else:
                    logger.error(
                        "Push channel renewal failed for %s/%s after 2 attempts: %s — "
                        "falling back to poll-only until next restart",
                        channel.source, channel.resource_id, e,
                    )
                    self._channels.pop(key, None)

    async def reregister_all(self) -> None:
        """Force re-registration of all adapters (callback URL change)."""
        for source, adapter in self._adapters.items():
            client = await self._get_client(source)
            if client is None:
                continue
            # Tear down existing channels for this source before re-registering.
            for (s, resource_id), channel in list(self._channels.items()):
                if s != source:
                    continue
                key = (s, resource_id)
                handle = self._renewal_handles.pop(key, None)
                if handle:
                    handle.cancel()
                try:
                    await adapter.teardown(client, channel)
                except Exception as e:
                    logger.warning("Teardown before reregister failed for %s/%s: %s", s, resource_id, e)
                self._channels.pop(key, None)
                try:
                    await mongodb.db.push_channels.delete_one(
                        {"source": s, "resource_id": resource_id}
                    )
                except Exception:
                    logger.debug("Failed to delete push channel doc %s/%s", s, resource_id, exc_info=True)
            await self._register_adapter(adapter, client)

    async def teardown_all_channels(self) -> None:
        """Stop all push channels without stopping the registry."""
        for (source, resource_id), channel in list(self._channels.items()):
            adapter = self._adapters.get(source)
            if not adapter:
                continue
            key = (source, resource_id)
            handle = self._renewal_handles.pop(key, None)
            if handle:
                handle.cancel()
            client = await self._get_client(source)
            if client:
                try:
                    await adapter.teardown(client, channel)
                except Exception as e:
                    logger.warning("Teardown failed for %s/%s: %s", source, resource_id, e)
            self._channels.pop(key, None)
            try:
                await mongodb.db.push_channels.delete_one(
                    {"source": source, "resource_id": resource_id}
                )
            except Exception:
                logger.debug(
                    "Failed to delete push channel doc %s/%s",
                    source,
                    resource_id,
                    exc_info=True,
                )

    # -------------------------------------------------------------------------
    # Dispatch (called by the webhook route background task)
    # -------------------------------------------------------------------------

    async def process_notification(
        self, source: str, adapter: PushAdapter, headers: dict, body: bytes
    ) -> None:
        """Acquire client, call on_notification, dispatch based on trigger_mode."""
        from services.automation import automation_service

        client = await self._get_client(source)
        if client is None:
            logger.warning(
                "Push notification for '%s' ignored: integration not available", source
            )
            return

        try:
            events = await adapter.on_notification(client, headers, body)  # type: ignore[arg-type]
            self._health.record_success(source)
        except Exception as e:
            await self._health.record_failure(source, e)
            logger.warning("on_notification failed for '%s': %s", source, e)
            return

        if adapter.trigger_mode == "reactive":
            for event in events:
                await automation_service.on_push_event(event)
        else:
            # anticipated — kick out-of-cycle watcher poll
            await automation_service.kick_source(source)

    # -------------------------------------------------------------------------
    # Cursor persistence (called by adapters via registry reference)
    # -------------------------------------------------------------------------

    async def update_cursor(self, source: str, resource_id: str, cursor: str) -> None:
        """Persist an updated sync cursor for a channel. Called by adapters after
        successful incremental fetch so the cursor survives restarts."""
        key = (source, resource_id)
        channel = self._channels.get(key)
        if channel is None:
            return
        now = datetime.now(timezone.utc)
        channel.sync_cursor = cursor
        channel.cursor_updated_at = now
        try:
            await mongodb.db.push_channels.update_one(
                {"source": source, "resource_id": resource_id},
                {"$set": {"sync_cursor": cursor, "cursor_updated_at": now}},
            )
        except Exception as e:
            logger.warning("Failed to persist cursor for %s/%s: %s", source, resource_id, e)

    def get_channel(self, source: str, resource_id: str) -> PushChannel | None:
        return self._channels.get((source, resource_id))

    def get_channels_for_source(self, source: str) -> list[PushChannel]:
        return [ch for (s, _), ch in self._channels.items() if s == source]

    # -------------------------------------------------------------------------
    # Lookup
    # -------------------------------------------------------------------------

    def get(self, source: str) -> PushAdapter | None:
        return self._adapters.get(source)

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _get_client(self, source: str) -> object | None:
        """Acquire the integration client for a source. Returns None if unavailable."""
        from core.integrations import integrations
        from core.integrations.manager import NeedsReauth

        try:
            return await integrations.get(source)
        except (KeyError, NeedsReauth) as e:
            logger.debug("Integration '%s' not available for push: %s", source, e)
            return None
        except Exception as e:
            logger.warning("Unexpected error getting client for '%s': %s", source, e)
            return None

    async def _persist_channel(self, channel: PushChannel) -> None:
        try:
            await mongodb.db.push_channels.update_one(
                {"source": channel.source, "resource_id": channel.resource_id},
                {"$set": _to_doc(channel)},
                upsert=True,
            )
        except Exception as e:
            logger.warning(
                "Failed to persist push channel %s/%s: %s",
                channel.source, channel.resource_id, e,
            )


def _log_task_exception(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception():
        logger.error("Push renewal task failed", exc_info=task.exception())


# Global singleton
push_registry = PushRegistry()
