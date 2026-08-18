"""
CalendarPushAdapter — Google Calendar Watch API push adapter.

Pattern: Signal push (hybrid / anticipated).
Google Calendar push notifications carry no event data — they signal "something
changed on calendar X". The adapter verifies the notification and returns an
empty list; the registry sees trigger_mode="anticipated" and calls
kick_source("calendar"), triggering an out-of-cycle CalendarWatcher.poll().
The existing compute_fire_time → call_later timer pipeline handles scheduling.

Scope: Google Calendar only. When the UnifiedCalendarClient also wraps Outlook,
this adapter still subscribes only to the Google provider — Microsoft Graph
subscriptions (change notifications) are not implemented; Outlook relies on
the 60s poll backstop.

Watch channel lifecycle (Google-specific):
- Channels are per calendar resource (not per source). A user with 3 calendars
  gets 3 watch channels.
- Channels cannot be renewed in-place. Renewal creates a new channel with a new
  UUID, then stops the old one.
- Each channel has a per-channel token (random secret) stored in PushChannel.extra.
  Verification checks X-Goog-Channel-Token against the stored token.
- Google returns the expiration as a millisecond epoch timestamp.

No new OAuth scopes required — calendar.events (already in GOOGLE_CALENDAR_SCOPES)
covers events.watch and channels.stop.
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx

from services.push import PushChannel

logger = logging.getLogger(__name__)


async def _calendar_callback_url() -> str | None:
    from core.integrations.external_ingress import resolve_external_ingress_base_url

    base_url = await resolve_external_ingress_base_url()
    if not base_url:
        return None
    return f"{base_url}/api/v1/push/calendar"


class CalendarPushAdapter:
    """Push adapter for Google Calendar Watch API.

    Registers one watch channel per tracked Google calendar. Push notifications
    trigger kick_source("calendar") which re-runs the full 24h poll — we
    do not use syncToken because it requires fixed query parameters, which
    is incompatible with CalendarWatcher's rolling time window.
    """

    source = "calendar"
    trigger_mode = "anticipated"

    async def register(self, client) -> list[PushChannel]:
        """Register watch channels for all tracked Google calendars.

        Returns one PushChannel per Google calendar. If Google is not configured
        (Outlook-only deployment), returns [] and Outlook polls as the backstop.
        Skips calendars where watch registration fails.

        The `client` parameter is the UnifiedCalendarClient from the calendar
        integration. We reach into its GoogleProvider for the low-level
        Calendar API transport and calendar IDs.
        """
        google_provider = _get_google_provider(client)
        if google_provider is None:
            logger.info("No Google Calendar provider connected; push adapter idle.")
            return []

        transport: httpx.AsyncClient = google_provider.client
        cal_ids = await google_provider.list_calendar_ids()
        channels: list[PushChannel] = []

        callback_url = await _calendar_callback_url()
        if not callback_url:
            logger.info("No external ingress base URL; calendar push adapter idle.")
            return []

        for cal_id in cal_ids:
            channel_id = str(uuid4())
            token = secrets.token_hex(32)
            body = {
                "id": channel_id,
                "type": "web_hook",
                "address": callback_url,
                "token": token,
            }
            try:
                resp = await transport.post(
                    f"/calendars/{_encode_cal_id(cal_id)}/events/watch",
                    json=body,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                logger.warning(
                    "Calendar watch registration failed for calendar '%s': %s — "
                    "poll fallback active", cal_id, e,
                )
                continue

            expiration = _parse_expiration(data.get("expiration"))
            provider_resource_id = data.get("resourceId", "")

            channel = PushChannel(
                source=self.source,
                resource_id=cal_id,
                channel_id=channel_id,
                provider_resource_id=provider_resource_id,
                expiration=expiration,
                extra={"token": token},
            )
            channels.append(channel)
            logger.info(
                "Registered Calendar watch channel for '%s' (expires %s)",
                cal_id, expiration.isoformat(),
            )

        return channels

    async def renew(self, client, channel: PushChannel) -> PushChannel:
        """Renew a calendar watch channel.

        Google Calendar channels cannot be renewed in-place. We create a new
        channel first, then stop the old one. If the new channel creation fails,
        the old channel continues until it expires (poll fallback then takes over).
        """
        google_provider = _get_google_provider(client)
        if google_provider is None:
            raise RuntimeError("Cannot renew Google calendar channel: provider not connected")
        transport: httpx.AsyncClient = google_provider.client

        callback_url = await _calendar_callback_url()
        if not callback_url:
            raise RuntimeError("Cannot renew Google calendar channel: no external ingress URL")
        new_channel_id = str(uuid4())
        new_token = secrets.token_hex(32)

        body = {
            "id": new_channel_id,
            "type": "web_hook",
            "address": callback_url,
            "token": new_token,
        }
        resp = await transport.post(
            f"/calendars/{_encode_cal_id(channel.resource_id)}/events/watch",
            json=body,
        )
        resp.raise_for_status()
        data = resp.json()

        new_expiration = _parse_expiration(data.get("expiration"))
        new_provider_resource_id = data.get("resourceId", "")

        # Stop the old channel (best-effort — if it fails, it will expire on its own)
        await self._stop_channel(transport, channel)

        return PushChannel(
            source=self.source,
            resource_id=channel.resource_id,
            channel_id=new_channel_id,
            provider_resource_id=new_provider_resource_id,
            expiration=new_expiration,
            extra={"token": new_token},
        )

    async def verify(self, headers: dict, body: bytes) -> bool:
        """Verify a Google Calendar push notification.

        Checks X-Goog-Channel-Token against the stored per-channel token.
        The registry provides channel state via get_channel(); adapters
        look up the stored token by channel ID from X-Goog-Channel-ID.
        """
        from services.push.registry import push_registry

        channel_id = headers.get("x-goog-channel-id", "")
        received_token = headers.get("x-goog-channel-token", "")
        if not channel_id or not received_token:
            return False

        # Find the channel by channel_id to retrieve its stored token
        for ch in push_registry.get_channels_for_source(self.source):
            if ch.channel_id == channel_id:
                stored_token = ch.extra.get("token", "")
                return secrets.compare_digest(received_token, stored_token)

        logger.debug(
            "Calendar push notification for unknown channel_id '%s' — may be a "
            "leftover channel from a previous session", channel_id,
        )
        return False

    async def on_notification(
        self, client, headers: dict, body: bytes
    ) -> list:
        """Handle a verified Calendar push notification.

        Returns empty list — the registry sees trigger_mode="anticipated" and
        calls kick_source("calendar") for an out-of-cycle watcher re-poll.
        The existing timer pipeline handles all scheduling from there.
        """
        logger.debug(
            "Calendar push notification received (resource_state=%s, channel=%s)",
            headers.get("x-goog-resource-state", ""),
            headers.get("x-goog-channel-id", ""),
        )
        return []

    async def teardown(self, client, channel: PushChannel) -> None:
        """Stop a single Calendar watch channel."""
        google_provider = _get_google_provider(client)
        if google_provider is None:
            return
        await self._stop_channel(google_provider.client, channel)

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    async def _stop_channel(
        self, client: httpx.AsyncClient, channel: PushChannel
    ) -> None:
        try:
            resp = await client.post(
                "/channels/stop",
                json={"id": channel.channel_id, "resourceId": channel.provider_resource_id},
            )
            # 204 No Content on success; 404 if already expired — both are fine
            if resp.status_code not in (200, 204, 404):
                resp.raise_for_status()
        except Exception as e:
            logger.warning(
                "Failed to stop Calendar watch channel %s: %s", channel.channel_id, e
            )


def _get_google_provider(client):
    """Extract the GoogleProvider from the UnifiedCalendarClient.

    This adapter only speaks the Google Calendar Watch API; if Google isn't
    connected (Outlook-only deployment), returns None and the adapter stays idle.
    """
    return client.get_provider("google")


def _encode_cal_id(cal_id: str) -> str:
    from urllib.parse import quote
    return quote(cal_id, safe="")


def _parse_expiration(expiration_ms: str | int | None) -> datetime:
    """Parse Google's millisecond epoch expiration timestamp."""
    try:
        ms = int(expiration_ms)  # type: ignore[arg-type]
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc) + timedelta(days=7)
