"""Credential management API models."""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class CredentialCardStatus(str, Enum):
    MISSING = "missing"
    STORED = "stored"
    ENV_DEPRECATED = "env_deprecated"


class CredentialCard(BaseModel):
    id: str
    label: str
    description: str
    secret_name: str
    status: CredentialCardStatus
    source: Optional[str] = None
    masked_suffix: Optional[str] = None
    next_action: Optional[str] = None
    detail: Optional[str] = None
    docs_url: Optional[str] = None
    docs_label: Optional[str] = None


class ExternalTriggersStatus(BaseModel):
    enabled: bool = False
    base_url: str = ""
    provider: str = "none"
    last_received_at: Optional[str] = None
    inbox_pending: int = 0
    inbox_dead_letter: int = 0
    last_error: Optional[str] = None
    detail: str = ""


class CredentialsListResponse(BaseModel):
    items: list[CredentialCard] = Field(default_factory=list)
    external_triggers: ExternalTriggersStatus


class CredentialUpsertRequest(BaseModel):
    value: str


class CredentialActionResult(BaseModel):
    ok: bool
    message: str
    card: CredentialCard


class CredentialValidationResult(BaseModel):
    ok: bool
    message: str
