"""Transport-neutral external ingress configuration.

Packaged Host persists the public callback base URL in Mongo system_config.
Contributor/custom deployments may override with EXTERNAL_INGRESS_BASE_URL.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from core import settings
from core.credentials.store import credential_store

logger = logging.getLogger(__name__)

_EXTERNAL_INGRESS_KEY = "external_ingress"
IngressProvider = Literal["tailscale_funnel", "custom", "none"]


class ExternalIngressState(BaseModel):
    enabled: bool = False
    provider: IngressProvider = "none"
    base_url: Optional[str] = None
    composio_subscription_ok: bool = False
    secret_present: bool = False
    push_channels_ok: bool = False
    last_error: Optional[str] = None
    last_received_at: Optional[datetime] = None
    last_reconciled_at: Optional[datetime] = None
    inbox_pending: int = 0
    inbox_dead_letter: int = 0
    detail: Optional[str] = None


class ExternalIngressUpdate(BaseModel):
    enabled: bool = True
    provider: IngressProvider = "tailscale_funnel"
    base_url: Optional[str] = Field(
        default=None,
        description="Public HTTPS origin with no trailing path, e.g. https://host.ts.net",
    )


class ExternalIngressStore:
    """Mongo-backed product config with an in-process cache.

    ``save`` merges into the cache so partial updates cannot drop fields such as
    ``last_received_at``. Mongo ``$set`` already merges at the document level.
    """

    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache
        try:
            from services.database.mongodb import mongodb

            col = mongodb.get_collection("system_config")
            doc = await col.find_one({"_id": _EXTERNAL_INGRESS_KEY})
            if not doc:
                self._cache = {}
                return self._cache
            self._cache = {k: v for k, v in doc.items() if k != "_id"}
            return self._cache
        except Exception:
            logger.debug("Failed to load external_ingress config", exc_info=True)
            self._cache = {}
            return self._cache

    async def save(self, payload: dict[str, Any]) -> None:
        from services.database.mongodb import mongodb

        col = mongodb.get_collection("system_config")
        await col.update_one({"_id": _EXTERNAL_INGRESS_KEY}, {"$set": payload}, upsert=True)
        merged = dict(self._cache or {})
        merged.update(payload)
        self._cache = merged

    async def clear(self) -> None:
        cleared = {
            "enabled": False,
            "provider": "none",
            "base_url": None,
            "last_error": None,
            "last_received_at": None,
            "composio_subscription_ok": False,
            "push_channels_ok": False,
        }
        await self.save(cleared)
        self._cache = dict(cleared)

    def clear_cache(self) -> None:
        self._cache = None


external_ingress_store = ExternalIngressStore()


def _normalize_base_url(url: str | None) -> str | None:
    if not url:
        return None
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return None
    return cleaned


def resolve_external_ingress_base_url_sync() -> str | None:
    """Resolve public callback origin without Mongo (packaged uses cache/env)."""
    env_override = getattr(settings, "EXTERNAL_INGRESS_BASE_URL", None)
    if isinstance(env_override, str) and env_override.strip():
        return _normalize_base_url(env_override)

    cached = external_ingress_store._cache or {}
    if cached.get("enabled") and cached.get("base_url"):
        return _normalize_base_url(str(cached["base_url"]))
    return None


async def resolve_external_ingress_base_url() -> str | None:
    await external_ingress_store.load()
    return resolve_external_ingress_base_url_sync()


async def mark_event_received() -> None:
    await external_ingress_store.save({"last_received_at": datetime.now(timezone.utc)})


async def get_external_ingress_state() -> ExternalIngressState:
    from services.inbound_events import inbound_event_service

    doc = await external_ingress_store.load()
    base_url = await resolve_external_ingress_base_url()
    secret_present = bool(
        credential_store.get_secret("COMPOSIO_WEBHOOK_SECRET")
        or settings.COMPOSIO_WEBHOOK_SECRET
    )
    stats = await inbound_event_service.stats()
    enabled = bool(doc.get("enabled") and base_url)

    if not enabled:
        detail = "Off — calendar and Gmail automations still poll every minute."
    elif doc.get("last_error"):
        detail = f"Needs attention — {doc['last_error']}"
    elif not doc.get("last_received_at"):
        detail = "Configured — waiting for first external event."
    else:
        detail = "Verified — at least one external event has been received."

    return ExternalIngressState(
        enabled=enabled,
        provider=doc.get("provider") or ("custom" if base_url else "none"),
        base_url=base_url,
        composio_subscription_ok=bool(doc.get("composio_subscription_ok")),
        secret_present=secret_present,
        push_channels_ok=bool(doc.get("push_channels_ok")),
        last_error=doc.get("last_error"),
        last_received_at=doc.get("last_received_at"),
        last_reconciled_at=doc.get("last_reconciled_at"),
        inbox_pending=stats.pending + stats.retry + stats.processing,
        inbox_dead_letter=stats.dead_letter,
        detail=detail,
    )


async def configure_external_ingress(update: ExternalIngressUpdate) -> ExternalIngressState:
    """Persist ingress config and reconcile provider subscriptions/channels."""
    base_url = _normalize_base_url(update.base_url)
    if update.enabled and not base_url:
        raise ValueError("base_url is required when enabling external ingress")

    if not update.enabled or not base_url:
        await _disable_providers()
        await external_ingress_store.clear()
        return await get_external_ingress_state()

    await external_ingress_store.save(
        {
            "enabled": True,
            "provider": update.provider,
            "base_url": base_url,
            "last_error": None,
            "last_reconciled_at": datetime.now(timezone.utc),
        }
    )

    composio_ok = False
    push_ok = False
    last_error: str | None = None
    try:
        from core.integrations.composio_gateway import get_composio_gateway

        gateway = get_composio_gateway()
        if gateway:
            gateway.set_callback_host(base_url)
            await gateway.ensure_webhook_subscription()
            composio_ok = bool(gateway.webhook_secret)
    except Exception as exc:
        last_error = f"Composio subscription failed: {exc}"
        logger.warning("%s", last_error)

    try:
        from services.push.registry import push_registry

        await push_registry.reregister_all()
        push_ok = True
    except Exception as exc:
        msg = f"Push channel registration failed: {exc}"
        last_error = f"{last_error}; {msg}" if last_error else msg
        logger.warning("%s", msg)

    await external_ingress_store.save(
        {
            "composio_subscription_ok": composio_ok,
            "push_channels_ok": push_ok,
            "last_error": last_error,
            "last_reconciled_at": datetime.now(timezone.utc),
        }
    )
    return await get_external_ingress_state()


async def _disable_providers() -> None:
    try:
        from core.integrations.composio_gateway import get_composio_gateway

        gateway = get_composio_gateway()
        if gateway:
            await gateway.clear_webhook_subscription()
    except Exception:
        logger.warning("Failed to clear Composio webhook subscription", exc_info=True)
    try:
        from services.push.registry import push_registry

        await push_registry.teardown_all_channels()
    except Exception:
        logger.warning("Failed to teardown push channels", exc_info=True)
