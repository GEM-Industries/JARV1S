"""Runtime user preferences API."""

from fastapi import APIRouter, Depends

from api.deps.device_auth import require_owner_id
from api.websockets.connection import manager
from core.preferences.models import UserPreferences, UserPreferencesPatch
from core.preferences.service import get_user_preferences, patch_user_preferences

router = APIRouter(prefix="/preferences", tags=["preferences"])


@router.get("/", response_model=UserPreferences)
async def get_preferences(owner_id: str = Depends(require_owner_id)) -> UserPreferences:
    return await get_user_preferences(owner_id)


@router.patch("/", response_model=UserPreferences)
async def patch_preferences(
    request: UserPreferencesPatch,
    owner_id: str = Depends(require_owner_id),
) -> UserPreferences:
    preferences = await patch_user_preferences(owner_id, request)
    await manager.broadcast_preferences_update(preferences)
    return preferences
