from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field

class MongoBaseModel(BaseModel):
    """Base model for all MongoDB documents."""
    id: Optional[str] = Field(None, alias="_id")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    class Config:
        populate_by_name = True
        json_encoders = {
            datetime: lambda dt: dt.isoformat()
        } 