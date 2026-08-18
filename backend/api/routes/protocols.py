"""Protocol definition API routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from api.deps.device_auth import require_owner_id
from services.database.mongodb import mongodb

router = APIRouter(prefix="/protocols", tags=["protocols"])


class ProtocolSummary(BaseModel):
    name: str
    description: str
    step_count: int
    run_count: int
    last_run_at: datetime | None = None
    prefetch_safe: bool
    updated_at: datetime | None = None


@router.get("/", response_model=list[ProtocolSummary])
async def list_protocols(owner_id: str = Depends(require_owner_id)) -> list[ProtocolSummary]:
    """List saved user protocols for the authenticated owner."""
    cursor = mongodb.db.protocols.find(
        {"owner_id": owner_id},
        {
            "_id": 0,
            "name": 1,
            "description": 1,
            "steps": 1,
            "run_count": 1,
            "last_run_at": 1,
            "prefetch_safe": 1,
            "updated_at": 1,
        },
    ).sort("updated_at", -1)

    docs = await cursor.to_list(length=200)
    return [
        ProtocolSummary(
            name=str(doc.get("name", "Protocol")),
            description=str(doc.get("description", "")),
            step_count=len(doc.get("steps") if isinstance(doc.get("steps"), list) else []),
            run_count=int(doc.get("run_count", 0)),
            last_run_at=doc.get("last_run_at"),
            prefetch_safe=bool(doc.get("prefetch_safe", False)),
            updated_at=doc.get("updated_at"),
        )
        for doc in docs
    ]
