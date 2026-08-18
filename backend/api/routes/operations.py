"""Operations run-detail API routes."""

from fastapi import APIRouter, Depends, HTTPException, Query

from api.deps.device_auth import require_owner_id
from core.activity.models import ActivityItem
from core.operations import (
    OperationRunDetail,
    get_trigger_run_detail,
    get_user_turn_detail,
    list_user_turns,
)
from core.operations.projection import ManagedSetup, SetupType, find_managed_setups, resolve_managed_setup
from core.operations.lifecycle_dispatch import (
    delete_managed_setup,
    pause_managed_setup,
    resume_managed_setup,
)
from core.operations.definitions import (
    SetupKind,
    SetupStatus,
)
from core.operations.setups import SetupPatch

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get(
    "/setups",
    response_model=list[ManagedSetup],
    response_model_exclude_none=True,
)
async def get_setups(
    kind: SetupKind | None = Query(default=None),
    status: SetupStatus | None = Query(default=None),
    setup_type: SetupType | None = Query(default=None),
    search: str | None = Query(default=None, max_length=120),
    owner_id: str = Depends(require_owner_id),
) -> list[ManagedSetup]:
    """Return the unified catalog of user-managed configured behavior."""
    rows = await find_managed_setups(
        owner_id,
        query=search,
        status=status,
        setup_type=setup_type,
    )
    if kind:
        rows = [row for row in rows if row.kind == kind]
    return rows


@router.get(
    "/setups/{setup_ref}",
    response_model=ManagedSetup,
    response_model_exclude_none=True,
)
async def get_setup(
    setup_ref: str,
    owner_id: str = Depends(require_owner_id),
) -> ManagedSetup:
    result = await resolve_managed_setup(owner_id, setup_ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Setup {setup_ref} not found.")
    if isinstance(result, list):
        raise HTTPException(status_code=409, detail=f"Setup {setup_ref} is ambiguous.")
    return result


@router.delete("/setups/{setup_ref}", status_code=204)
async def remove_setup(
    setup_ref: str,
    owner_id: str = Depends(require_owner_id),
) -> None:
    resolved = await resolve_managed_setup(owner_id, setup_ref)
    if resolved is None or isinstance(resolved, list):
        raise HTTPException(status_code=404, detail=f"Setup {setup_ref} not found.")
    try:
        await delete_managed_setup(owner_id, resolved)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch(
    "/setups/{setup_ref}",
    response_model=ManagedSetup,
    response_model_exclude_none=True,
)
async def update_setup(
    setup_ref: str,
    patch: SetupPatch,
    owner_id: str = Depends(require_owner_id),
) -> ManagedSetup:
    """Pause or resume one managed setup through its owning domain."""
    resolved = await resolve_managed_setup(owner_id, setup_ref)
    if resolved is None:
        raise HTTPException(status_code=404, detail=f"Setup {setup_ref} not found.")
    if isinstance(resolved, list):
        raise HTTPException(status_code=409, detail=f"Setup {setup_ref} is ambiguous.")
    try:
        if patch.enabled is False:
            await pause_managed_setup(owner_id, resolved)
        elif "paused_until" in patch.model_fields_set and patch.paused_until is not None:
            await pause_managed_setup(owner_id, resolved, until=patch.paused_until)
        elif patch.enabled is True or (
            "paused_until" in patch.model_fields_set and patch.paused_until is None
        ):
            await resume_managed_setup(owner_id, resolved)
        else:
            raise ValueError("Provide enabled or paused_until")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    updated = await resolve_managed_setup(owner_id, resolved.resource_ref)
    if updated is None or isinstance(updated, list):
        raise HTTPException(status_code=404, detail=f"Setup {setup_ref} not found after update.")
    return updated


@router.get("/runs/{instance_id}", response_model=OperationRunDetail)
async def get_operation_run(
    instance_id: str,
    owner_id: str = Depends(require_owner_id),
) -> OperationRunDetail:
    """Return lifecycle, trace, perf, and protocol detail for a trigger run."""
    detail = await get_trigger_run_detail(owner_id, instance_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Run {instance_id} not found.")
    return detail


@router.get("/turns", response_model=list[ActivityItem])
async def get_user_turn_list(
    node_id: str | None = Query(default=None, description="Filter to turns from this node"),
    limit: int = Query(default=50, ge=1, le=200),
    owner_id: str = Depends(require_owner_id),
) -> list[ActivityItem]:
    """Opt-in list of user-initiated turns for Operations facets (not default activity)."""
    return await list_user_turns(owner_id, limit=limit, node_id=node_id)


@router.get("/turns/{turn_id}", response_model=OperationRunDetail)
async def get_user_turn(
    turn_id: str,
    owner_id: str = Depends(require_owner_id),
) -> OperationRunDetail:
    """Return trace and telemetry for a bare user turn keyed by turn_id."""
    detail = await get_user_turn_detail(owner_id, turn_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Turn {turn_id} not found.")
    return detail
