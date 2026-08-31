"""
AuthManager — OAuth grant custody for bespoke integrations.

Mongo `oauth_provider_configs` holds the OAuth app registration (client_id,
optional client_secret). Mongo `oauth_tokens` holds non-secret grant metadata
(one document per provider). Access and refresh tokens live in CredentialStore.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from core.auth.exceptions import ScopeGapError
from core.auth.models import OAuthToken, ProviderConfig
from core.credentials.store import credential_store
from core.integrations.manager import NeedsReauth

logger = logging.getLogger(__name__)

_REFRESH_BUFFER_SECS = 300
_VAULT_KEY_PREFIX = "oauth."
_SECRET_FIELDS = ("access_token", "refresh_token")
_METADATA_FIELDS = (
    "provider",
    "account_email",
    "token_expiry",
    "granted_scopes",
    "created_at",
    "last_refreshed_at",
)


def _vault_key(provider: str) -> str:
    return f"{_VAULT_KEY_PREFIX}{provider}"


def _is_terminal_refresh_error(exc: httpx.HTTPStatusError) -> bool:
    if exc.response.status_code != 400:
        return False
    error = ""
    try:
        error = str(exc.response.json().get("error") or "")
    except Exception:
        pass
    if error == "invalid_grant":
        return True
    return "invalid_grant" in (exc.response.text or "").lower()


class AuthManager:
    def __init__(self) -> None:
        self._token_cache: dict[str, OAuthToken] = {}
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, provider: str) -> asyncio.Lock:
        lock = self._refresh_locks.get(provider)
        if lock is None:
            lock = asyncio.Lock()
            self._refresh_locks[provider] = lock
        return lock

    # ------------------------------------------------------------------
    # Provider config
    # ------------------------------------------------------------------

    async def get_provider_config(self, provider: str) -> ProviderConfig:
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_provider_configs"]
        doc = await col.find_one({"provider": provider})
        if not doc:
            raise KeyError(f"No OAuth app configured for '{provider}'.")
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

    def _read_vault(self, provider: str) -> dict[str, str]:
        raw = credential_store.get_stored_secret(_vault_key(provider))
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Ignoring corrupt OAuth vault payload for '%s'", provider)
            return {}
        if not isinstance(payload, dict):
            return {}
        return {str(k): str(v) for k, v in payload.items() if v}

    def _write_vault(self, provider: str, secrets: dict[str, str]) -> None:
        credential_store.set_secret(_vault_key(provider), json.dumps(secrets))

    async def store_token(self, token: OAuthToken) -> None:
        existing = self._read_vault(token.provider)
        refresh_token = token.refresh_token or existing.get("refresh_token") or ""
        stored = token.model_copy(update={"refresh_token": refresh_token})
        self._write_vault(
            stored.provider,
            {
                "access_token": stored.access_token,
                "refresh_token": stored.refresh_token,
            },
        )

        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_tokens"]
        metadata = {field: getattr(stored, field) for field in _METADATA_FIELDS}
        await col.delete_many({"provider": stored.provider})
        await col.update_one(
            {"provider": stored.provider},
            {"$set": metadata},
            upsert=True,
        )
        self._token_cache[stored.provider] = stored
        logger.info("Stored token for provider '%s' (%s)", stored.provider, stored.account_email)

    async def clear_grant(self, provider: str) -> None:
        """Remove the user grant. Leaves the OAuth app registration in place."""
        self._token_cache.pop(provider, None)
        credential_store.delete_secret(_vault_key(provider))
        from services.database.mongodb import mongodb
        await mongodb.db["oauth_tokens"].delete_many({"provider": provider})
        logger.info("Cleared OAuth grant for '%s'", provider)

    async def peek_grant(self, provider: str) -> Optional[OAuthToken]:
        """Return the stored grant without refreshing. None if disconnected."""
        cached = self._token_cache.get(provider)
        if cached:
            return cached
        try:
            return await self._assemble_from_store(provider)
        except NeedsReauth:
            return None

    # ------------------------------------------------------------------
    # Token retrieval
    # ------------------------------------------------------------------

    async def _assemble_from_store(self, provider: str) -> OAuthToken:
        from services.database.mongodb import mongodb
        col = mongodb.db["oauth_tokens"]
        doc = await col.find_one({"provider": provider})
        if not doc:
            raise NeedsReauth(provider)

        doc = dict(doc)
        doc.pop("_id", None)
        mongo_access = doc.pop("access_token", None)
        mongo_refresh = doc.pop("refresh_token", None)
        secrets = self._read_vault(provider)

        if mongo_access or mongo_refresh:
            if not secrets.get("access_token") and mongo_access:
                secrets = {
                    "access_token": str(mongo_access),
                    "refresh_token": str(mongo_refresh or secrets.get("refresh_token") or ""),
                }
                self._write_vault(provider, secrets)
            await col.update_one(
                {"provider": provider},
                {"$unset": {field: "" for field in _SECRET_FIELDS}},
            )

        if not secrets.get("access_token") or "token_expiry" not in doc:
            raise NeedsReauth(provider)

        token = OAuthToken(
            provider=provider,
            account_email=doc.get("account_email") or "",
            access_token=secrets["access_token"],
            refresh_token=secrets.get("refresh_token") or "",
            token_expiry=doc["token_expiry"],
            granted_scopes=list(doc.get("granted_scopes") or []),
            created_at=doc.get("created_at") or datetime.now(timezone.utc),
            last_refreshed_at=doc.get("last_refreshed_at") or datetime.now(timezone.utc),
        )
        self._token_cache[provider] = token
        return token

    async def get_token(self, provider: str) -> OAuthToken:
        cached = self._token_cache.get(provider)
        if cached and not self._needs_refresh(cached):
            return cached
        if cached is None:
            token = await self._assemble_from_store(provider)
            if not self._needs_refresh(token):
                return token
        return await self.refresh_token(provider)

    # ------------------------------------------------------------------
    # Scope validation
    # ------------------------------------------------------------------

    async def ensure_scopes(self, provider: str, required: list[str]) -> OAuthToken:
        token = await self.get_token(provider)
        granted = set(token.granted_scopes)
        missing = [s for s in required if s not in granted]
        if missing:
            raise ScopeGapError(provider, missing)
        return token

    # ------------------------------------------------------------------
    # Token refresh
    # ------------------------------------------------------------------

    async def refresh_token(self, provider: str) -> OAuthToken:
        from core.auth.providers import BUILTIN_PROVIDERS

        if provider not in BUILTIN_PROVIDERS:
            raise NeedsReauth(provider)
        async with self._lock_for(provider):
            token = self._token_cache.get(provider)
            if token is None:
                token = await self._assemble_from_store(provider)
            if not self._needs_refresh(token):
                return token
            return await self._exchange_refresh(token)

    async def _exchange_refresh(self, token: OAuthToken) -> OAuthToken:
        from core.auth.providers import resolve_provider_config

        config, _ = await resolve_provider_config(token.provider)
        payload: dict[str, str] = {
            "client_id": config.client_id,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
        if token.provider == "google" and config.client_secret:
            payload["client_secret"] = config.client_secret
        if token.provider == "microsoft":
            payload["scope"] = " ".join(token.granted_scopes)

        logger.info("Refreshing %s access token...", token.provider)
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(config.token_uri, data=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as e:
            if _is_terminal_refresh_error(e):
                logger.error("%s token refresh rejected: %s", token.provider, e.response.text)
                await self.clear_grant(token.provider)
                raise NeedsReauth(token.provider) from e
            logger.error("%s token refresh failed: %s", token.provider, e.response.text)
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            logger.error("%s token refresh transport error", token.provider)
            raise

        now = datetime.now(timezone.utc)
        expires_in = data.get("expires_in", 3600)
        new_scopes = (
            data.get("scope", "").split() if data.get("scope") else token.granted_scopes
        )
        refreshed = token.model_copy(
            update={
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token") or token.refresh_token,
                "token_expiry": now + timedelta(seconds=expires_in),
                "granted_scopes": new_scopes or token.granted_scopes,
                "last_refreshed_at": now,
            }
        )
        await self.store_token(refreshed)
        logger.info("%s access token refreshed successfully", token.provider)
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


auth_manager = AuthManager()
