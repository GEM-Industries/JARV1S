"""
OAuth credential models for bespoke integrations.

ProviderConfig holds the OAuth app credentials (client_id, client_secret) for
a provider — one document per provider in `oauth_provider_configs`.

OAuthToken holds the issued token for a user account — one document per
(provider, account_email) pair in `oauth_tokens`.

Keeping app credentials separate from tokens means changing the registered
OAuth app never requires touching token documents.
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ProviderConfig(BaseModel):
    provider: str                        # "google" | "microsoft"
    client_id: str
    client_secret: Optional[str] = None  # None for Microsoft public client (Device Code)
    token_uri: str
    auth_uri: str


class OAuthToken(BaseModel):
    provider: str                        # "google" | "microsoft"
    account_email: str                   # "user@gmail.com"
    access_token: str
    refresh_token: str
    token_expiry: datetime               # UTC
    granted_scopes: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=_utcnow)
    last_refreshed_at: datetime = Field(default_factory=_utcnow)
