"""Pydantic models for presence visibility REST surfaces."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from core.auth.device_models import DeviceKind

PresenceNodeStatus = Literal["online", "offline"]


class PresenceCore(BaseModel):
    name: str


class PresenceNode(BaseModel):
    node_id: str
    node_label: str | None = None
    kind: DeviceKind | Literal["unknown"]
    status: PresenceNodeStatus
    capabilities: list[str] = Field(default_factory=list)
    room_name: str | None = None
    ha_area_id: str | None = None
    last_seen_at: datetime | None = None
    active: bool = False
    device_id: str | None = None
    disconnected: bool = False


class PresenceView(BaseModel):
    core: PresenceCore
    nodes: list[PresenceNode] = Field(default_factory=list)


class RevokeDeviceResponse(BaseModel):
    revoked: bool


class DisconnectDeviceResponse(BaseModel):
    disconnected: bool


class ResumeDeviceResponse(BaseModel):
    resumed: bool


class AssignNodeRoomRequest(BaseModel):
    ha_area_id: str | None = None
