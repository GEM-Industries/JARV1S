"""Bespoke push webhook ingress — verify, enqueue, ACK.

POST /api/v1/push/{source} receives provider notifications (e.g. Google Calendar
Watch). Gmail remains poll-only until a dedicated adapter exists.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from starlette.responses import Response

from services.inbound_events import inbound_event_service
from services.push.registry import push_registry

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/push")

_ALLOWLISTED_HEADERS = {
    "x-goog-channel-id",
    "x-goog-resource-id",
    "x-goog-resource-state",
    "x-goog-changed",
    "x-goog-channel-token",
    "x-goog-message-number",
    "content-type",
}


def _push_idempotency_key(source: str, headers: dict[str, str]) -> str:
    channel_id = headers.get("x-goog-channel-id", "")
    resource_id = headers.get("x-goog-resource-id", "")
    message_number = headers.get("x-goog-message-number", "")
    if channel_id and resource_id and message_number:
        return f"push:{source}:{channel_id}:{resource_id}:{message_number}"
    if channel_id and resource_id:
        return f"push:{source}:{channel_id}:{resource_id}:{headers.get('x-goog-resource-state', 'unknown')}"
    # Fall back to channel+token+state to avoid unbounded duplicates without message numbers.
    token = headers.get("x-goog-channel-token", "")[:16]
    state = headers.get("x-goog-resource-state", "unknown")
    return f"push:{source}:{channel_id or 'none'}:{token}:{state}"


@router.post("/{source}")
async def push_webhook(source: str, request: Request) -> Response:
    """Verify authenticity, persist to durable inbox, then ACK."""
    adapter = push_registry.get(source)
    if not adapter:
        raise HTTPException(status_code=404, detail=f"No push adapter registered for '{source}'")

    raw_body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    try:
        valid = await adapter.verify(headers, raw_body)
    except Exception as e:
        logger.warning("Push verification error for '%s': %s", source, e)
        raise HTTPException(status_code=401, detail="Verification failed") from e

    if not valid:
        logger.warning("Push verification failed for source '%s'", source)
        raise HTTPException(status_code=401, detail="Invalid push notification")

    safe_headers = {k: v for k, v in headers.items() if k in _ALLOWLISTED_HEADERS}
    try:
        await inbound_event_service.enqueue(
            idempotency_key=_push_idempotency_key(source, safe_headers),
            kind="push",
            source=source,
            raw_body=raw_body,
            headers=safe_headers,
        )
    except Exception:
        logger.exception("Failed to enqueue push notification for '%s'", source)
        raise HTTPException(status_code=503, detail="Failed to persist inbound event") from None

    return Response(status_code=200)
