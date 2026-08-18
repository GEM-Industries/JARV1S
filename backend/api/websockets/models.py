from datetime import datetime, timezone
from typing import Optional, Any, Dict, Literal
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .types import WSMessageType

CLIENT_DIAGNOSTIC_EVENT_NAMES = frozenset(
    {
        "transport_transition",
        "mic_acquire",
        "mic_interrupted",
        "mic_flatline",
        "playback_summary",
        "playback_failed",
        "notification_failed",
        "location_unavailable",
    }
)
CLIENT_DIAGNOSTIC_MAX_EVENTS = 10
CLIENT_DIAGNOSTIC_MAX_METADATA_KEYS = 12
CLIENT_DIAGNOSTIC_MAX_STRING_LEN = 64
CLIENT_DIAGNOSTIC_MAX_KEY_LEN = 32
CLIENT_DIAGNOSTIC_EVENT_CATEGORIES = {
    "transport_transition": "transport",
    "mic_acquire": "mic",
    "mic_interrupted": "mic",
    "mic_flatline": "mic",
    "playback_summary": "playback",
    "playback_failed": "playback",
    "notification_failed": "notification",
    "location_unavailable": "location",
}


class WSMessage(BaseModel):
    """Base WebSocket message model."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: WSMessageType
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None

    class Config:
        json_encoders = {
            datetime: lambda dt: dt.isoformat()
        }


class WSResponse(BaseModel):
    """WebSocket response model."""
    message_id: str
    type: WSMessageType
    data: Dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        json_encoders = {
            datetime: lambda dt: dt.isoformat()
        }


def _truncate_diag_str(value: str, max_len: int = CLIENT_DIAGNOSTIC_MAX_STRING_LEN) -> str:
    cleaned = "".join(ch if ch.isprintable() else " " for ch in value).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return f"{cleaned[: max_len - 1]}…"


class ClientDiagnosticEvent(BaseModel):
    """One bounded client diagnostic breadcrumb."""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=0, le=2**53 - 1)
    ts: datetime
    category: Literal["transport", "mic", "playback", "notification"]
    event: str
    severity: Literal["info", "warning", "error"] = "info"
    turn_id: Optional[str] = Field(default=None, max_length=64)
    message_id: Optional[str] = Field(default=None, max_length=64)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("event")
    @classmethod
    def validate_event_name(cls, value: str) -> str:
        if value not in CLIENT_DIAGNOSTIC_EVENT_NAMES:
            raise ValueError(f"unsupported diagnostic event: {value}")
        return value

    @field_validator("turn_id", "message_id")
    @classmethod
    def sanitize_identifier(cls, value: Optional[str]) -> Optional[str]:
        return _truncate_diag_str(value) if value else None

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, value: Dict[str, Any]) -> Dict[str, Any]:
        if len(value) > CLIENT_DIAGNOSTIC_MAX_METADATA_KEYS:
            raise ValueError("too many metadata keys")
        sanitized: Dict[str, Any] = {}
        for raw_key, raw_val in value.items():
            if not isinstance(raw_key, str):
                continue
            key = _truncate_diag_str(raw_key, CLIENT_DIAGNOSTIC_MAX_KEY_LEN)
            if not key:
                continue
            if isinstance(raw_val, bool) or raw_val is None:
                sanitized[key] = raw_val
            elif isinstance(raw_val, int):
                sanitized[key] = raw_val
            elif isinstance(raw_val, float):
                if raw_val != raw_val or raw_val in (float("inf"), float("-inf")):
                    continue
                sanitized[key] = round(raw_val, 3)
            elif isinstance(raw_val, str):
                sanitized[key] = _truncate_diag_str(raw_val)
            else:
                continue
            if len(sanitized) >= CLIENT_DIAGNOSTIC_MAX_METADATA_KEYS:
                break
        return sanitized

    @model_validator(mode="after")
    def validate_category(self) -> "ClientDiagnosticEvent":
        if self.category != CLIENT_DIAGNOSTIC_EVENT_CATEGORIES[self.event]:
            raise ValueError("diagnostic category does not match event")
        return self


class ClientDiagnosticBatch(BaseModel):
    """Bounded client→server diagnostic batch."""

    model_config = ConfigDict(extra="forbid")

    events: list[ClientDiagnosticEvent] = Field(default_factory=list)
    dropped_count: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("events")
    @classmethod
    def validate_event_count(cls, value: list[ClientDiagnosticEvent]) -> list[ClientDiagnosticEvent]:
        if len(value) > CLIENT_DIAGNOSTIC_MAX_EVENTS:
            raise ValueError(f"at most {CLIENT_DIAGNOSTIC_MAX_EVENTS} events per batch")
        if not value:
            raise ValueError("events must not be empty")
        return value
