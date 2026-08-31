"""OAuth provider metadata and config resolution."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Literal, Optional

from core.auth.models import ProviderConfig

logger = logging.getLogger(__name__)

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
    "spotify": {
        "token_uri": "https://accounts.spotify.com/api/token",
        "auth_uri": "https://accounts.spotify.com/authorize",
        "userinfo_uri": "https://api.spotify.com/v1/me",
    },
}

BASE_SCOPES: dict[str, set[str]] = {
    "google": {"openid", "https://www.googleapis.com/auth/userinfo.email"},
    "microsoft": {"offline_access", "openid", "profile"},
    "spotify": {"user-read-email"},
}

BUILTIN_PROVIDERS = frozenset(PROVIDER_URIS)

_PRODUCT_OAUTH_ENV = "JARVIS_PRODUCT_OAUTH"
_bundled_clients_cache: tuple[str, dict[str, Any]] | None = None


def _bundled_clients() -> dict[str, Any]:
    global _bundled_clients_cache
    path = os.environ.get(_PRODUCT_OAUTH_ENV, "").strip()
    if _bundled_clients_cache is not None and _bundled_clients_cache[0] == path:
        return _bundled_clients_cache[1]
    if not path:
        data: dict[str, Any] = {}
    else:
        try:
            parsed = json.loads(Path(path).read_text(encoding="utf-8"))
            data = parsed if isinstance(parsed, dict) else {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read product OAuth identity from %s: %s", path, exc)
            data = {}
    _bundled_clients_cache = (path, data)
    return data


def product_client_metadata(provider: str) -> Optional[tuple[str, Optional[str]]]:
    """Return (client_id, client_secret) from the official app bundle when present."""
    if provider not in PROVIDER_URIS:
        return None
    entry = _bundled_clients().get(provider)
    if not isinstance(entry, dict):
        return None
    client_id = str(entry.get("client_id") or "").strip()
    if not client_id:
        return None
    secret = entry.get("client_secret")
    client_secret = str(secret).strip() if secret else None
    return client_id, client_secret or None


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
    try:
        await resolve_provider_config(provider)
        return True
    except KeyError:
        return False


def scopes_for_provider(provider: str, registered: list[str]) -> list[str]:
    base = BASE_SCOPES.get(provider, set())
    return sorted(base | set(registered))
