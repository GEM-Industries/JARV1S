"""Composio webhook verification, normalization, and lifecycle handling.

HTTP routes authenticate via this module, then enqueue; processing stays here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Any

from core.config import settings
from core.context import ensure_aware
from services.automation import TriggerEvent, automation_service

logger = logging.getLogger(__name__)

_WEBHOOK_MAX_AGE_SECONDS = 300
_CONNECTION_LIFECYCLE_EVENTS = {
    "composio.connected_account.expired",
}


def resolve_composio_webhook_secret() -> str | None:
    """Resolve HMAC secret: gateway → CredentialStore → env override."""
    from core.credentials.store import credential_store
    from core.integrations.composio_gateway import get_composio_gateway

    gateway = get_composio_gateway()
    return (
        (gateway.webhook_secret if gateway else None)
        or credential_store.get_secret("COMPOSIO_WEBHOOK_SECRET")
        or settings.COMPOSIO_WEBHOOK_SECRET
    )


def verify_composio_signature(
    body: bytes,
    webhook_id: str,
    webhook_timestamp: str,
    webhook_signature: str,
    secret: str,
) -> bool:
    signing_input = f"{webhook_id}.{webhook_timestamp}.".encode() + body
    expected = base64.b64encode(
        hmac.new(secret.encode(), signing_input, hashlib.sha256).digest()
    ).decode()
    received = (
        webhook_signature.split(",", 1)[-1]
        if "," in webhook_signature
        else webhook_signature
    )
    return hmac.compare_digest(expected, received)


def composio_timestamp_is_recent(value: str, *, now: datetime | None = None) -> bool:
    try:
        timestamp = datetime.fromtimestamp(int(value), tz=timezone.utc)
    except (OverflowError, TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    return abs((current - timestamp).total_seconds()) <= _WEBHOOK_MAX_AGE_SECONDS


def derive_source_event(slug: str) -> tuple[str, str]:
    """Derive (source, event_type) from a Composio trigger slug.

    Splits on the first underscore: GMAIL_NEW_GMAIL_MESSAGE -> ("gmail", "new_gmail_message").
    """
    parts = slug.split("_", 1)
    source = parts[0].lower()
    event_type = parts[1].lower() if len(parts) > 1 else slug.lower()
    return (source, event_type)


def normalize_composio_event(payload: dict[str, Any]) -> TriggerEvent | None:
    """Normalize a Composio trigger payload into a TriggerEvent."""
    metadata = payload.get("metadata") or {}
    trigger_slug = (
        metadata.get("trigger_slug")
        or payload.get("trigger_slug")
        or payload.get("trigger_name")
        or payload.get("triggerName", "")
    )
    data = payload.get("data") or payload.get("payload") or {}
    log_id = metadata.get("log_id") or payload.get("log_id", "")

    if not trigger_slug:
        logger.warning("Composio webhook payload missing trigger_slug: %s", payload)
        return None

    source, event_type = derive_source_event(trigger_slug)

    timestamp = payload.get("timestamp") or metadata.get("occurred_at", "")
    try:
        occurred_at = (
            ensure_aware(timestamp, timezone.utc) if timestamp else datetime.now(timezone.utc)
        )
    except (ValueError, TypeError):
        occurred_at = datetime.now(timezone.utc)

    event_id = log_id or payload.get("id", "") or trigger_slug

    return TriggerEvent(
        source=source,
        event_type=event_type,
        event_id=event_id,
        occurred_at=occurred_at,
        provider="composio",
        payload=data if isinstance(data, dict) else {"raw": str(data)},
        raw_event_type=trigger_slug,
    )


def parse_lifecycle_event(
    payload: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """Extract lifecycle event context from a Composio V3 webhook envelope."""
    event_type = payload.get("type", "")
    if not isinstance(event_type, str) or event_type not in _CONNECTION_LIFECYCLE_EVENTS:
        return "", None, None

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return event_type, None, None

    toolkit = data.get("toolkit")
    app_name: str | None = None
    if isinstance(toolkit, dict):
        slug = toolkit.get("slug")
        app_name = slug if isinstance(slug, str) and slug else None
    elif isinstance(toolkit, str) and toolkit:
        app_name = toolkit

    connected_account_id = data.get("id")
    if not isinstance(connected_account_id, str) or not connected_account_id:
        connected_account_id = None

    return event_type, app_name, connected_account_id


async def handle_connection_lifecycle_event(payload: dict[str, Any]) -> bool:
    """Handle a Composio connection lifecycle event.

    Returns True if the payload was a lifecycle event (handled or skipped).
    """
    event_type, app_name, connected_account_id = parse_lifecycle_event(payload)
    if not event_type:
        return False

    if not app_name:
        logger.warning(
            "Composio lifecycle event '%s' has no toolkit slug (account=%s) — skipping",
            event_type,
            connected_account_id,
        )
        return True

    from core.integrations.lifecycle import teardown_local_integration

    removed = await teardown_local_integration(app_name)
    logger.info(
        "Handled Composio lifecycle event '%s' for '%s' (local plugin removed=%s)",
        event_type,
        app_name,
        removed,
    )
    return True


def composio_idempotency_key(
    *,
    webhook_id: str | None,
    payload: dict[str, Any],
) -> str | None:
    """Resolve a stable Composio idempotency key; reject weak fallbacks."""
    if webhook_id and webhook_id.strip():
        return f"composio:{webhook_id.strip()}"
    metadata = payload.get("metadata") or {}
    log_id = metadata.get("log_id") or payload.get("log_id")
    if isinstance(log_id, str) and log_id.strip():
        return f"composio:{log_id.strip()}"
    return None


async def process_composio_inbound(payload: dict[str, Any]) -> None:
    """Dispatch a verified Composio inbound event to lifecycle or automation."""
    if await handle_connection_lifecycle_event(payload):
        return
    event = normalize_composio_event(payload)
    if event is None:
        raise ValueError("Composio payload missing trigger_slug")
    await automation_service.on_push_event(event)
