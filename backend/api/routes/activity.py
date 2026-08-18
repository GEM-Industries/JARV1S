"""Activity feed REST adapter."""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps.device_auth import require_owner_id
from core.activity import ActivityItem, ActivityPage, ActivityQuery, activity_page, recent_activity
from core.activity.models import ActivityCategory, ActivityKind, ActivityPageOutcome
from core.activity.page import PAGE_SIZE_DEFAULT, PAGE_SIZE_MAX
from core.activity.service import DEFAULT_ACTIVITY_LIMIT, MAX_ACTIVITY_LIMIT

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/page", response_model=ActivityPage)
async def get_activity_page(
    limit: int = Query(default=PAGE_SIZE_DEFAULT, ge=1, le=PAGE_SIZE_MAX),
    cursor: str | None = Query(default=None),
    category: ActivityCategory | None = Query(default=None),
    outcome: ActivityPageOutcome | None = Query(default=None),
    source: str | None = Query(default=None, max_length=120),
    node_id: str | None = Query(default=None, max_length=120),
    since: datetime | None = Query(default=None),
    until: datetime | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    owner_id: str = Depends(require_owner_id),
) -> ActivityPage:
    """Return an opaque-cursor page of compact activity pointers.

    When ``category`` is omitted, the page is operational only (reminders,
    automations, tasks, system). Pass ``category=conversation`` for the
    Conversations facet.
    """
    query = ActivityQuery(
        category=category,
        outcome=outcome,
        source=source,
        node_id=node_id,
        since=since,
        until=until,
        search=search,
    )
    try:
        return await activity_page(
            owner_id,
            query=query,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/", response_model=list[ActivityItem])
async def get_activity(
    limit: int = Query(default=DEFAULT_ACTIVITY_LIMIT, ge=1, le=MAX_ACTIVITY_LIMIT),
    kind: ActivityKind | None = Query(default=None, description="Filter: headless, task, trigger, or automation"),
    include_user: bool = Query(default=False, description="Fold user-initiated turns into the unified timeline"),
    owner_id: str = Depends(require_owner_id),
):
    """Recent silent work, trigger outcomes, automation failures, and background tasks.

    User-initiated voice/text turns are excluded by default. Pass
    ``include_user=true`` only when a caller explicitly needs chat mixed in,
    or use ``GET /api/v1/activity/page?category=conversation`` /
    ``GET /api/v1/operations/turns`` for the Conversations facet.
    """
    return await recent_activity(
        owner_id,
        limit=limit,
        kind=kind,
        include_user=include_user,
    )
