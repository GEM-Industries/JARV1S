"""Product credential management REST API."""

from fastapi import APIRouter, Depends, HTTPException, Response

from api.deps.device_auth import require_device
from core.credentials.models import (
    CredentialActionResult,
    CredentialUpsertRequest,
    CredentialValidationResult,
    CredentialsListResponse,
)
from core.credentials import service as credentials_service

router = APIRouter(
    prefix="/credentials",
    tags=["credentials"],
    dependencies=[Depends(require_device)],
)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/", response_model=CredentialsListResponse)
async def list_credentials(response: Response) -> CredentialsListResponse:
    _no_store(response)
    return await credentials_service.list_credentials()


@router.put("/{credential_id}", response_model=CredentialActionResult)
async def save_credential(
    credential_id: str,
    body: CredentialUpsertRequest,
    response: Response,
) -> CredentialActionResult:
    _no_store(response)
    try:
        return await credentials_service.save_credential(credential_id, body.value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown credential.") from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{credential_id}", response_model=CredentialActionResult)
async def remove_credential(credential_id: str, response: Response) -> CredentialActionResult:
    _no_store(response)
    try:
        return await credentials_service.remove_credential(credential_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown credential.") from exc


@router.post("/{credential_id}/validate", response_model=CredentialValidationResult)
async def validate_credential(
    credential_id: str,
    body: CredentialUpsertRequest,
    response: Response,
) -> CredentialValidationResult:
    _no_store(response)
    try:
        return await credentials_service.validate_credential(credential_id, body.value)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Unknown credential.") from exc
