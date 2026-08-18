"""Google/Microsoft OAuth provider metadata and config resolution."""

from __future__ import annotations

from typing import Literal, Optional

from core import settings
from core.auth.models import ProviderConfig

ConfigMode = Literal["product", "self_managed"]

PROVIDER_URIS: dict[str, dict[str, str]] = {
    "google": {
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "userinfo_uri": "https://www.googleapis.com/oauth2/v1/userinfo",
    },
    "microsoft": {
        "token_uri": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        "auth_uri": "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize",
        "userinfo_uri": "https://graph.microsoft.com/v1.0/me",
    },
}

BASE_SCOPES: dict[str, set[str]] = {
    "google": {"openid", "https://www.googleapis.com/auth/userinfo.email"},
    "microsoft": {"offline_access", "openid", "profile"},
}

BUILTIN_PROVIDERS = frozenset(PROVIDER_URIS)


def product_client_metadata(provider: str) -> Optional[tuple[str, Optional[str]]]:
    """Return (client_id, client_secret) from product env when configured."""
    if provider == "google":
        client_id = settings.GOOGLE_OAUTH_CLIENT_ID
        if client_id:
            return client_id, settings.GOOGLE_OAUTH_CLIENT_SECRET
    elif provider == "microsoft":
        client_id = settings.MICROSOFT_OAUTH_CLIENT_ID
        if client_id:
            return client_id, None
    return None


def provider_config_from_product(provider: str) -> Optional[ProviderConfig]:
    meta = product_client_metadata(provider)
    if not meta:
        return None
    client_id, client_secret = meta
    uris = PROVIDER_URIS[provider]
    return ProviderConfig(
        provider=provider,
        client_id=client_id,
        client_secret=client_secret,
        token_uri=uris["token_uri"],
        auth_uri=uris["auth_uri"],
    )


def has_product_metadata(provider: str) -> bool:
    return product_client_metadata(provider) is not None


async def resolve_provider_config(provider: str) -> tuple[ProviderConfig, ConfigMode]:
    """Product metadata first, then stored self-managed ProviderConfig."""
    product = provider_config_from_product(provider)
    if product:
        return product, "product"

    from core.auth.manager import auth_manager

    stored = await auth_manager.get_provider_config(provider)
    return stored, "self_managed"


async def is_connectable(provider: str) -> bool:
    if has_product_metadata(provider):
        return True
    try:
        from core.auth.manager import auth_manager

        await auth_manager.get_provider_config(provider)
        return True
    except KeyError:
        return False


def scopes_for_provider(provider: str, registered: list[str]) -> list[str]:
    base = BASE_SCOPES.get(provider, set())
    return sorted(base | set(registered))
