from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import uuid4
from pydantic import BaseModel, Field

from .types import EventType

class Event(BaseModel):
    """Base model for all events in the system."""
    id: str = Field(default_factory=lambda: str(uuid4()))
    type: EventType
    data: Dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: Optional[str] = None
    correlation_id: Optional[str] = None  # For tracking related events

    class Config:
        json_encoders = {
            datetime: lambda dt: dt.isoformat()
        } 