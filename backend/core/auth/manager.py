"""
AuthManager — central OAuth credential lifecycle for bespoke integrations.

Owns two MongoDB collections:
  oauth_provider_configs  — one document per provider (client_id, client_secret, etc.)
  oauth_tokens            — one document per (provider, account_email)

Each token is cached in-memory after first load to avoid a MongoDB round-trip
on every tool call. The cache is invalidated on every store_token() call.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from core.auth.exceptions import ScopeGapError
from core.auth.models import OAuthToken, ProviderConfig
from core.integrations.manager import NeedsReauth

logger = logging.getLogger(__name__)

# Seconds before expiry to proactively refresh
_REFRESH_BUFFER_SECS = 300


class AuthManager:
    def __init__(self) -> None:
        self._token_cache: dict[str, OAuthToken] = {}  # keyed by provider

    # ------------------------------------------------------------------
    # Provider config
    # ------------------------------------------------------------------

    async def get_provider_config(self, provider: str) -> ProviderConfig:
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_provider_configs"]
        doc = await col.find_one({"provider": provider})
        if not doc:
            raise KeyError(f"No provider config found for '{provider}'. Run the setup command.")
        doc.pop("_id", None)
        return ProviderConfig(**doc)

    async def store_provider_config(self, config: ProviderConfig) -> None:
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_provider_configs"]
        await col.update_one(
            {"provider": config.provider},
            {"$set": config.model_dump()},
            upsert=True,
        )
        logger.info("Stored provider config for '%s'", config.provider)

    # ------------------------------------------------------------------
    # Token storage
    # ------------------------------------------------------------------

    async def store_token(self, token: OAuthToken) -> None:
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_tokens"]
        doc = token.model_dump()
        await col.update_one(
            {"provider": token.provider, "account_email": token.account_email},
            {"$set": doc},
            upsert=True,
        )
        self._token_cache[token.provider] = token
        logger.info("Stored token for provider '%s' (%s)", token.provider, token.account_email)

    async def delete_provider(self, provider: str) -> None:
        from services.database.mongodb import mongodb
        await mongodb.db["oauth_provider_configs"].delete_many({"provider": provider})
        await mongodb.db["oauth_tokens"].delete_many({"provider": provider})
        self._token_cache.pop(provider, None)
        logger.info("Deleted all OAuth data for provider '%s'", provider)

    # ------------------------------------------------------------------
    # Token retrieval
    # ------------------------------------------------------------------

    async def get_token(self, provider: str) -> OAuthToken:
        # 1. In-memory cache
        cached = self._token_cache.get(provider)
        if cached:
            if self._needs_refresh(cached):
                return await self.refresh_token(provider)
            return cached

        # 2. MongoDB
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_tokens"]
        doc = await col.find_one({"provider": provider})
        if doc:
            doc.pop("_id", None)
            token = OAuthToken(**doc)
            self._token_cache[provider] = token
            if self._needs_refresh(token):
                return await self.refresh_token(provider)
            return token

        # 3. MongoDB miss — token not found
        raise NeedsReauth(provider)

    # ------------------------------------------------------------------
    # Scope validation
    # ------------------------------------------------------------------

    async def ensure_scopes(self, provider: str, required: list[str]) -> OAuthToken:
        token = await self.get_token(provider)
        # Normalize: strip URL prefix for comparison (e.g. both full and short forms)
        granted = {s.rstrip("/").split("/")[-1] for s in token.granted_scopes}
        granted_full = set(token.granted_scopes)
        missing = [
            s for s in required
            if s not in granted_full and s.rstrip("/").split("/")[-1] not in granted
        ]
        if missing:
            raise ScopeGapError(provider, missing)
        return token

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_token(self, provider: str) -> OAuthToken:
        if provider == "google":
            return await self._refresh_google()
        if provider == "microsoft":
            return await self._refresh_microsoft()
        raise NeedsReauth(provider)

    async def _refresh_google(self) -> OAuthToken:
        cached = self._token_cache.get("google")
        if not cached:
            from services.database.mongodb import mongodb
            col = mongodb.db["oauth_tokens"]
            doc = await col.find_one({"provider": "google"})
            if not doc:
                raise NeedsReauth("google")
            doc.pop("_id", None)
            cached = OAuthToken(**doc)

        from core.auth.providers import resolve_provider_config

        config, _ = await resolve_provider_config("google")

        logger.info("Refreshing Google access token...")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    config.token_uri,
                    data={
                        "client_id": config.client_id,
                        "client_secret": config.client_secret,
                        "refresh_token": cached.refresh_token,
                        "grant_type": "refresh_token",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("Google token refresh failed: %s", e.response.text)
            raise NeedsReauth("google") from e
        except Exception as e:
            logger.error("Google token refresh error: %s", e)
            raise NeedsReauth("google") from e

        now = datetime.now(timezone.utc)
        expires_in = data.get("expires_in", 3600)
        token_expiry = now + timedelta(seconds=expires_in)

        # Google may return updated scopes on refresh
        new_scopes = data.get("scope", "").split() if data.get("scope") else cached.granted_scopes

        refreshed = cached.model_copy(update={
            "access_token": data["access_token"],
            "token_expiry": token_expiry,
            "granted_scopes": new_scopes or cached.granted_scopes,
            "last_refreshed_at": now,
        })

        await self.store_token(refreshed)
        logger.info("Google access token refreshed successfully")
        return refreshed

    async def _refresh_microsoft(self) -> OAuthToken:
        cached = self._token_cache.get("microsoft")
        if not cached:
            from services.database.mongodb import mongodb
            col = mongodb.db["oauth_tokens"]
            doc = await col.find_one({"provider": "microsoft"})
            if not doc:
                raise NeedsReauth("microsoft")
            doc.pop("_id", None)
            cached = OAuthToken(**doc)

        from core.auth.providers import resolve_provider_config

        config, _ = await resolve_provider_config("microsoft")

        # Direct POST — public clients don't send client_secret
        logger.info("Refreshing Microsoft access token...")
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    config.token_uri,
                    data={
                        "client_id": config.client_id,
                        "grant_type": "refresh_token",
                        "refresh_token": cached.refresh_token,
                        "scope": " ".join(cached.granted_scopes),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("Microsoft token refresh failed: %s", e.response.text)
            raise NeedsReauth("microsoft") from e
        except Exception as e:
            logger.error("Microsoft token refresh error: %s", e)
            raise NeedsReauth("microsoft") from e

        now = datetime.now(timezone.utc)
        expires_in = data.get("expires_in", 3600)

        refreshed = cached.model_copy(update={
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", cached.refresh_token),
            "token_expiry": now + timedelta(seconds=expires_in),
            "last_refreshed_at": now,
        })

        await self.store_token(refreshed)
        logger.info("Microsoft access token refreshed successfully")
        return refreshed

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _needs_refresh(token: OAuthToken) -> bool:
        now = datetime.now(timezone.utc)
        expiry = token.token_expiry
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        return now >= expiry - timedelta(seconds=_REFRESH_BUFFER_SECS)


# Global singleton
auth_manager = AuthManager()
