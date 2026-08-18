"""Canonical external trigger events from trusted owner-controlled systems."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, Field

from core.context import ensure_aware
from services.automation import TriggerEvent, automation_service
from services.database.mongodb import mongodb

logger = logging.getLogger(__name__)

_MAX_SKEW = timedelta(minutes=5)
_TOKEN_PREFIX = "jvx_"


class ExternalCredential(BaseModel):
    source: str
    token_prefix: str
    created_at: datetime
    revoked: bool = False
    label: str | None = None


class ExternalCredentialCreated(ExternalCredential):
    token: str


class ExternalTriggerBody(BaseModel):
    event_id: str = Field(min_length=1, max_length=200)
    event_type: str = Field(min_length=1, max_length=120)
    occurred_at: datetime | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_external_credential(
    *,
    source: str,
    label: str | None = None,
) -> ExternalCredentialCreated:
    source_key = source.strip().lower()
    if not source_key or not source_key.replace("_", "").isalnum():
        raise ValueError("source must be alphanumeric/underscore")
    token = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    await mongodb.db.external_trigger_credentials.update_one(
        {"source": source_key},
        {
            "$set": {
                "source": source_key,
                "token_hash": _hash_token(token),
                "token_prefix": token[:12],
                "created_at": now,
                "revoked": False,
                "label": label,
            }
        },
        upsert=True,
    )
    return ExternalCredentialCreated(
        source=source_key,
        token_prefix=token[:12],
        created_at=now,
        revoked=False,
        label=label,
        token=token,
    )


async def list_external_credentials() -> list[ExternalCredential]:
    rows: list[ExternalCredential] = []
    async for doc in mongodb.db.external_trigger_credentials.find({"revoked": {"$ne": True}}):
        rows.append(
            ExternalCredential(
                source=doc["source"],
                token_prefix=doc.get("token_prefix", ""),
                created_at=doc.get("created_at") or datetime.now(timezone.utc),
                revoked=bool(doc.get("revoked")),
                label=doc.get("label"),
            )
        )
    return rows


async def revoke_external_credential(source: str) -> bool:
    result = await mongodb.db.external_trigger_credentials.update_one(
        {"source": source.strip().lower()},
        {"$set": {"revoked": True}},
    )
    return result.modified_count > 0


async def verify_external_token(source: str, token: str | None) -> bool:
    if not token:
        return False
    doc = await mongodb.db.external_trigger_credentials.find_one(
        {"source": source.strip().lower(), "revoked": {"$ne": True}}
    )
    if not doc:
        return False
    expected = doc.get("token_hash") or ""
    return hmac.compare_digest(expected, _hash_token(token))


def _as_aware_utc(value: datetime | str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return ensure_aware(value, timezone.utc)


def validate_external_body(body: ExternalTriggerBody) -> datetime:
    occurred = _as_aware_utc(body.occurred_at)
    skew = abs(datetime.now(timezone.utc) - occurred)
    if skew > _MAX_SKEW:
        raise ValueError("occurred_at is outside the allowed 5-minute window")
    return occurred


async def process_external_inbound(doc: dict[str, Any]) -> None:
    payload = doc.get("payload") or {}
    source = str(doc.get("source") or "")
    event = TriggerEvent(
        source=source,
        event_type=str(payload.get("event_type") or "event"),
        event_id=str(payload.get("event_id") or doc.get("idempotency_key") or ""),
        occurred_at=_as_aware_utc(payload.get("occurred_at")),
        provider="external",
        payload=payload.get("payload") if isinstance(payload.get("payload"), dict) else payload,
        raw_event_type=str(payload.get("event_type") or "event"),
    )
    await automation_service.on_push_event(event)
