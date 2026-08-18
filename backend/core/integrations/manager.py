"""
Integration Manager - The Gatekeeper for External Services.

Inspired by FastAPI's Depends() pattern, this centralizes:
1. Credential loading (CredentialStore with optional .env fallback)
2. Client lazy-initialization
3. OAuth token refresh via optional per-integration refresh hooks
4. Scope validation via AuthManager for OAuth-backed integrations
"""

import inspect
import logging
from typing import Any, Callable, Dict, Optional

from core.credentials.store import credential_store

logger = logging.getLogger(__name__)


class NeedsReauth(Exception):
    """Raised when an OAuth token cannot be refreshed and requires user re-authorization."""

    def __init__(self, integration: str):
        self.integration = integration
        super().__init__(f"Integration '{integration}' requires re-authorization.")


class IntegrationManager:
    """
    Centralized manager for external service clients.

    Usage:
        client = await integrations.get("weather")  # Lazy-loads and caches

    OAuth integrations:
        Register with provider= and required_scopes= to opt into AuthManager scope
        validation. The validated OAuthToken is injected into config["_oauth_token"]
        before the factory runs.

    Refresh hooks:
        Register an async refresh(client, config) callable. It is invoked before
        returning a cached client, allowing token refresh to be transparent.
        On failure it should raise NeedsReauth so the agent can speak a recovery message.
    """

    def __init__(self):
        self._config_keys: Dict[str, list[str]] = {}
        self._clients: Dict[str, Any] = {}
        self._factories: Dict[str, Callable] = {}
        self._refresh_hooks: Dict[str, Optional[Callable]] = {}
        self._providers: Dict[str, Optional[str]] = {}
        self._required_scopes: Dict[str, Optional[list]] = {}
        self._aux_scopes: Dict[str, set[str]] = {}
        self._aux_provider_integrations: Dict[str, set[str]] = {}
        self._auth_manager = None

    def set_auth_manager(self, auth_manager) -> None:
        """Wire in the AuthManager singleton after MongoDB connects."""
        self._auth_manager = auth_manager

    def register(
        self,
        name: str,
        factory: Callable,
        config_keys: list[str] = None,
        refresh: Optional[Callable] = None,
        provider: Optional[str] = None,
        required_scopes: Optional[list[str]] = None,
    ):
        self._factories[name] = factory
        self._refresh_hooks[name] = refresh
        self._providers[name] = provider
        self._required_scopes[name] = required_scopes
        self._config_keys[name] = list(config_keys or [])

    def _resolve_config(self, name: str) -> dict[str, Any]:
        if name == "smart_home":
            from plugins.smart_home.config import resolve_ha_connection_config

            return resolve_ha_connection_config()
        keys = self._config_keys.get(name, [])
        if not keys:
            return {}
        return {key: credential_store.get_secret(key) for key in keys}

    async def _close_client(self, client: Any) -> None:
        close = getattr(client, "aclose", None)
        if close is None:
            return
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            logger.warning("Failed to close integration client: %s", exc)

    async def _evict_client(self, name: str) -> None:
        client = self._clients.pop(name, None)
        if client is not None:
            await self._close_client(client)

    async def _run_refresh_hook(self, name: str) -> None:
        hook = self._refresh_hooks.get(name)
        if not hook:
            return
        config = self._resolve_config(name)
        try:
            if inspect.iscoroutinefunction(hook):
                await hook(self._clients[name], config)
            else:
                hook(self._clients[name], config)
        except NeedsReauth:
            await self._evict_client(name)
            raise
        except Exception as e:
            logger.warning("Refresh hook for '%s' failed: %s", name, e)
            await self._evict_client(name)
            raise NeedsReauth(name) from e

    async def get(self, name: str) -> Any:
        if name in self._clients:
            await self._run_refresh_hook(name)
            return self._clients[name]

        if name not in self._factories:
            raise KeyError(f"Integration '{name}' not registered")

        config = dict(self._resolve_config(name))

        provider = self._providers.get(name)
        required_scopes = self._required_scopes.get(name)
        if provider and required_scopes and self._auth_manager:
            token = await self._auth_manager.ensure_scopes(provider, required_scopes)
            config["_oauth_token"] = token

        try:
            factory = self._factories[name]
            if inspect.iscoroutinefunction(factory):
                client = await factory(config)
            else:
                client = factory(config)

            self._clients[name] = client
            logger.info("Initialized integration: %s", name)
        except Exception as e:
            logger.error("Failed to initialize %s: %s", name, e)
            raise

        await self._run_refresh_hook(name)
        return self._clients[name]

    def get_config(self, name: str, key: str, default: Any = None) -> Any:
        return self._resolve_config(name).get(key, default)

    def get_provider_name(self, name: str) -> Optional[str]:
        return self._providers.get(name)

    def resolve_oauth_provider(
        self,
        integration_name: str,
        exc: Exception | None = None,
    ) -> Optional[str]:
        """Map an integration or auth error to the OAuth provider widget to show."""
        from core.auth.exceptions import ScopeGapError

        provider = self._providers.get(integration_name)
        if isinstance(exc, ScopeGapError):
            return exc.provider
        if isinstance(exc, NeedsReauth):
            if exc.integration in self.get_bespoke_providers():
                return exc.integration
            if exc.integration == integration_name:
                providers = self.resolve_oauth_providers(integration_name)
                return providers[0] if providers else None
        if provider:
            return provider
        providers = self.resolve_oauth_providers(integration_name)
        if providers:
            return providers[0]
        return None

    def resolve_oauth_providers(self, integration_name: str) -> list[str]:
        """Return every OAuth provider that can authorize an integration."""
        provider = self._providers.get(integration_name)
        if provider:
            return [provider]
        if integration_name not in self._factories:
            return []
        providers = {
            provider_name
            for provider_name, integration_names in self._aux_provider_integrations.items()
            if integration_name in integration_names
        }
        if providers:
            return self._order_providers(providers)
        return []

    def _order_providers(self, providers: set[str]) -> list[str]:
        ordered: list[str] = []
        for candidate in ("google", "microsoft"):
            if candidate in providers:
                ordered.append(candidate)
        ordered.extend(sorted(providers - set(ordered)))
        return ordered

    def get_scopes_for_provider(self, provider: str) -> list[str]:
        scopes: set[str] = set()
        for name, prov in self._providers.items():
            if prov == provider and self._required_scopes.get(name):
                scopes.update(self._required_scopes[name])
        scopes.update(self._aux_scopes.get(provider, set()))
        return sorted(scopes)

    def register_aux_provider_scopes(
        self,
        provider: str,
        scopes: list[str],
        integration_name: str | None = None,
    ) -> None:
        if not scopes:
            return
        bucket = self._aux_scopes.setdefault(provider, set())
        bucket.update(scopes)
        if integration_name:
            integrations = self._aux_provider_integrations.setdefault(provider, set())
            integrations.add(integration_name)

    def get_bespoke_providers(self) -> set[str]:
        names = {p for p in self._providers.values() if p}
        names.update(self._aux_scopes.keys())
        return names

    def is_available(self, name: str) -> bool:
        if name not in self._factories:
            return False
        config = self._resolve_config(name)
        return all(v is not None for v in config.values()) if config else True

    async def reset(self, name: str | None = None) -> None:
        if name:
            await self._evict_client(name)
            return
        names = list(self._clients.keys())
        for integration_name in names:
            await self._evict_client(integration_name)

    async def unregister(self, name: str) -> None:
        await self._evict_client(name)
        self._factories.pop(name, None)
        self._refresh_hooks.pop(name, None)
        self._providers.pop(name, None)
        self._required_scopes.pop(name, None)
        self._config_keys.pop(name, None)

    async def shutdown(self) -> None:
        await self.reset()


# Global singleton instance
integrations = IntegrationManager()
