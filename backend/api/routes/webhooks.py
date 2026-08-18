"""Composio + trusted-external webhook ingress — verify, enqueue, ACK."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.security.utils import get_authorization_scheme_param

from core.integrations.composio_webhooks import (
    composio_idempotency_key,
    composio_timestamp_is_recent,
    resolve_composio_webhook_secret,
    verify_composio_signature,
)
from core.integrations.external_webhooks import (
    ExternalTriggerBody,
    validate_external_body,
    verify_external_token,
)
from services.inbound_events import inbound_event_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks")


@router.post("/composio")
async def composio_webhook(request: Request) -> dict:
    """Verify HMAC, persist to durable inbox, then ACK."""
    raw_body = await request.body()
    webhook_secret = resolve_composio_webhook_secret()
    if not webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")

    sig = request.headers.get("webhook-signature", "")
    wh_id = request.headers.get("webhook-id", "")
    wh_ts = request.headers.get("webhook-timestamp", "")
    if (
        not sig
        or not wh_id
        or not composio_timestamp_is_recent(wh_ts)
        or not verify_composio_signature(raw_body, wh_id, wh_ts, sig, webhook_secret)
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from None

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    idempotency_key = composio_idempotency_key(webhook_id=wh_id, payload=payload)
    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Missing webhook-id / log_id for idempotency",
        )

    try:
        result = await inbound_event_service.enqueue(
            idempotency_key=idempotency_key,
            kind="composio",
            source="composio",
            payload=payload,
            headers={
                "webhook-id": wh_id,
                "webhook-timestamp": wh_ts,
            },
        )
    except Exception:
        logger.exception("Failed to enqueue Composio webhook")
        raise HTTPException(status_code=503, detail="Failed to persist inbound event") from None

    return {"status": "ok", "duplicate": result.duplicate, "event_id": result.event_id}


@router.post("/external/{source}")
async def external_webhook(source: str, request: Request, body: ExternalTriggerBody) -> dict:
    """Authenticated canonical trigger endpoint for owner-controlled systems."""
    auth = request.headers.get("authorization")
    scheme, token = get_authorization_scheme_param(auth)
    if scheme.lower() != "bearer" or not await verify_external_token(source, token):
        raise HTTPException(status_code=401, detail="Invalid external trigger token")

    try:
        occurred_at = validate_external_body(body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        result = await inbound_event_service.enqueue(
            idempotency_key=f"external:{source.strip().lower()}:{body.event_id}",
            kind="external",
            source=source.strip().lower(),
            payload={
                "event_id": body.event_id,
                "event_type": body.event_type,
                "occurred_at": occurred_at.isoformat(),
                "payload": body.payload,
            },
        )
    except Exception:
        logger.exception("Failed to enqueue external webhook")
        raise HTTPException(status_code=503, detail="Failed to persist inbound event") from None

    return {"status": "ok", "duplicate": result.duplicate, "event_id": result.event_id}
