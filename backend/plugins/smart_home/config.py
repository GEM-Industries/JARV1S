"""Resolved Home Assistant connection — product config + credential boundary."""

from __future__ import annotations

import logging
from typing import Any

from core.credentials.store import credential_store
from plugins.smart_home.ha_client import (
    HA_TOKEN_CLIENT_NAME,
    HomeAssistantClient,
    normalize_ha_url,
)
from plugins.smart_home.status import check_liveness

logger = logging.getLogger(__name__)

_HA_CONFIG_KEY = "ha_config"


class HaConfigStore:
    def __init__(self) -> None:
        self._cache: dict[str, Any] | None = None

    async def load_persisted(self) -> dict[str, Any] | None:
        if self._cache is not None:
            return self._cache
        try:
            from services.database.mongodb import mongodb

            col = mongodb.get_collection("system_config")
            doc = await col.find_one({"_id": _HA_CONFIG_KEY})
            if not doc:
                return None
            if doc.get("disconnected"):
                self._cache = {"disconnected": True}
                return self._cache
            url = str(doc.get("url") or "").strip()
            if not url:
                return None
            self._cache = {"url": url, "disconnected": False}
            return self._cache
        except Exception:
            return None

    async def save(self, *, url: str) -> None:
        from services.database.mongodb import mongodb

        normalized = normalize_ha_url(url)
        payload = {"url": normalized, "disconnected": False}
        col = mongodb.get_collection("system_config")
        await col.update_one({"_id": _HA_CONFIG_KEY}, {"$set": payload}, upsert=True)
        self._cache = payload

    async def clear(self) -> None:
        """Mark product connection removed so env fallback cannot resurrect it."""
        from services.database.mongodb import mongodb

        payload = {"disconnected": True, "url": ""}
        col = mongodb.get_collection("system_config")
        await col.update_one({"_id": _HA_CONFIG_KEY}, {"$set": payload}, upsert=True)
        self._cache = {"disconnected": True}

    def is_disconnected(self) -> bool:
        return bool(self._cache and self._cache.get("disconnected"))

    def cached_url(self) -> str | None:
        if not self._cache or self._cache.get("disconnected"):
            return None
        url = self._cache.get("url")
        return str(url) if url else None

    def clear_cache(self) -> None:
        self._cache = None


ha_config_store = HaConfigStore()


def resolve_ha_connection_sync() -> tuple[str | None, str | None]:
    if ha_config_store.is_disconnected():
        return None, None
    url = ha_config_store.cached_url()
    if not url:
        url = credential_store.get_secret("HA_URL")
    token = credential_store.get_secret("HA_TOKEN")
    return url, token


async def resolve_ha_connection() -> tuple[str | None, str | None]:
    await ha_config_store.load_persisted()
    return resolve_ha_connection_sync()


def is_ha_configured() -> bool:
    url, token = resolve_ha_connection_sync()
    return bool(url and token)


def resolve_ha_connection_config() -> dict[str, str | None]:
    url, token = resolve_ha_connection_sync()
    return {"HA_URL": url, "HA_TOKEN": token}


async def persist_ha_connection(url: str, token: str) -> None:
    from core.integrations.manager import integrations

    normalized = normalize_ha_url(url)
    await ha_config_store.save(url=normalized)
    credential_store.set_secret("HA_TOKEN", token.strip())
    await integrations.reset("smart_home")


async def clear_ha_connection() -> None:
    """Revoke the HA-side JARV1S token when possible, then clear local credentials."""
    from core.integrations.manager import integrations

    url, token = await resolve_ha_connection()
    if url and token:
        client = HomeAssistantClient(base_url=url, token=token)
        try:
            await client.delete_long_lived_access_tokens(HA_TOKEN_CLIENT_NAME)
        except Exception as exc:
            logger.warning("Could not revoke Home Assistant token on disconnect: %s", exc)
        finally:
            await client.aclose()

    await ha_config_store.clear()
    credential_store.delete_secret("HA_TOKEN")
    await integrations.reset("smart_home")


async def validate_ha_connection(url: str, token: str):
    normalized = normalize_ha_url(url)
    return await check_liveness(normalized, token.strip())
