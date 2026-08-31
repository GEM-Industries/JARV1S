"""Pydantic models for per-device WebSocket auth."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

DeviceKind = Literal["browser", "desktop", "phone", "satellite"]


def device_kind_override_for_client_surface(
    client_surface: str | None,
) -> DeviceKind | None:
    """Map an explicit client bootstrap hint to a durable device kind at pair time."""
    if client_surface == "desktop_app":
        return "desktop"
    if client_surface == "phone":
        return "phone"
    if client_surface == "satellite":
        return "satellite"
    return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DeviceLocation(BaseModel):
    provider: Literal["manual", "home_assistant", "unknown"] = "unknown"
    room_id: str | None = None
    room_name: str | None = None
    ha_area_id: str | None = None
    ha_device_id: str | None = None
    ha_entity_id: str | None = None


class DeviceCredentialRecord(BaseModel):
    device_id: str
    owner_id: str
    node_id: str
    node_label: str | None = None
    capabilities: list[str] = Field(
        default_factory=lambda: ["mic", "speaker", "display"]
    )
    location: DeviceLocation = Field(default_factory=DeviceLocation)
    token_hash: str
    kind: DeviceKind = "browser"
    revoked_at: datetime | None = None
    disconnected_at: datetime | None = None
    created_at: datetime = Field(default_factory=_utcnow)
    last_seen_at: datetime | None = None


class DeviceCredentialSummary(BaseModel):
    device_id: str
    owner_id: str
    node_id: str
    node_label: str | None = None
    capabilities: list[str]
    location: DeviceLocation
    kind: DeviceKind
    revoked_at: datetime | None = None
    disconnected_at: datetime | None = None
    created_at: datetime
    last_seen_at: datetime | None = None


class DeviceAuthResult(BaseModel):
    device_id: str
    owner_id: str
    node_id: str
    node_label: str | None = None
    capabilities: list[str]
    location: DeviceLocation
    kind: DeviceKind


class PairingCodeIssueResult(BaseModel):
    code: str
    expires_at: datetime
    owner_id: str


class PairConsumeRequest(BaseModel):
    code: str = Field(max_length=32)
    node_id: str = Field(max_length=128)
    node_label: str | None = Field(default=None, max_length=80)
    capabilities: str | None = Field(default=None, max_length=256)
    client_surface: Literal["browser", "desktop_app", "phone", "satellite"] | None = None
    location_provider: str | None = Field(default=None, max_length=32)
    room_id: str | None = Field(default=None, max_length=128)
    room_name: str | None = Field(default=None, max_length=128)
    ha_area_id: str | None = Field(default=None, max_length=128)
    ha_device_id: str | None = Field(default=None, max_length=128)
    ha_entity_id: str | None = Field(default=None, max_length=128)


class PairConsumeResponse(BaseModel):
    device_id: str
    owner_id: str
    node_id: str
    device_token: str | None = None


class PairConsumeResult(PairConsumeResponse):
    device_token: str


class PairingCodeIssueRequest(BaseModel):
    node_label: str | None = Field(default=None, max_length=80)
    capabilities: list[str] | None = Field(default=None, max_length=16)
    room_name: str | None = Field(default=None, max_length=128)
    node_id: str | None = Field(default=None, max_length=128)
    ha_area_id: str | None = Field(default=None, max_length=128)


class PairingCodeIssueResponse(BaseModel):
    code: str
    expires_at: datetime
    owner_id: str
    pairing_url: str | None = None


class WsTicketRequest(BaseModel):
    device_token: str | None = Field(default=None, max_length=512)


class WsTicketResponse(BaseModel):
    ticket: str
    expires_at: datetime


class SatelliteCredentialCreateRequest(BaseModel):
    node_label: str | None = Field(default=None, max_length=80)
    node_id: str | None = Field(default=None, max_length=128)
    ha_area_id: str | None = Field(default=None, max_length=128)
    room_name: str | None = Field(default=None, max_length=128)
    capabilities: list[str] | None = Field(default=None, max_length=16)


class SatelliteCredentialCreateResponse(BaseModel):
    device_id: str
    node_id: str
    node_label: str | None = None
    device_token: str
    backend_ws_url: str
