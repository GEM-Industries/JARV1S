"""Freshness evaluation for trigger delivery and offer retry scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from core.time import coerce_datetime_or_none


def source_event_start(instance: Any) -> datetime | None:
    source_event = instance.source_event or {}
    item = source_event.get("item", {}) if isinstance(source_event, dict) else {}
    return coerce_datetime_or_none(item.get("start") or source_event.get("start"))


def freshness_expiry_reason(
    instance: Any,
    *,
    now: datetime,
    due_at: datetime | None,
) -> str | None:
    policy = instance.freshness_snapshot

    expires_at = coerce_datetime_or_none(policy.expires_at)
    if expires_at and now > expires_at:
        return "freshness_expired"

    if policy.stale_if_source_event_started:
        event_start = source_event_start(instance)
        if event_start and now > event_start:
            return "calendar_event_started"

    expire_after_due_s = policy.expire_after_due_s
    if due_at and isinstance(expire_after_due_s, int) and now > due_at + timedelta(seconds=expire_after_due_s):
        return "delivery_ttl_expired"

    return None


def trigger_expiry_reason(
    instance: Any,
    *,
    now: datetime | None = None,
) -> str | None:
    now = now or datetime.now(timezone.utc)
    due_at = coerce_datetime_or_none(instance.due_at)
    return freshness_expiry_reason(instance, now=now, due_at=due_at)


def freshness_forces_delivery(instance: Any, expiry_reason: str | None) -> bool:
    if not expiry_reason:
        return False
    if expiry_reason == "calendar_event_started":
        return False
    policy = getattr(instance, "freshness_snapshot", None)
    return getattr(policy, "on_expiry", "expire") == "force_deliver"
