import logging
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Dict, Literal, Mapping, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# Global context for the current tool execution.
# `owner_id` is the durable storage namespace; live socket state is `connection_id`.
session_context: ContextVar[Dict[str, Any]] = ContextVar("session_context", default={})

GEO_FRESHNESS_TTL = timedelta(minutes=30)
GEO_FUTURE_SKEW = timedelta(minutes=5)
PLACEHOLDER_LOCATIONS = frozenset(
    {
        "current location",
        "my location",
        "near me",
        "here",
        "nearby",
        "this location",
        "my current location",
    }
)
_INTERACTIVE_DEVICE_KINDS = frozenset({"browser", "desktop", "phone"})


class GeoPosition(BaseModel):
    """Ephemeral device-reported geographic coordinates for the active session."""

    latitude: float
    longitude: float
    source: Literal["gps"] = "gps"
    accuracy_m: float | None = Field(default=None, ge=0)
    captured_at: datetime | None = None
    city: str | None = None

    @field_validator("latitude")
    @classmethod
    def _valid_latitude(cls, value: float) -> float:
        if not -90.0 <= value <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        return value

    @field_validator("longitude")
    @classmethod
    def _valid_longitude(cls, value: float) -> float:
        if not -180.0 <= value <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        return value

    @field_validator("captured_at", mode="before")
    @classmethod
    def _parse_captured_at(cls, value: Any) -> datetime | None:
        if value is None or value == "":
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        raise ValueError("captured_at must be an ISO datetime")

    def is_fresh(self, *, now: datetime | None = None, ttl: timedelta = GEO_FRESHNESS_TTL) -> bool:
        if self.captured_at is None:
            return True
        now = now or datetime.now(timezone.utc)
        age = now - self.captured_at
        return -GEO_FUTURE_SKEW <= age <= ttl

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "source": self.source,
        }
        if self.accuracy_m is not None:
            data["accuracy_m"] = self.accuracy_m
        if self.captured_at is not None:
            data["captured_at"] = self.captured_at.isoformat()
        if self.city:
            data["city"] = self.city
        return data


def parse_geo_position(raw: Any) -> GeoPosition | None:
    """Validate a client-provided location payload. Returns None for empty input."""
    if raw is None:
        return None
    if isinstance(raw, GeoPosition):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("location must be an object with latitude and longitude")
    return GeoPosition.model_validate(dict(raw))


def fresh_geo_position(
    raw: Any,
    *,
    now: datetime | None = None,
) -> GeoPosition | None:
    """Return a valid, fresh device position or None."""
    try:
        geo = parse_geo_position(raw)
    except ValueError:
        return None
    return geo if geo is not None and geo.is_fresh(now=now) else None


def is_placeholder_location(value: str | None) -> bool:
    if value is None:
        return True
    normalized = " ".join(str(value).strip().lower().split())
    return not normalized or normalized in PLACEHOLDER_LOCATIONS


def format_geo_coordinates(location: Mapping[str, Any]) -> str:
    return f"{float(location['latitude'])},{float(location['longitude'])}"


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
    """Identity values needed by tools during a single runtime action."""

    owner_id: str
    connection_id: str
    node_id: str | None = None
    speaker_id: str | None = None
    location_ref: dict[str, Any] | None = None
    device_kind: str | None = None


@dataclass(frozen=True, slots=True)
class ToolRuntimeContext:
    """Typed source for the dict stored in `session_context`."""

    identity: RuntimeIdentity
    timezone: str = "UTC"
    location: Optional[Dict[str, Any]] = None
    extras: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        identity = self.identity
        data: Dict[str, Any] = {
            **dict(self.extras),
            "owner_id": identity.owner_id,
            "connection_id": identity.connection_id,
            "timezone": self.timezone,
            "location": self.location,
        }
        if identity.node_id is not None:
            data["node_id"] = identity.node_id
        if identity.speaker_id is not None:
            data["speaker_id"] = identity.speaker_id
        if identity.location_ref is not None:
            data["location_ref"] = identity.location_ref
        if identity.device_kind is not None:
            data["device_kind"] = identity.device_kind
        return data


def bind_tool_context(context: ToolRuntimeContext) -> Token[Dict[str, Any]]:
    """Bind owner/connection context for code-executed tools or UI callbacks."""
    return session_context.set(context.as_dict())


def reset_tool_context(token: Token[Dict[str, Any]]) -> None:
    """Restore the previous tool context."""
    session_context.reset(token)


def get_ctx() -> Dict[str, Any]:
    """Get the full context dictionary."""
    return session_context.get()


def get_owner_id() -> str:
    """Get the durable owner namespace for storage, permissions, and memories."""
    owner_id = get_ctx().get("owner_id")
    if not owner_id:
        raise RuntimeError("No owner_id found in the current tool context.")
    return owner_id


def get_connection_id() -> str:
    """Get the live WebSocket connection id for this tool context."""
    connection_id = get_ctx().get("connection_id")
    if not connection_id:
        raise RuntimeError("No connection_id found in the current tool context.")
    return connection_id


def get_node_id() -> str | None:
    """Get the stable endpoint/node id when this tool came from a live node."""
    return get_ctx().get("node_id")


def get_timezone() -> str:
    """Get the current timezone IANA name (defaults to "UTC")."""
    return get_ctx().get("timezone", "UTC")


def get_tz() -> ZoneInfo:
    """Get the current timezone as a ZoneInfo object."""
    return ZoneInfo(get_timezone())


def to_local_iso(dt_str: str, tz: Optional[ZoneInfo] = None) -> str:
    """Convert an ISO datetime string to the user's local timezone.
    Handles Z-suffix UTC and offset-aware strings. Returns the original
    string unchanged on parse failure or empty input.
    """
    if not dt_str:
        return dt_str
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.astimezone(tz or get_tz()).isoformat()
    except (ValueError, TypeError):
        return dt_str


def ensure_aware(dt_str: str, tz: Optional[tzinfo] = None) -> datetime:
    """Parse an ISO datetime string, attaching tz to naive inputs.

    Naive strings (no offset, no Z) are treated as already being in tz
    (default: user's local tz). Offset-aware strings keep their tzinfo.
    Accepts any tzinfo subclass (ZoneInfo, datetime.timezone, etc.).
    Raises ValueError on unparseable input — use when you need a datetime,
    not a string passthrough.
    """
    dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz or get_tz())
    return dt


def get_location() -> Optional[Dict[str, Any]]:
    """Get the device-reported location coordinates for this turn, if any."""
    return get_ctx().get("location")


def _location_ref_is_room_bound(location_ref: Any) -> bool:
    if not isinstance(location_ref, Mapping):
        return False
    return bool(
        location_ref.get("ha_area_id")
        or location_ref.get("room_id")
        or location_ref.get("room_name")
    )


def allows_home_location_fallback(ctx: Mapping[str, Any] | None = None) -> bool:
    """Home Assistant home coords are only for fixed/room-bound endpoints.

    Interactive phone/web/desktop devices without fresh GPS must ask for an
    explicit place rather than silently using home. Policy keys off durable
    `device_kind` (and room binding), not the client-supplied surface hint.
    """
    ctx = ctx if ctx is not None else get_ctx()
    if _location_ref_is_room_bound(ctx.get("location_ref")):
        return True
    device_kind = ctx.get("device_kind")
    if device_kind in _INTERACTIVE_DEVICE_KINDS:
        return False
    # Satellites, headless/background turns without a device, and other
    # fixed endpoints without an interactive device kind.
    return True


_HOME_LOCATION_CACHE: Optional[Dict[str, Any]] = None
_HOME_LOCATION_TS: float = 0.0
_HOME_LOCATION_TTL_S: float = 3600.0


async def _get_home_location() -> Optional[Dict[str, Any]]:
    """The home's coordinates from Home Assistant config (cached). None if HA is unset."""
    global _HOME_LOCATION_CACHE, _HOME_LOCATION_TS
    if _HOME_LOCATION_CACHE is not None and (time.monotonic() - _HOME_LOCATION_TS) < _HOME_LOCATION_TTL_S:
        return _HOME_LOCATION_CACHE
    try:
        from core.integrations import integrations

        client = await integrations.get("smart_home")
        config = await client.get_config()
    except Exception as exc:  # HA unconfigured/unreachable — fall back to stale or None
        logger.debug("Home location unavailable: %s", exc)
        return _HOME_LOCATION_CACHE
    lat, lon = config.get("latitude"), config.get("longitude")
    if lat is None or lon is None:
        return None
    # HA location_name is the instance label (e.g. "Jarvis"), not a locality —
    # omit city so maps/weather do not treat it as a geographic bias string.
    _HOME_LOCATION_CACHE = {
        "latitude": float(lat),
        "longitude": float(lon),
        "source": "home",
    }
    _HOME_LOCATION_TS = time.monotonic()
    return _HOME_LOCATION_CACHE


async def resolve_current_location() -> Optional[Dict[str, Any]]:
    """Best-effort coordinates for location-aware tools (e.g. weather, maps).

    Precedence:
    1. Fresh device-reported GPS for the active turn.
    2. Home Assistant home coordinates — only for fixed/room-bound endpoints
       (satellites, room speakers) or non-interactive/background turns.
    """
    device = get_location()
    if device and device.get("latitude") is not None and device.get("longitude") is not None:
        geo = fresh_geo_position(device)
        if geo is not None:
            resolved = geo.as_dict()
            if "source" not in resolved:
                resolved["source"] = "device"
            elif resolved["source"] == "gps":
                resolved["source"] = "device"
            return resolved
        logger.debug("Ignoring invalid or stale device location")

    if allows_home_location_fallback():
        return await _get_home_location()
    return None


async def resolve_search_location() -> Optional[Dict[str, Any]]:
    """Best-effort center for soft local-search ranking.

    Unlike current-location resolution, an interactive device may use the
    configured home as a search preference when fresh device GPS is unavailable.
    Callers must use this only as a soft bias, never as the user's current position.
    """
    return await resolve_current_location() or await _get_home_location()
