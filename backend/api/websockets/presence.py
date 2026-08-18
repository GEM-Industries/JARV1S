"""Presence identity for live WebSocket nodes.

This module keeps transport/device metadata at the WebSocket boundary. It does
not try to model households, rooms, users, or permissions; those can grow from
these stable references when Home Assistant or speaker identification is ready.

Durable policy uses `device_kind` from the authenticated credential or the
server-classified local bypass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Mapping, cast
from uuid import uuid4

from core.auth.device_models import DeviceAuthResult, DeviceKind
from core.config import settings

LocationProvider = Literal["manual", "home_assistant", "unknown"]

DEFAULT_CAPABILITIES = frozenset({"mic", "speaker", "display"})


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


def parse_capabilities(raw: str | None) -> frozenset[str]:
    """Parse comma-separated capabilities from the client handshake."""
    if not raw:
        return DEFAULT_CAPABILITIES
    values = frozenset(part.strip().lower() for part in raw.split(",") if part.strip())
    return values or DEFAULT_CAPABILITIES


@dataclass(frozen=True, slots=True)
class LocationRef:
    """Optional pointer to where a node lives.

    Home Assistant may become the source of truth later, so we store external
    references instead of creating a first-class room graph inside JARV1S.
    """

    provider: LocationProvider = "unknown"
    room_id: str | None = None
    room_name: str | None = None
    ha_area_id: str | None = None
    ha_device_id: str | None = None
    ha_entity_id: str | None = None

    @classmethod
    def from_values(
        cls,
        *,
        provider: str | None = None,
        room_id: str | None = None,
        room_name: str | None = None,
        ha_area_id: str | None = None,
        ha_device_id: str | None = None,
        ha_entity_id: str | None = None,
    ) -> "LocationRef":
        normalized_provider = _clean(provider) or "unknown"
        if normalized_provider not in {"manual", "home_assistant", "unknown"}:
            normalized_provider = "unknown"
        return cls(
            provider=cast(LocationProvider, normalized_provider),
            room_id=_clean(room_id),
            room_name=_clean(room_name),
            ha_area_id=_clean(ha_area_id),
            ha_device_id=_clean(ha_device_id),
            ha_entity_id=_clean(ha_entity_id),
        )

    def model_dump(self) -> dict[str, str | None]:
        return {
            "provider": self.provider,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "ha_area_id": self.ha_area_id,
            "ha_device_id": self.ha_device_id,
            "ha_entity_id": self.ha_entity_id,
        }


@dataclass(frozen=True, slots=True)
class PresenceIdentity:
    """Identity for a live mic/speaker/display endpoint."""

    connection_id: str
    owner_id: str
    node_id: str
    node_label: str | None
    capabilities: frozenset[str]
    device_kind: DeviceKind
    location: LocationRef

    @property
    def node_key(self) -> str:
        """Stable key for replacing the same owner's reconnecting node."""
        return f"{self.owner_id}:{self.node_id}"

    def model_dump(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "owner_id": self.owner_id,
            "node_id": self.node_id,
            "node_label": self.node_label,
            "capabilities": sorted(self.capabilities),
            "device_kind": self.device_kind,
            "location": self.location.model_dump(),
        }

    def context(self) -> dict[str, Any]:
        """Prompt/runtime-safe context for turns from this node."""
        return {
            "owner_id": self.owner_id,
            "connection_id": self.connection_id,
            "node_id": self.node_id,
            "node_label": self.node_label,
            "node_capabilities": sorted(self.capabilities),
            "device_kind": self.device_kind,
            "location_ref": self.location.model_dump(),
        }


def build_presence_from_auth(
    auth: DeviceAuthResult,
    values: Mapping[str, str | None],
    *,
    connection_id: str | None = None,
) -> PresenceIdentity:
    """Build trusted presence identity from an authenticated device record."""
    location = LocationRef(
        provider=auth.location.provider,
        room_id=auth.location.room_id,
        room_name=auth.location.room_name,
        ha_area_id=auth.location.ha_area_id,
        ha_device_id=auth.location.ha_device_id,
        ha_entity_id=auth.location.ha_entity_id,
    )
    return PresenceIdentity(
        connection_id=connection_id or f"conn-{uuid4().hex[:12]}",
        owner_id=auth.owner_id,
        node_id=auth.node_id,
        node_label=auth.node_label or _clean(values.get("node_label")),
        capabilities=frozenset(auth.capabilities),
        device_kind=auth.kind,
        location=location,
    )


def build_presence_identity(
    values: Mapping[str, str | None],
    *,
    connection_id: str | None = None,
    allow_owner_override: bool = False,
    device_kind: DeviceKind = "browser",
) -> PresenceIdentity:
    """Build presence identity for the server-trusted local bypass."""
    raw_owner_id = values.get("owner_id") if allow_owner_override else None
    location = LocationRef.from_values(
        provider=values.get("location_provider"),
        room_id=values.get("room_id"),
        room_name=values.get("room_name"),
        ha_area_id=values.get("ha_area_id"),
        ha_device_id=values.get("ha_device_id"),
        ha_entity_id=values.get("ha_entity_id"),
    )
    return PresenceIdentity(
        connection_id=connection_id or f"conn-{uuid4().hex[:12]}",
        owner_id=_clean(raw_owner_id) or settings.DEFAULT_USER_ID,
        node_id=_clean(values.get("node_id")) or "default",
        node_label=_clean(values.get("node_label")),
        capabilities=parse_capabilities(values.get("capabilities")),
        device_kind=device_kind,
        location=location,
    )
