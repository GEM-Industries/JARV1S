"""Durable inbound event inbox with leased retry worker.

Routes verify authenticity, persist here, then ACK. Processing happens
asynchronously with crash-safe leases, bounded retries, and dead letters.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, Optional
from uuid import uuid4

from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from services.database.mongodb import mongodb

logger = logging.getLogger(__name__)

InboundKind = Literal["composio", "push", "external"]
InboundStatus = Literal["pending", "processing", "retry", "processed", "dead_letter"]

_MAX_ATTEMPTS = 5
_LEASE_SECONDS = 60
_POLL_INTERVAL_S = 1.0
_BASE_BACKOFF_S = 5
_RETENTION_DAYS = 7


class InboundEventStats(BaseModel):
    pending: int = 0
    processing: int = 0
    retry: int = 0
    processed: int = 0
    dead_letter: int = 0
    oldest_pending_age_s: Optional[float] = None
    last_received_at: Optional[datetime] = None
    last_processed_at: Optional[datetime] = None


class InboundEventSummary(BaseModel):
    id: str
    kind: InboundKind
    source: str
    status: InboundStatus
    attempts: int = 0
    last_error: Optional[str] = None
    received_at: datetime
    processed_at: Optional[datetime] = None
    next_attempt_at: Optional[datetime] = None


class EnqueueResult(BaseModel):
    event_id: str
    duplicate: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _backoff_seconds(attempts: int) -> int:
    return min(300, _BASE_BACKOFF_S * (2 ** max(0, attempts - 1)))


class InboundEventService:
    """Mongo-backed durable inbox for signed external triggers."""

    def __init__(self) -> None:
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._worker_id = str(uuid4())

    async def enqueue(
        self,
        *,
        idempotency_key: str,
        kind: InboundKind,
        source: str,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> EnqueueResult:
        now = _utcnow()
        expires_at = now + timedelta(days=_RETENTION_DAYS)
        event_id = str(uuid4())
        doc = {
            "id": event_id,
            "idempotency_key": idempotency_key,
            "kind": kind,
            "source": source,
            "payload": payload,
            "raw_body": raw_body.decode("utf-8", errors="replace") if raw_body is not None else None,
            "headers": headers or {},
            "status": "pending",
            "attempts": 0,
            "next_attempt_at": now,
            "lease_until": None,
            "lease_owner": None,
            "last_error": None,
            "received_at": now,
            "processed_at": None,
            "expires_at": expires_at,
        }
        try:
            await mongodb.db.inbound_events.insert_one(doc)
            return EnqueueResult(event_id=event_id, duplicate=False)
        except DuplicateKeyError:
            existing = await mongodb.db.inbound_events.find_one(
                {"idempotency_key": idempotency_key},
                {"id": 1},
            )
            return EnqueueResult(
                event_id=str(existing["id"]) if existing else event_id,
                duplicate=True,
            )

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("InboundEventService started (worker=%s)", self._worker_id)

    async def stop(self) -> None:
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("InboundEventService stopped")

    async def stats(self) -> InboundEventStats:
        now = _utcnow()
        counts: dict[str, int] = {
            "pending": 0,
            "processing": 0,
            "retry": 0,
            "processed": 0,
            "dead_letter": 0,
        }
        async for row in mongodb.db.inbound_events.aggregate(
            [{"$group": {"_id": "$status", "count": {"$sum": 1}}}]
        ):
            status = row.get("_id")
            if status in counts:
                counts[status] = int(row.get("count") or 0)

        oldest = await mongodb.db.inbound_events.find_one(
            {"status": {"$in": ["pending", "retry"]}},
            sort=[("received_at", 1)],
            projection={"received_at": 1},
        )
        oldest_age: Optional[float] = None
        if oldest and oldest.get("received_at"):
            received = oldest["received_at"]
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
            oldest_age = max(0.0, (now - received).total_seconds())

        last_received = await mongodb.db.inbound_events.find_one(
            {},
            sort=[("received_at", -1)],
            projection={"received_at": 1},
        )
        last_processed = await mongodb.db.inbound_events.find_one(
            {"status": "processed"},
            sort=[("processed_at", -1)],
            projection={"processed_at": 1},
        )
        return InboundEventStats(
            pending=counts["pending"],
            processing=counts["processing"],
            retry=counts["retry"],
            processed=counts["processed"],
            dead_letter=counts["dead_letter"],
            oldest_pending_age_s=oldest_age,
            last_received_at=last_received.get("received_at") if last_received else None,
            last_processed_at=last_processed.get("processed_at") if last_processed else None,
        )

    async def list_dead_letters(self, *, limit: int = 50) -> list[InboundEventSummary]:
        rows: list[InboundEventSummary] = []
        cursor = (
            mongodb.db.inbound_events.find({"status": "dead_letter"})
            .sort("received_at", -1)
            .limit(limit)
        )
        async for doc in cursor:
            rows.append(self._to_summary(doc))
        return rows

    async def list_recent(self, *, limit: int = 50) -> list[InboundEventSummary]:
        rows: list[InboundEventSummary] = []
        cursor = mongodb.db.inbound_events.find({}).sort("received_at", -1).limit(limit)
        async for doc in cursor:
            rows.append(self._to_summary(doc))
        return rows

    async def retry_event(self, event_id: str) -> InboundEventSummary | None:
        now = _utcnow()
        result = await mongodb.db.inbound_events.find_one_and_update(
            {"id": event_id, "status": {"$in": ["dead_letter", "retry", "processed"]}},
            {
                "$set": {
                    "status": "pending",
                    "next_attempt_at": now,
                    "lease_until": None,
                    "lease_owner": None,
                    "last_error": None,
                }
            },
            return_document=True,
        )
        if not result:
            return None
        return self._to_summary(result)

    async def _poll_loop(self) -> None:
        while self.running:
            try:
                claimed = await self._claim_next()
                if claimed:
                    await self._process(claimed)
                    continue
            except Exception:
                logger.exception("Inbound event worker tick failed")
            await asyncio.sleep(_POLL_INTERVAL_S)

    async def _claim_next(self) -> dict[str, Any] | None:
        now = _utcnow()
        lease_until = now + timedelta(seconds=_LEASE_SECONDS)
        return await mongodb.db.inbound_events.find_one_and_update(
            {
                "$or": [
                    {
                        "status": {"$in": ["pending", "retry"]},
                        "next_attempt_at": {"$lte": now},
                    },
                    {
                        "status": "processing",
                        "lease_until": {"$lte": now},
                    },
                ]
            },
            {
                "$set": {
                    "status": "processing",
                    "lease_until": lease_until,
                    "lease_owner": self._worker_id,
                },
                "$inc": {"attempts": 1},
            },
            sort=[("received_at", 1)],
            return_document=True,
        )

    async def _process(self, doc: dict[str, Any]) -> None:
        event_id = doc["id"]
        kind = doc.get("kind")
        try:
            if kind == "composio":
                from core.integrations.composio_webhooks import process_composio_inbound

                await process_composio_inbound(doc.get("payload") or {})
            elif kind == "push":
                from services.push.registry import push_registry

                adapter = push_registry.get(doc.get("source", ""))
                if adapter is None:
                    raise RuntimeError(f"No push adapter for source '{doc.get('source')}'")
                raw = (doc.get("raw_body") or "").encode("utf-8")
                await push_registry.process_notification(
                    doc["source"],
                    adapter,
                    dict(doc.get("headers") or {}),
                    raw,
                )
            elif kind == "external":
                from core.integrations.external_webhooks import process_external_inbound

                await process_external_inbound(doc)
            else:
                raise RuntimeError(f"Unknown inbound kind '{kind}'")

            await mongodb.db.inbound_events.update_one(
                {"id": event_id, "lease_owner": self._worker_id},
                {
                    "$set": {
                        "status": "processed",
                        "processed_at": _utcnow(),
                        "lease_until": None,
                        "lease_owner": None,
                        "last_error": None,
                        "next_attempt_at": None,
                    }
                },
            )
            try:
                from core.integrations.external_ingress import mark_event_received

                await mark_event_received()
            except Exception:
                logger.debug("Failed to update external ingress last_received_at", exc_info=True)
        except Exception as exc:
            await self._fail(event_id, str(exc), attempts=int(doc.get("attempts") or 1))

    async def _fail(self, event_id: str, error: str, *, attempts: int) -> None:
        now = _utcnow()
        if attempts >= _MAX_ATTEMPTS:
            await mongodb.db.inbound_events.update_one(
                {"id": event_id, "lease_owner": self._worker_id},
                {
                    "$set": {
                        "status": "dead_letter",
                        "last_error": error[:2000],
                        "lease_until": None,
                        "lease_owner": None,
                        "next_attempt_at": None,
                    }
                },
            )
            logger.error("Inbound event %s moved to dead_letter: %s", event_id, error)
            return

        await mongodb.db.inbound_events.update_one(
            {"id": event_id, "lease_owner": self._worker_id},
            {
                "$set": {
                    "status": "retry",
                    "last_error": error[:2000],
                    "lease_until": None,
                    "lease_owner": None,
                    "next_attempt_at": now + timedelta(seconds=_backoff_seconds(attempts)),
                }
            },
        )
        logger.warning(
            "Inbound event %s attempt %d failed; retrying: %s",
            event_id,
            attempts,
            error,
        )

    @staticmethod
    def _to_summary(doc: dict[str, Any]) -> InboundEventSummary:
        return InboundEventSummary(
            id=str(doc.get("id")),
            kind=doc.get("kind") or "external",
            source=str(doc.get("source") or ""),
            status=doc.get("status") or "pending",
            attempts=int(doc.get("attempts") or 0),
            last_error=doc.get("last_error"),
            received_at=doc.get("received_at") or _utcnow(),
            processed_at=doc.get("processed_at"),
            next_attempt_at=doc.get("next_attempt_at"),
        )


inbound_event_service = InboundEventService()
