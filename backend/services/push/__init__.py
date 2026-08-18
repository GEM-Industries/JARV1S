"""
Push adapter protocol and shared types for the Push Layer.

PushAdapter is the provider-side counterpart to Watcher. Where Watcher
provides a polling interface, PushAdapter provides a push/webhook interface.

Ownership split:
  - Adapters own: provider API calls, verification, notification parsing,
    incremental data fetching, sync cursor semantics.
  - PushRegistry owns: renewal timers, MongoDB persistence, restart recovery,
    dispatch routing, health tracking.

Adding a new push provider requires only:
  1. Create services/push/<provider>.py implementing PushAdapter.
  2. PushRegistry auto-discovers it at startup.
  No changes to main.py, routes, or AutomationService.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from services.watchers import TriggerMode  # "anticipated" | "reactive"

if TYPE_CHECKING:
    import httpx
    from services.automation import TriggerEvent


@dataclass
class PushChannel:
    """Persisted state for a single watch channel / subscription.

    Keyed by (source, resource_id) -- supports multiple channels per source
    (e.g. one watch channel per Google Calendar calendar ID).
    """
    source: str
    resource_id: str
    channel_id: str
    provider_resource_id: str
    expiration: datetime
    sync_cursor: str | None = None
    cursor_updated_at: datetime | None = None
    last_renewed_at: datetime | None = None
    extra: dict = field(default_factory=dict)


@runtime_checkable
class PushAdapter(Protocol):
    """
    Thin, stateless provider adapter for one push notification source.
    Does not own timers, persistence, or retry -- PushRegistry manages those.

    trigger_mode controls how the registry dispatches after on_notification():
      - "anticipated": return empty list; registry calls kick_source() to trigger
        an out-of-cycle watcher poll and timer reconciliation.
      - "reactive": return TriggerEvents; registry calls on_push_event() for
        immediate rule evaluation and dispatch.

    source must match the corresponding Watcher.source so that _fired dedup
    prevents double-fire when both push and poll paths deliver the same event.
    """

    source: str
    trigger_mode: TriggerMode

    async def register(self, client: "httpx.AsyncClient") -> list[PushChannel]:
        """Register watch channel(s) with the provider.

        Returns one PushChannel per watched resource (e.g. per calendar ID).
        The registry persists the returned channels and schedules renewal timers
        from their expiration timestamps.
        """
        ...

    async def renew(
        self, client: "httpx.AsyncClient", channel: PushChannel
    ) -> PushChannel:
        """Renew a single expiring channel. Returns the replacement PushChannel.

        For Google Calendar: create a new watch channel (new UUID), then stop the
        old one. Google channels cannot be renewed in-place.
        """
        ...

    async def verify(self, headers: dict, body: bytes) -> bool:
        """Verify that the inbound notification is authentic.

        Called in the request handler (before ACK) so invalid requests are
        rejected immediately without wasting background task resources.

        Google Calendar requires X-Goog-Channel-Token to match the stored channel token.
        """
        ...

    async def on_notification(
        self,
        client: "httpx.AsyncClient",
        headers: dict,
        body: bytes,
    ) -> list["TriggerEvent"]:
        """Process a verified push notification.

        Reactive adapters: fetch incremental data and return TriggerEvents.
        Anticipated adapters: return empty list (registry uses trigger_mode to
        call kick_source() instead of dispatching events directly).

        The client is acquired by the registry in the background task so token
        refresh happens asynchronously after the 200 ACK is already sent.
        """
        ...

    async def teardown(
        self, client: "httpx.AsyncClient", channel: PushChannel
    ) -> None:
        """Unsubscribe / stop a single watch channel on shutdown or disconnect."""
        ...
